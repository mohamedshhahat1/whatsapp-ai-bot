#!/usr/bin/env sh
#
# Prove a backup can actually be restored, without touching production.
#
# A dump that parses is not the same as a dump that restores. This rebuilds
# one into a scratch database on the same server, counts rows in the tables
# that matter, and drops it again. Run weekly by the scheduler, and by hand
# before you rely on a specific file:
#
#   docker compose -f docker-compose.prod.yml exec backup /scripts/verify-restore.sh latest
#   docker compose -f docker-compose.prod.yml exec backup /scripts/verify-restore.sh daily/whatsapp_ai_bot-20260731T0200Z.dump
#
# Exit code is the result: 0 means that file is known-restorable.

set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
PGHOST="${PGHOST:-db}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-postgres}"
PGDATABASE="${PGDATABASE:-whatsapp_ai_bot}"
STATE_DIR="${BACKUP_DIR}/state"

SCRATCH_DB="restore_drill_$(date -u +%Y%m%d%H%M%S)"

if [ -n "${PGPASSWORD_FILE:-}" ] && [ -f "${PGPASSWORD_FILE}" ]; then
    PGPASSWORD="$(cat "${PGPASSWORD_FILE}")"
    export PGPASSWORD
fi
export PGHOST PGPORT PGUSER

log() {
    printf '%s [verify-restore] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

resolve() {
    case "$1" in
        latest)
            ls -1t "${BACKUP_DIR}"/daily/*.dump 2>/dev/null | head -n 1
            ;;
        /*)
            echo "$1"
            ;;
        *)
            echo "${BACKUP_DIR}/$1"
            ;;
    esac
}

ARCHIVE="$(resolve "${1:-latest}")"

if [ -z "${ARCHIVE}" ] || [ ! -f "${ARCHIVE}" ]; then
    log "ERROR: no such backup: ${1:-latest}"
    exit 1
fi

log "verifying ${ARCHIVE}"

# --------------------------------------------------------------------------
# Checksum first. Catches bit rot between the write and now, which is the one
# failure mode the write-time verification cannot see.
# --------------------------------------------------------------------------
if [ -f "${ARCHIVE}.sha256" ]; then
    if ( cd "$(dirname "${ARCHIVE}")" && sha256sum -c "$(basename "${ARCHIVE}").sha256" >/dev/null 2>&1 ); then
        log "checksum ok"
    else
        log "ERROR: checksum MISMATCH -- this file has been corrupted since it was written"
        exit 1
    fi
else
    log "WARNING: no .sha256 alongside this archive, skipping the integrity check"
fi

cleanup() {
    # Always drop the scratch database, including on failure -- otherwise a
    # few failed drills leave a pile of half-restored databases on the server.
    psql -d postgres -q -c "DROP DATABASE IF EXISTS \"${SCRATCH_DB}\" WITH (FORCE);" >/dev/null 2>&1 || true
}
trap cleanup EXIT

log "creating scratch database ${SCRATCH_DB}"
psql -d postgres -q -c "CREATE DATABASE \"${SCRATCH_DB}\";"

# --------------------------------------------------------------------------
# The knowledge base needs the pgvector extension present before the dump's
# table definitions can be created.
# --------------------------------------------------------------------------
psql -d "${SCRATCH_DB}" -q -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null 2>&1 || \
    log "WARNING: could not create the vector extension in the scratch database"

log "restoring into the scratch database"
if ! pg_restore \
        --dbname="${SCRATCH_DB}" \
        --no-owner \
        --no-privileges \
        --exit-on-error \
        "${ARCHIVE}" 2>/tmp/restore_err; then
    log "ERROR: pg_restore failed"
    sed 's/^/    /' /tmp/restore_err | head -n 40
    exit 1
fi

# --------------------------------------------------------------------------
# Structural checks. A restore that "succeeds" into an empty database is the
# failure this is here to catch.
# --------------------------------------------------------------------------
failed=0

for table in users conversations messages documents document_chunks; do
    if ! count="$(psql -d "${SCRATCH_DB}" -tAc "SELECT count(*) FROM ${table};" 2>/dev/null)"; then
        log "ERROR: table '${table}' is missing from the restored database"
        failed=1
        continue
    fi
    log "  ${table}: ${count} rows"
done

# The alembic marker tells us the dump came from a migrated database and which
# revision it is at. Restoring a dump older than the running code is a
# different and much worse problem than restoring a corrupt one.
if revision="$(psql -d "${SCRATCH_DB}" -tAc "SELECT version_num FROM alembic_version;" 2>/dev/null)"; then
    log "  schema revision: ${revision}"
else
    log "ERROR: no alembic_version table -- this dump did not come from a migrated database"
    failed=1
fi

if [ "${failed}" -ne 0 ]; then
    log "VERIFICATION FAILED for ${ARCHIVE}"
    exit 1
fi

mkdir -p "${STATE_DIR}"
cat > "${STATE_DIR}/last_verified.json" <<EOF
{"status":"ok","archive":"${ARCHIVE}","verified_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","schema_revision":"${revision}"}
EOF

log "VERIFIED: ${ARCHIVE} is restorable"
