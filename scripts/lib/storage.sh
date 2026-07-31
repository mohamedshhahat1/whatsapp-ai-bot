#!/usr/bin/env sh
#
# Off-site storage provider abstraction.
#
# Sourced by backup-upload.sh, backup-download.sh and backup-verify-remote.sh.
# Not executable on its own.
#
# One contract, four backends. Every provider implements exactly four verbs:
#
#   storage_put    <local_file> <remote_key>
#   storage_get    <remote_key> <local_file>
#   storage_list   <remote_prefix>          -> keys on stdout, one per line
#   storage_delete <remote_key>
#
# Callers never branch on the provider. That is the whole point: restore.sh
# must work identically whether the bytes came from S3 or Azure, because the
# day you need it is not the day to discover the restore path was only ever
# tested against one backend.
#
# Provider is selected with BACKUP_REMOTE_PROVIDER:
#   s3     AWS S3 (also any S3-compatible endpoint via BACKUP_S3_ENDPOINT)
#   b2     Backblaze B2 (via its S3-compatible API -- see note below)
#   gcs    Google Cloud Storage
#   azure  Azure Blob Storage
#   none   off-site disabled (default; every verb becomes a no-op)
#
# Backblaze deliberately goes through the S3-compatible endpoint rather than
# the native b2 CLI. It means one code path instead of two, it is the path
# Backblaze themselves document for tooling, and it keeps credentials in the
# same AWS_* shape. The only B2-specific part is defaulting the endpoint.

# --------------------------------------------------------------------------
# Retry wrapper.
#
# Every network verb goes through this. Object storage fails transiently far
# more often than people expect -- DNS blips, 503 SlowDown from S3, a token
# refresh landing mid-request. A backup that gives up on the first 503 is a
# backup that silently stops existing during the exact week the network is
# having a bad time.
#
# Exponential backoff, capped. Deliberately not jittered: there is one backup
# process, not a fleet, so there is no thundering herd to spread out.
# --------------------------------------------------------------------------
STORAGE_RETRY_ATTEMPTS="${BACKUP_REMOTE_RETRY_ATTEMPTS:-5}"
STORAGE_RETRY_BASE_SECONDS="${BACKUP_REMOTE_RETRY_BASE_SECONDS:-3}"
STORAGE_RETRY_MAX_SECONDS="${BACKUP_REMOTE_RETRY_MAX_SECONDS:-60}"

storage_log() {
    printf '%s [storage] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

_storage_retry() {
    _label="$1"
    shift
    _attempt=1
    _delay="${STORAGE_RETRY_BASE_SECONDS}"

    while : ; do
        if "$@"; then
            [ "${_attempt}" -gt 1 ] && storage_log "${_label} succeeded on attempt ${_attempt}"
            return 0
        fi

        if [ "${_attempt}" -ge "${STORAGE_RETRY_ATTEMPTS}" ]; then
            storage_log "${_label} failed after ${_attempt} attempts"
            return 1
        fi

        storage_log "${_label} failed (attempt ${_attempt}), retrying in ${_delay}s"
        sleep "${_delay}"
        _attempt=$((_attempt + 1))
        _delay=$((_delay * 2))
        [ "${_delay}" -gt "${STORAGE_RETRY_MAX_SECONDS}" ] && _delay="${STORAGE_RETRY_MAX_SECONDS}"
    done
}

# --------------------------------------------------------------------------
# Configuration and validation.
#
# Fails at startup with a specific message rather than at 02:00 with a stack
# trace. Each provider states exactly which variables it needs.
# --------------------------------------------------------------------------
STORAGE_PROVIDER="$(printf '%s' "${BACKUP_REMOTE_PROVIDER:-none}" | tr '[:upper:]' '[:lower:]')"
STORAGE_BUCKET="${BACKUP_REMOTE_BUCKET:-}"
STORAGE_PREFIX="${BACKUP_REMOTE_PREFIX:-whatsapp-ai-bot}"

storage_enabled() {
    [ "${STORAGE_PROVIDER}" != "none" ] && [ -n "${STORAGE_PROVIDER}" ]
}

_storage_require_bucket() {
    if [ -z "${STORAGE_BUCKET}" ]; then
        storage_log "BACKUP_REMOTE_BUCKET is required when BACKUP_REMOTE_PROVIDER=${STORAGE_PROVIDER}"
        return 1
    fi
}

_storage_require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        storage_log "the '$1' command is required for provider ${STORAGE_PROVIDER} but is not installed"
        return 1
    fi
}

# Read a credential from a Docker secret file when <NAME>_FILE is set,
# otherwise from the plain variable. Same convention the application uses, so
# operators learn one rule and it holds everywhere.
storage_read_secret() {
    _var_name="$1"
    _file_var="${_var_name}_FILE"

    eval "_file_path=\${${_file_var}:-}"
    if [ -n "${_file_path}" ] && [ -f "${_file_path}" ]; then
        cat "${_file_path}"
        return 0
    fi

    eval "printf '%s' \"\${${_var_name}:-}\""
}

# --------------------------------------------------------------------------
# Provider: S3 and Backblaze B2 (S3-compatible API).
# --------------------------------------------------------------------------
_s3_endpoint_args() {
    if [ -n "${BACKUP_S3_ENDPOINT:-}" ]; then
        printf -- '--endpoint-url %s' "${BACKUP_S3_ENDPOINT}"
    fi
}

_storage_init_s3() {
    _storage_require_cmd aws || return 1
    _storage_require_bucket || return 1

    AWS_ACCESS_KEY_ID="$(storage_read_secret BACKUP_S3_ACCESS_KEY_ID)"
    AWS_SECRET_ACCESS_KEY="$(storage_read_secret BACKUP_S3_SECRET_ACCESS_KEY)"

    if [ -z "${AWS_ACCESS_KEY_ID}" ] || [ -z "${AWS_SECRET_ACCESS_KEY}" ]; then
        storage_log "BACKUP_S3_ACCESS_KEY_ID / BACKUP_S3_SECRET_ACCESS_KEY are required"
        return 1
    fi

    AWS_DEFAULT_REGION="${BACKUP_S3_REGION:-us-east-1}"
    export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION

    # B2's S3 endpoint follows a fixed pattern. Default it so a B2 operator
    # only has to supply the region, not memorise the hostname. The scheme is
    # held in a variable so the host can be overridden in one place.
    if [ "${STORAGE_PROVIDER}" = "b2" ] && [ -z "${BACKUP_S3_ENDPOINT:-}" ]; then
        _b2_scheme="https"
        _b2_host="s3.${AWS_DEFAULT_REGION}.backblazeb2.com"
        BACKUP_S3_ENDPOINT="${_b2_scheme}://${_b2_host}"
        export BACKUP_S3_ENDPOINT
        storage_log "defaulted B2 endpoint to ${BACKUP_S3_ENDPOINT}"
    fi
}

_s3_put() {
    # shellcheck disable=SC2046
    aws $(_s3_endpoint_args) s3 cp "$1" "s3://${STORAGE_BUCKET}/$2" \
        --only-show-errors ${BACKUP_S3_STORAGE_CLASS:+--storage-class "${BACKUP_S3_STORAGE_CLASS}"}
}

_s3_get() {
    # shellcheck disable=SC2046
    aws $(_s3_endpoint_args) s3 cp "s3://${STORAGE_BUCKET}/$1" "$2" --only-show-errors
}

_s3_list() {
    # --recursive prints "<date> <time> <size> <key>"; we want bare keys.
    # shellcheck disable=SC2046
    aws $(_s3_endpoint_args) s3 ls "s3://${STORAGE_BUCKET}/$1" --recursive \
        | awk '{ $1=""; $2=""; $3=""; sub(/^[ \t]+/, ""); print }'
}

_s3_delete() {
    # shellcheck disable=SC2046
    aws $(_s3_endpoint_args) s3 rm "s3://${STORAGE_BUCKET}/$1" --only-show-errors
}

# --------------------------------------------------------------------------
# Provider: Google Cloud Storage.
# --------------------------------------------------------------------------
_storage_init_gcs() {
    _storage_require_cmd gcloud || return 1
    _storage_require_bucket || return 1

    _key_file="${BACKUP_GCS_CREDENTIALS_FILE:-}"
    if [ -z "${_key_file}" ] || [ ! -f "${_key_file}" ]; then
        storage_log "BACKUP_GCS_CREDENTIALS_FILE must point at a service account JSON key"
        return 1
    fi

    if ! gcloud auth activate-service-account --key-file="${_key_file}" --quiet >/dev/null 2>&1; then
        storage_log "gcloud could not authenticate with ${_key_file}"
        return 1
    fi

    if [ -n "${BACKUP_GCS_PROJECT:-}" ]; then
        gcloud config set project "${BACKUP_GCS_PROJECT}" --quiet >/dev/null 2>&1 || true
    fi

    GOOGLE_APPLICATION_CREDENTIALS="${_key_file}"
    export GOOGLE_APPLICATION_CREDENTIALS
}

_gcs_put() {
    gcloud storage cp "$1" "gs://${STORAGE_BUCKET}/$2" --quiet
}

_gcs_get() {
    gcloud storage cp "gs://${STORAGE_BUCKET}/$1" "$2" --quiet
}

_gcs_list() {
    # Strip the gs://bucket/ prefix so callers see the same bare keys as S3.
    gcloud storage ls "gs://${STORAGE_BUCKET}/$1**" --quiet 2>/dev/null \
        | sed "s#^gs://${STORAGE_BUCKET}/##"
}

_gcs_delete() {
    gcloud storage rm "gs://${STORAGE_BUCKET}/$1" --quiet
}

# --------------------------------------------------------------------------
# Provider: Azure Blob Storage.
# --------------------------------------------------------------------------
_storage_init_azure() {
    _storage_require_cmd az || return 1
    _storage_require_bucket || return 1

    AZURE_STORAGE_ACCOUNT="${BACKUP_AZURE_ACCOUNT:-}"
    if [ -z "${AZURE_STORAGE_ACCOUNT}" ]; then
        storage_log "BACKUP_AZURE_ACCOUNT is required for provider azure"
        return 1
    fi

    # A SAS token is preferred over an account key: it can be scoped to one
    # container and expired independently. Both are supported.
    AZURE_STORAGE_SAS_TOKEN="$(storage_read_secret BACKUP_AZURE_SAS_TOKEN)"
    AZURE_STORAGE_KEY="$(storage_read_secret BACKUP_AZURE_ACCOUNT_KEY)"

    if [ -z "${AZURE_STORAGE_SAS_TOKEN}" ] && [ -z "${AZURE_STORAGE_KEY}" ]; then
        storage_log "one of BACKUP_AZURE_SAS_TOKEN or BACKUP_AZURE_ACCOUNT_KEY is required"
        return 1
    fi

    export AZURE_STORAGE_ACCOUNT
    [ -n "${AZURE_STORAGE_SAS_TOKEN}" ] && export AZURE_STORAGE_SAS_TOKEN
    [ -n "${AZURE_STORAGE_KEY}" ] && export AZURE_STORAGE_KEY
    return 0
}

_azure_put() {
    az storage blob upload --container-name "${STORAGE_BUCKET}" \
        --file "$1" --name "$2" --overwrite --only-show-errors --output none
}

_azure_get() {
    az storage blob download --container-name "${STORAGE_BUCKET}" \
        --name "$1" --file "$2" --only-show-errors --output none
}

_azure_list() {
    az storage blob list --container-name "${STORAGE_BUCKET}" \
        --prefix "$1" --query "[].name" --output tsv --only-show-errors
}

_azure_delete() {
    az storage blob delete --container-name "${STORAGE_BUCKET}" \
        --name "$1" --only-show-errors --output none
}

# --------------------------------------------------------------------------
# Public dispatch.
# --------------------------------------------------------------------------
storage_init() {
    if ! storage_enabled; then
        storage_log "off-site storage disabled (BACKUP_REMOTE_PROVIDER=none)"
        return 0
    fi

    case "${STORAGE_PROVIDER}" in
        s3|b2) _storage_init_s3 ;;
        gcs)   _storage_init_gcs ;;
        azure) _storage_init_azure ;;
        *)
            storage_log "unknown BACKUP_REMOTE_PROVIDER '${STORAGE_PROVIDER}' (expected s3, b2, gcs, azure or none)"
            return 1
            ;;
    esac
}

storage_put() {
    storage_enabled || return 0
    case "${STORAGE_PROVIDER}" in
        s3|b2) _storage_retry "put $2" _s3_put "$1" "$2" ;;
        gcs)   _storage_retry "put $2" _gcs_put "$1" "$2" ;;
        azure) _storage_retry "put $2" _azure_put "$1" "$2" ;;
    esac
}

storage_get() {
    storage_enabled || return 1
    case "${STORAGE_PROVIDER}" in
        s3|b2) _storage_retry "get $1" _s3_get "$1" "$2" ;;
        gcs)   _storage_retry "get $1" _gcs_get "$1" "$2" ;;
        azure) _storage_retry "get $1" _azure_get "$1" "$2" ;;
    esac
}

storage_list() {
    storage_enabled || return 0
    case "${STORAGE_PROVIDER}" in
        s3|b2) _storage_retry "list $1" _s3_list "$1" ;;
        gcs)   _storage_retry "list $1" _gcs_list "$1" ;;
        azure) _storage_retry "list $1" _azure_list "$1" ;;
    esac
}

storage_delete() {
    storage_enabled || return 0
    case "${STORAGE_PROVIDER}" in
        s3|b2) _storage_retry "delete $1" _s3_delete "$1" ;;
        gcs)   _storage_retry "delete $1" _gcs_delete "$1" ;;
        azure) _storage_retry "delete $1" _azure_delete "$1" ;;
    esac
}

# Full remote key for a tier and filename, e.g.
#   whatsapp-ai-bot/daily/whatsapp_ai_bot-20260731T0200Z.dump.enc
storage_key() {
    printf '%s/%s/%s' "${STORAGE_PREFIX}" "$1" "$2"
}
