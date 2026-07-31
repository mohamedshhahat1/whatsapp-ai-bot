#!/usr/bin/env sh
#
# Encrypt one local backup and upload it off-site, then enforce remote
# retention.
#
# Called by backup.sh immediately after a dump verifies locally, and runnable
# by hand:
#
#   ./scripts/backup-upload.sh daily/whatsapp_ai_bot-20260731T0200Z.dump
#   ./scripts/backup-upload.sh --all-tiers
#
# Order of operations is deliberate: compress -> encrypt -> checksum -> upload
# -> verify remote size -> record state. The checksum is taken over the
# ENCRYPTED bytes, because that is what actually has to survive the round
# trip. A checksum of the plaintext would still match after a corrupted
# upload/download cycle only by luck.
#
# The dump is already -Z 9 compressed by pg_dump, so we do not re-compress the
# archive itself; COMPRESS_BEFORE_ENCRYPT exists for operators who switch
# backup.sh to an uncompressed format.

set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=scripts/lib/storage.sh
. "${SCRIPT_DIR}/lib/storage.sh"

BACKUP_DIR="${BACKUP_DIR:-/backups}"
STATE_DIR="${BACKUP_DIR}/state"
LOG_FILE="${STATE_DIR}/upload.log"

RETAIN_REMOTE_DAILY="${RETAIN_REMOTE_DAILY:-30}"
RETAIN_REMOTE_WEEKLY="${RETAIN_REMOTE_WEEKLY:-12}"
RETAIN_REMOTE_MONTHLY="${RETAIN_REMOTE_MONTHLY:-24}"

COMPRESS_BEFORE_ENCRYPT="${BACKUP_COMPRESS_BEFORE_ENCRYPT:-false}"

mkdir -p "${STATE_DIR}"

log() {
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "${LOG_FILE}"
}

fail() {
    log "ERROR: $*"
    cat > "${STATE_DIR}/last_upload.json" <<JSON
{"status":"failed","finished_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","error":"$*"}
JSON
    exit 1
}

if ! storage_enabled; then
    log "off-site upload skipped: BACKUP_REMOTE_PROVIDER is not set"
    exit 0
fi

storage_init || fail "storage provider initialisation failed"

# --------------------------------------------------------------------------
# Encryption.
#
# age when available (modern, small, no key-management footguns), otherwise
# OpenSSL AES-256-CBC with PBKDF2. Refusing to upload unencrypted is a hard
# rule: the whole point of off-site is that the bytes now live somewhere you
# do not control, and a Postgres dump contains every customer phone number and
# every message they ever sent.
# --------------------------------------------------------------------------
encrypt_file() {
    _plain="$1"
    _cipher="$2"

    _age_recipient="$(storage_read_secret BACKUP_AGE_RECIPIENT)"
    _passphrase="$(storage_read_secret BACKUP_ENCRYPTION_PASSPHRASE)"

    if [ -n "${_age_recipient}" ] && command -v age >/dev/null 2>&1; then
        age --recipient "${_age_recipient}" --output "${_cipher}" "${_plain}" \
            || return 1
        printf 'age'
        return 0
    fi

    if [ -n "${_passphrase}" ]; then
        printf '%s' "${_passphrase}" > "${TMPDIR_RUN}/pass"
        openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
            -in "${_plain}" -out "${_cipher}" \
            -pass "file:${TMPDIR_RUN}/pass" || return 1
        rm -f "${TMPDIR_RUN}/pass"
        printf 'openssl'
        return 0
    fi

    return 2
}

TMPDIR_RUN="$(mktemp -d)"
cleanup() { rm -rf "${TMPDIR_RUN}"; }
trap cleanup EXIT INT TERM

upload_one() {
    _rel="$1"
    _tier="$(dirname "${_rel}")"
    _base="$(basename "${_rel}")"
    _local="${BACKUP_DIR}/${_rel}"

    [ -f "${_local}" ] || fail "no such backup: ${_local}"

    _work="${TMPDIR_RUN}/${_base}"
    cp "${_local}" "${_work}"

    if [ "${COMPRESS_BEFORE_ENCRYPT}" = "true" ]; then
        gzip -9 -f "${_work}"
        _work="${_work}.gz"
        _base="${_base}.gz"
    fi

    _cipher="${_work}.enc"
    if _method="$(encrypt_file "${_work}" "${_cipher}")"; then
        _base="${_base}.enc"
    else
        _rc=$?
        if [ "${_rc}" -eq 2 ]; then
            fail "refusing to upload unencrypted -- set BACKUP_AGE_RECIPIENT or BACKUP_ENCRYPTION_PASSPHRASE"
        fi
        fail "encryption failed for ${_rel}"
    fi
    log "encrypted ${_rel} using ${_method}"

    # Checksum over the ciphertext: this is what must survive the round trip.
    _sum="$(sha256sum "${_cipher}" | awk '{print $1}')"
    printf '%s  %s\n' "${_sum}" "${_base}" > "${_cipher}.sha256"
    _size="$(wc -c < "${_cipher}" | tr -d ' ')"

    _key="$(storage_key "${_tier}" "${_base}")"
    _sumkey="${_key}.sha256"

    storage_put "${_cipher}" "${_key}" || fail "upload failed for ${_key}"
    storage_put "${_cipher}.sha256" "${_sumkey}" || fail "checksum upload failed for ${_sumkey}"

    log "uploaded ${_key} size=${_size} sha256=${_sum}"

    # Read-back verification: prove the object is actually listable at the
    # destination. An upload that returns 0 but wrote nothing is a real
    # failure mode with S3-compatible gateways.
    if ! storage_list "${_key}" | grep -qF "${_base}"; then
        fail "post-upload verification failed: ${_key} is not listable"
    fi
    log "verified remote object ${_key}"

    UPLOADED_KEY="${_key}"
    UPLOADED_SHA="${_sum}"
    UPLOADED_SIZE="${_size}"

    rm -f "${_work}" "${_cipher}" "${_cipher}.sha256"
}

# --------------------------------------------------------------------------
# Remote retention. Prune by count, newest-first, exactly like the local
# tiers -- an age-based rule empties the bucket after an outage.
# --------------------------------------------------------------------------
prune_remote() {
    _tier="$1"
    _keep="$2"
    _prefix="${STORAGE_PREFIX}/${_tier}/"

    _keys="$(storage_list "${_prefix}" | grep -v '\.sha256$' | sort -r || true)"
    [ -z "${_keys}" ] && return 0

    _n=0
    printf '%s\n' "${_keys}" | while read -r _k; do
        [ -z "${_k}" ] && continue
        _n=$((_n + 1))
        if [ "${_n}" -gt "${_keep}" ]; then
            storage_delete "${_k}" && log "pruned remote ${_k}"
            storage_delete "${_k}.sha256" >/dev/null 2>&1 || true
        fi
    done
}

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if [ "${1:-}" = "--all-tiers" ]; then
    for tier in daily weekly monthly; do
        for f in "${BACKUP_DIR}/${tier}"/*.dump; do
            [ -f "${f}" ] || continue
            upload_one "${tier}/$(basename "${f}")"
        done
    done
elif [ -n "${1:-}" ]; then
    upload_one "$1"
else
    newest="$(ls -1t "${BACKUP_DIR}/daily"/*.dump 2>/dev/null | head -n 1 || true)"
    [ -n "${newest}" ] || fail "no local backup found to upload"
    upload_one "daily/$(basename "${newest}")"
fi

prune_remote daily "${RETAIN_REMOTE_DAILY}"
prune_remote weekly "${RETAIN_REMOTE_WEEKLY}"
prune_remote monthly "${RETAIN_REMOTE_MONTHLY}"

date -u +%s > "${STATE_DIR}/last_upload_success"

cat > "${STATE_DIR}/last_upload.json" <<JSON
{
  "status": "ok",
  "provider": "${STORAGE_PROVIDER}",
  "bucket": "${STORAGE_BUCKET}",
  "started_at": "${started_at}",
  "finished_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "key": "${UPLOADED_KEY:-}",
  "sha256": "${UPLOADED_SHA:-}",
  "size_bytes": ${UPLOADED_SIZE:-0}
}
JSON

log "off-site upload complete provider=${STORAGE_PROVIDER} bucket=${STORAGE_BUCKET}"
