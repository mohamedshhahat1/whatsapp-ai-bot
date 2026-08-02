#!/usr/bin/env sh
#
# Restore the database from a backup.
#
#   ./scripts/restore.sh --list
#   ./scripts/restore.sh --verify latest
#   ./scripts/restore.sh latest
#   ./scripts/restore.sh daily/whatsapp_ai_bot-20260731T0200Z.dump
#   ./scripts/restore.sh monthly/whatsapp_ai_bot-20260701T0200Z.dump
#
# Run from the repository root on the production host. It drives docker
# compose, so it must run on the host and not inside the backup container --
# it has to stop the app and worker, which cannot be done from within the
# stack it is stopping.
#
# See docs/BACKUP_RESTORE.md for the full recovery procedure.

set -eu

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
PGDATABASE="${PGDATABASE:-whatsapp_ai_bot}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"

compose() {
    docker compose -f "${COMPOSE_FILE}" "$@"
}

# Everything that touches the archives runs inside the backup container: it is
# the only place with the volume mounted and a matching pg_restore.
in_backup() {
    compose exec -T backup "$@"
}

log() {
    printf '%s [restore] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# --------------------------------------------------------------------------
# --list: what is available to restore from.
# --------------------------------------------------------------------------
if [ "${1:-}" = "--list" ]; then
    for tier in daily weekly monthly; do
        printf '\n%s:\n' "${tier}"
        in_backup sh -c "ls -1sht ${BACKUP_DIR}/${tier}/*.dump 2>/dev/null || echo '  (none)'" \
            | sed 's/^/  /'
    done
    printf '\nLast backup result:\n'
    in_backup sh -c "cat ${BACKUP_DIR}/state/last_result.json 2>/dev/null || echo '  (never run)'" \
        | sed 's/^/  /'
    printf '\nLast verified restore drill:\n'
    in_backup sh -c "cat ${BACKUP_DIR}/state/last_verified.json 2>/dev/null || echo '  (never run)'" \
        | sed 's/^/  /'
    exit 0
fi

# --------------------------------------------------------------------------
# --verify: rehearse into a scratch database, touching nothing real.
# --------------------------------------------------------------------------
if [ "${1:-}" = "--verify" ]; then
    shift
    exec in_backup /scripts/verify-restore.sh "${1:-latest}"
fi

case "${1:-}" in
    ""|-h|--help) usage 0 ;;
esac

ARCHIVE_REF="$1"

# --------------------------------------------------------------------------
# Resolve and check the archive BEFORE stopping anything. Discovering the
# filename was wrong after the app is already down turns a two-minute restore
# into a two-minute outage plus a panic.
# --------------------------------------------------------------------------
log "resolving ${ARCHIVE_REF}"

ARCHIVE="$(in_backup sh -c "
    case '${ARCHIVE_REF}' in
        latest) ls -1t ${BACKUP_DIR}/daily/*.dump 2>/dev/null | head -n 1 ;;
        /*)     echo '${ARCHIVE_REF}' ;;
        *)      echo '${BACKUP_DIR}/${ARCHIVE_REF}' ;;
    esac" | tr -d '\r')"

[ -n "${ARCHIVE}" ] || die "no backup matches '${ARCHIVE_REF}'"

in_backup test -f "${ARCHIVE}" || die "no such backup file: ${ARCHIVE}"

log "archive: ${ARCHIVE}"

log "checking the archive checksum"
in_backup sh -c "
    cd \"\$(dirname '${ARCHIVE}')\" || exit 1
    if [ -f \"\$(basename '${ARCHIVE}').sha256\" ]; then
        sha256sum -c \"\$(basename '${ARCHIVE}').sha256\"
    else
        echo 'WARNING: no checksum file alongside this archive'
    fi" || die "checksum mismatch -- refusing to restore a corrupted archive"

log "checking the archive parses"
in_backup pg_restore --list "${ARCHIVE}" > /dev/null \
    || die "pg_restore cannot read this archive"

# --------------------------------------------------------------------------
# Confirmation. Typing the database name is the only prompt that does not
# become reflex -- a y/N is answered without reading by the third restore.
# --------------------------------------------------------------------------
cat <<EOF

  ============================================================
  THIS WILL DESTROY THE CURRENT CONTENTS OF '${PGDATABASE}'
  ============================================================

  Restoring : ${ARCHIVE}
  Into      : ${PGDATABASE}

  Every conversation, message and customer written since that
  backup was taken will be gone. A safety dump of the current
  state is taken first, so this is reversible -- but only if
  the safety dump succeeds.

EOF

if [ "${ASSUME_YES:-0}" != "1" ]; then
    printf 'Type the database name to continue: '
    read -r answer
    [ "${answer}" = "${PGDATABASE}" ] || die "aborted (got '${answer}')"
fi

# --------------------------------------------------------------------------
# Stop the writers. Restoring underneath a running app leaves rows written
# between the drop and the load, referencing ids that the restore then
# renumbers -- the database comes back internally inconsistent.
#
# nginx stays up so customers get a clean 502 rather than a connection reset,
# and Meta's webhook retries redeliver whatever arrives during the window.
# --------------------------------------------------------------------------
log "stopping app and worker"
compose stop app worker

restore_failed=0

# --------------------------------------------------------------------------
# Safety dump of the CURRENT state, before it is destroyed.
# --------------------------------------------------------------------------
SAFETY="${BACKUP_DIR}/pre-restore/${PGDATABASE}-before-restore-$(date -u +%Y%m%dT%H%M%SZ).dump"
log "taking a safety dump of the current database: ${SAFETY}"

if in_backup sh -c "
    mkdir -p ${BACKUP_DIR}/pre-restore &&
    export PGPASSWORD=\"\$(cat \${PGPASSWORD_FILE})\" &&
    pg_dump --format=custom --compress=9 --no-owner --no-privileges \
        --file='${SAFETY}' '${PGDATABASE}'"; then
    log "safety dump written"
else
    log "WARNING: the safety dump FAILED"
    if [ "${ASSUME_YES:-0}" != "1" ]; then
        printf 'Continue without a safety dump? This is NOT reversible. [type: yes] '
        read -r answer
        if [ "${answer}" != "yes" ]; then
            compose start app worker
            die "aborted -- app and worker restarted, nothing was changed"
        fi
    fi
fi

# --------------------------------------------------------------------------
# The restore itself.
#
# --clean --if-exists drops each object before recreating it, so this works
# against a populated database without needing to drop the whole thing (which
# would fail anyway while any connection is still open).
#
# --single-transaction makes it all-or-nothing: a failure halfway through
# rolls back to the pre-restore state rather than leaving half a schema.
# --------------------------------------------------------------------------
log "restoring -- do not interrupt"

if in_backup sh -c "
    export PGPASSWORD=\"\$(cat \${PGPASSWORD_FILE})\" &&
    psql -d '${PGDATABASE}' -q -c 'CREATE EXTENSION IF NOT EXISTS vector;' &&
    pg_restore \
        --dbname='${PGDATABASE}' \
        --clean --if-exists \
        --no-owner --no-privileges \
        --single-transaction \
        --exit-on-error \
        '${ARCHIVE}'"; then
    log "restore completed"
else
    log "RESTORE FAILED -- the database was rolled back to its pre-restore state"
    restore_failed=1
fi

# --------------------------------------------------------------------------
# Post-restore verification.
# --------------------------------------------------------------------------
if [ "${restore_failed}" -eq 0 ]; then
    log "verifying the restored database"

    in_backup sh -c "
        export PGPASSWORD=\"\$(cat \${PGPASSWORD_FILE})\" &&
        psql -d '${PGDATABASE}' -tAc \"
            SELECT 'users=' || (SELECT count(*) FROM users)
                || ' conversations=' || (SELECT count(*) FROM conversations)
                || ' messages=' || (SELECT count(*) FROM messages)
                || ' chunks=' || (SELECT count(*) FROM document_chunks)
                || ' revision=' || (SELECT version_num FROM alembic_version);\"" \
        | sed 's/^/    /'

    # The dump carries whatever schema revision it was taken at. If the running
    # image is newer, the app will start against a schema it does not expect --
    # applying the outstanding migrations is what closes that gap.
    log "applying any migrations newer than the restored dump"
    compose run --rm migrate || log "WARNING: migrations failed, check the schema by hand"
fi

log "starting app and worker"
compose start app worker

log "waiting for the app to report ready"
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if compose exec -T app python -c "
import sys, urllib.request
try:
    urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
        log "app is ready"
        break
    fi
    sleep 5
done

if [ "${restore_failed}" -ne 0 ]; then
    die "restore failed -- the database is unchanged, the stack is back up"
fi

cat <<EOF

==> Restore complete.

The pre-restore safety dump is at:
  ${SAFETY}

If this restore was a mistake, undo it with:
  ./scripts/restore.sh ${SAFETY}

It lives outside the daily/weekly/monthly tiers, so rotation will not prune
it. Delete it by hand once you are satisfied.
EOF
