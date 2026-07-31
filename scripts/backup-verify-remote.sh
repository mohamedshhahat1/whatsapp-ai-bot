#!/usr/bin/env sh
#
# Verify off-site backups without restoring them.
#
#   ./scripts/backup-verify-remote.sh              # newest daily
#   ./scripts/backup-verify-remote.sh --all        # every object, every tier
#   ./scripts/backup-verify-remote.sh --tier monthly
#
# Checks, in order:
#   1. the tier is not empty
#   2. the newest object is not older than BACKUP_REMOTE_MAX_AGE_HOURS
#   3. every object has a .sha256 companion
#   4. the bytes download and match that checksum
#
# Step 4 is the one that matters. Cloud storage does not silently corrupt
# objects often, but "the upload job has been writing 0-byte files for three
# weeks" is a genuine and common failure, and listing alone will not catch it.
#
# Writes state/remote_verify.json for the healthcheck and Prometheus.

set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=scripts/lib/storage.sh
. "${SCRIPT_DIR}/lib/storage.sh"

BACKUP_DIR="${BACKUP_DIR:-/backups}"
STATE_DIR="${BACKUP_DIR}/state"
MAX_AGE_HOURS="${BACKUP_REMOTE_MAX_AGE_HOURS:-30}"

TIER="daily"
CHECK_ALL="false"

mkdir -p "${STATE_DIR}"

log() {
    printf '%s [verify-remote] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

ERRORS=0
CHECKED=0

note_error() {
    log "FAIL: $*"
    ERRORS=$((ERRORS + 1))
}

while [ $# -gt 0 ]; do
    case "$1" in
        --all)  CHECK_ALL="true"; shift ;;
        --tier) TIER="$2"; shift 2 ;;
        *) shift ;;
    esac
done

write_state() {
    _status="$1"
    cat > "${STATE_DIR}/remote_verify.json" <<JSON
{
  "status": "${_status}",
  "provider": "${STORAGE_PROVIDER}",
  "bucket": "${STORAGE_BUCKET}",
  "checked": ${CHECKED},
  "errors": ${ERRORS},
  "finished_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
}

if ! storage_enabled; then
    log "off-site storage disabled; nothing to verify"
    write_state "skipped"
    exit 0
fi

storage_init || { note_error "storage init failed"; write_state "failed"; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT INT TERM

verify_object() {
    _key="$1"
    CHECKED=$((CHECKED + 1))

    _local="${TMP}/$(basename "${_key}")"

    if ! storage_get "${_key}" "${_local}"; then
        note_error "could not download ${_key}"
        return 1
    fi

    if [ ! -s "${_local}" ]; then
        note_error "${_key} is a zero-byte object"
        return 1
    fi

    if ! storage_get "${_key}.sha256" "${_local}.sha256" 2>/dev/null; then
        note_error "${_key} has no .sha256 companion"
        return 1
    fi

    _expected="$(awk '{print $1}' < "${_local}.sha256")"
    _actual="$(sha256sum "${_local}" | awk '{print $1}')"
    if [ "${_expected}" != "${_actual}" ]; then
        note_error "${_key} checksum mismatch (expected ${_expected}, got ${_actual})"
        return 1
    fi

    log "OK ${_key} ($(wc -c < "${_local}" | tr -d ' ') bytes)"
    rm -f "${_local}" "${_local}.sha256"
    return 0
}

check_tier() {
    _tier="$1"
    _prefix="${STORAGE_PREFIX}/${_tier}/"

    _keys="$(storage_list "${_prefix}" | grep -v '\.sha256$' | sort -r || true)"

    if [ -z "${_keys}" ]; then
        note_error "tier '${_tier}' is empty -- no off-site backups exist at ${_prefix}"
        return 1
    fi

    _newest="$(printf '%s\n' "${_keys}" | head -n 1)"

    # Age is derived from the timestamp embedded in the filename rather than
    # from object metadata: metadata differs across the four providers, the
    # filename does not.
    _stamp="$(printf '%s' "${_newest}" | sed -n 's/.*-\([0-9]\{8\}T[0-9]\{4\}Z\).*/\1/p')"
    if [ -n "${_stamp}" ]; then
        _y="$(printf '%s' "${_stamp}" | cut -c1-4)"
        _m="$(printf '%s' "${_stamp}" | cut -c5-6)"
        _d="$(printf '%s' "${_stamp}" | cut -c7-8)"
        _hh="$(printf '%s' "${_stamp}" | cut -c10-11)"
        _mm="$(printf '%s' "${_stamp}" | cut -c12-13)"
        _epoch="$(date -u -d "${_y}-${_m}-${_d} ${_hh}:${_mm}:00" +%s 2>/dev/null \
                  || date -u -j -f '%Y-%m-%d %H:%M:%S' "${_y}-${_m}-${_d} ${_hh}:${_mm}:00" +%s 2>/dev/null \
                  || printf '')"
        if [ -n "${_epoch}" ]; then
            _age_h=$(( ( $(date -u +%s) - _epoch ) / 3600 ))
            if [ "${_age_h}" -gt "${MAX_AGE_HOURS}" ]; then
                note_error "newest ${_tier} backup is ${_age_h}h old (limit ${MAX_AGE_HOURS}h) -- uploads have stopped"
            else
                log "newest ${_tier} backup is ${_age_h}h old"
            fi
        fi
    fi

    if [ "${CHECK_ALL}" = "true" ]; then
        printf '%s\n' "${_keys}" | while read -r _k; do
            [ -n "${_k}" ] && verify_object "${_k}" || true
        done
    else
        verify_object "${_newest}" || true
    fi
}

if [ "${CHECK_ALL}" = "true" ]; then
    for t in daily weekly monthly; do
        check_tier "${t}" || true
    done
else
    check_tier "${TIER}" || true
fi

if [ "${ERRORS}" -gt 0 ]; then
    write_state "failed"
    log "verification FAILED: ${ERRORS} error(s) across ${CHECKED} object(s)"
    exit 1
fi

date -u +%s > "${STATE_DIR}/last_remote_verify_success"
write_state "ok"
log "verification passed: ${CHECKED} object(s) checked"
