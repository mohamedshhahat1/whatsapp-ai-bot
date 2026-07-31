#!/usr/bin/env sh
#
# Fetch a backup from off-site storage and decrypt it back to a usable dump.
#
#   ./scripts/backup-download.sh latest
#   ./scripts/backup-download.sh latest --tier monthly
#   ./scripts/backup-download.sh daily/whatsapp_ai_bot-20260731T0200Z.dump.enc
#   ./scripts/backup-download.sh latest --output /tmp/restore.dump
#
# Verifies the SHA-256 of the CIPHERTEXT before attempting decryption. If the
# bytes did not survive the round trip we want to say so plainly rather than
# hand a corrupt archive to pg_restore and let it fail somewhere in the middle
# of a table.
#
# Works against whichever provider is configured. restore.sh calls this when
# asked for a remote backup, so the restore path is provider-agnostic.

set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=scripts/lib/storage.sh
. "${SCRIPT_DIR}/lib/storage.sh"

BACKUP_DIR="${BACKUP_DIR:-/backups}"
DOWNLOAD_DIR="${BACKUP_DOWNLOAD_DIR:-${BACKUP_DIR}/restore}"

TIER="daily"
OUTPUT=""
TARGET=""

log() {
    printf '%s [download] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

fail() {
    log "ERROR: $*"
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --tier)   TIER="$2"; shift 2 ;;
        --output) OUTPUT="$2"; shift 2 ;;
        --help|-h)
            sed -n '2,20p' "$0"
            exit 0
            ;;
        *) TARGET="$1"; shift ;;
    esac
done

[ -n "${TARGET}" ] || fail "usage: backup-download.sh <latest|remote-key> [--tier daily|weekly|monthly] [--output path]"

storage_enabled || fail "off-site storage is not configured (BACKUP_REMOTE_PROVIDER=none)"
storage_init || fail "storage provider initialisation failed"

mkdir -p "${DOWNLOAD_DIR}"

# --------------------------------------------------------------------------
# Resolve "latest" to a concrete key.
#
# Keys embed a UTC timestamp in a sortable format (...-20260731T0200Z.dump.enc),
# so a reverse lexical sort is a reverse chronological sort. This is why the
# stamp format in backup.sh matters and must not be made friendlier.
# --------------------------------------------------------------------------
if [ "${TARGET}" = "latest" ]; then
    prefix="${STORAGE_PREFIX}/${TIER}/"
    log "resolving latest backup under ${prefix}"
    KEY="$(storage_list "${prefix}" | grep -v '\.sha256$' | sort -r | head -n 1 || true)"
    [ -n "${KEY}" ] || fail "no backups found in tier '${TIER}' at ${prefix}"
    log "latest is ${KEY}"
else
    case "${TARGET}" in
        "${STORAGE_PREFIX}"/*) KEY="${TARGET}" ;;
        *) KEY="${STORAGE_PREFIX}/${TARGET}" ;;
    esac
fi

BASE="$(basename "${KEY}")"
CIPHER="${DOWNLOAD_DIR}/${BASE}"

log "downloading ${KEY}"
storage_get "${KEY}" "${CIPHER}" || fail "download failed for ${KEY}"

[ -s "${CIPHER}" ] || fail "downloaded file is empty: ${CIPHER}"

# --------------------------------------------------------------------------
# Integrity check against the stored checksum.
# --------------------------------------------------------------------------
if storage_get "${KEY}.sha256" "${CIPHER}.sha256" 2>/dev/null; then
    expected="$(awk '{print $1}' < "${CIPHER}.sha256")"
    actual="$(sha256sum "${CIPHER}" | awk '{print $1}')"
    if [ "${expected}" != "${actual}" ]; then
        fail "checksum mismatch for ${KEY}: expected ${expected}, got ${actual}. The object is corrupt in remote storage -- try an older backup."
    fi
    log "checksum verified ${actual}"
else
    log "WARNING: no .sha256 companion for ${KEY}; integrity could not be verified"
fi

# --------------------------------------------------------------------------
# Decrypt. Mirror of backup-upload.sh: age first, then OpenSSL.
# --------------------------------------------------------------------------
PLAIN="${CIPHER}"

case "${BASE}" in
    *.enc)
        PLAIN="${DOWNLOAD_DIR}/$(basename "${BASE}" .enc)"
        identity="${BACKUP_AGE_IDENTITY_FILE:-}"
        passphrase="$(storage_read_secret BACKUP_ENCRYPTION_PASSPHRASE)"

        if [ -n "${identity}" ] && [ -f "${identity}" ] && command -v age >/dev/null 2>&1; then
            age --decrypt --identity "${identity}" --output "${PLAIN}" "${CIPHER}" \
                || fail "age decryption failed -- wrong identity file?"
            log "decrypted with age"
        elif [ -n "${passphrase}" ]; then
            passfile="$(mktemp)"
            printf '%s' "${passphrase}" > "${passfile}"
            if ! openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
                    -in "${CIPHER}" -out "${PLAIN}" -pass "file:${passfile}"; then
                rm -f "${passfile}"
                fail "openssl decryption failed -- wrong passphrase?"
            fi
            rm -f "${passfile}"
            log "decrypted with openssl"
        else
            fail "${BASE} is encrypted but no key is configured (set BACKUP_AGE_IDENTITY_FILE or BACKUP_ENCRYPTION_PASSPHRASE)"
        fi
        ;;
esac

case "${PLAIN}" in
    *.gz)
        gunzip -f "${PLAIN}"
        PLAIN="$(dirname "${PLAIN}")/$(basename "${PLAIN}" .gz)"
        log "decompressed"
        ;;
esac

# --------------------------------------------------------------------------
# Prove the decrypted result is a real archive before declaring success.
# --------------------------------------------------------------------------
if command -v pg_restore >/dev/null 2>&1; then
    pg_restore --list "${PLAIN}" >/dev/null 2>&1 \
        || fail "decrypted file is not a valid pg_dump archive: ${PLAIN}"
    log "archive structure verified"
fi

if [ -n "${OUTPUT}" ]; then
    mv "${PLAIN}" "${OUTPUT}"
    PLAIN="${OUTPUT}"
fi

log "ready: ${PLAIN}"
printf '%s\n' "${PLAIN}"
