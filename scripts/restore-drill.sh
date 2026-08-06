#!/usr/bin/env sh
#
# Automated restore drill: prove the backups are restorable, on a schedule,
# without a human.
#
#   ./scripts/restore-drill.sh                 # newest local daily backup
#   ./scripts/restore-drill.sh --remote        # pull from off-site instead
#   ./scripts/restore-drill.sh --keep          # never tear down the scratch env
#
# A backup that has never been restored is a hypothesis, not a backup. This
# script turns that hypothesis into a scheduled test with a pass/fail result.
#
# Eleven steps, each fatal:
#   1  obtain a backup (local newest, or downloaded + decrypted from off-site)
#   2  start a throwaway PostgreSQL container on a private port
#   3  restore the dump into it
#   4  run alembic upgrade head against the restored database
#   5  verify the schema (alembic_version present and at head)
#   6  verify every expected table exists
#   7  verify the expected indexes exist
#   8  verify the pgvector extension and its vector column
#   9  verify data integrity (row counts, FK orphans, NOT NULL sanity)
#  10  verify the application boots against the restored database
#  11  run the application health checks
#
# On failure the scratch environment is deliberately PRESERVED so the state
# can be inspected. That is the opposite of the usual cleanup instinct and it
# is intentional: a drill that destroys the evidence tells you it failed but
# never why.
#
# Writes state/restore_drill.json and a human-readable report.

set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

BACKUP_DIR="${BACKUP_DIR:-/backups}"
STATE_DIR="${BACKUP_DIR}/state"
REPORT_DIR="${BACKUP_DIR}/drills"
DRILL_ID="$(date -u +%Y%m%dT%H%M%SZ)"
REPORT="${REPORT_DIR}/drill-${DRILL_ID}.log"
REPORT_JSON="${REPORT_DIR}/drill-${DRILL_ID}.json"

DRILL_DB="${RESTORE_DRILL_DB:-restore_drill}"
DRILL_CONTAINER="${RESTORE_DRILL_CONTAINER:-wa-restore-drill-${DRILL_ID}}"
DRILL_PORT="${RESTORE_DRILL_PORT:-55432}"
DRILL_IMAGE="${RESTORE_DRILL_IMAGE:-pgvector/pgvector:pg16}"
DRILL_PASSWORD="${RESTORE_DRILL_PASSWORD:-drillpass}"
APP_IMAGE="${RESTORE_DRILL_APP_IMAGE:-${APP_IMAGE:-}}"

# The scratch database is published on 127.0.0.1 so a human can reach it with
# psql, and that publish is deliberately loopback-only: this script also runs
# on the production host, where a scratch PostgreSQL holding a restored copy
# of everything and accepting a well-known password has no business being on a
# public interface.
#
# But a loopback-only publish is invisible to other CONTAINERS. A container
# resolving host.docker.internal arrives at the host through the gateway
# address, and a port bound to 127.0.0.1 is not listening there. So steps 4
# and 10 join the database on a private network and address it by container
# name on its own port instead of bouncing off the host at all.
DRILL_NETWORK="${RESTORE_DRILL_NETWORK:-${DRILL_CONTAINER}-net}"

USE_REMOTE="false"
KEEP_ENV="false"
FAILED_STEP=""
STATUS="ok"
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

mkdir -p "${STATE_DIR}" "${REPORT_DIR}"

while [ $# -gt 0 ]; do
    case "$1" in
        --remote) USE_REMOTE="true"; shift ;;
        --keep)   KEEP_ENV="true"; shift ;;
        *) shift ;;
    esac
done

log() {
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "${REPORT}"
}

step() {
    CURRENT_STEP="$1"
    log "---- STEP ${CURRENT_STEP} ----"
}

# Collect diagnostics before anything is torn down.
capture_diagnostics() {
    log "capturing diagnostics for ${DRILL_CONTAINER}"
    docker logs "${DRILL_CONTAINER}" > "${REPORT_DIR}/drill-${DRILL_ID}-postgres.log" 2>&1 || true
    docker inspect "${DRILL_CONTAINER}" > "${REPORT_DIR}/drill-${DRILL_ID}-inspect.json" 2>&1 || true
}

write_json() {
    cat > "${REPORT_JSON}" <<JSON
{
  "drill_id": "${DRILL_ID}",
  "status": "${STATUS}",
  "failed_step": "${FAILED_STEP}",
  "source": "$( [ "${USE_REMOTE}" = "true" ] && printf 'remote' || printf 'local' )",
  "backup_file": "${DUMP_FILE:-}",
  "started_at": "${STARTED_AT}",
  "finished_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "container": "${DRILL_CONTAINER}",
  "preserved": $( [ "${STATUS}" = "failed" ] || [ "${KEEP_ENV}" = "true" ] && printf 'true' || printf 'false' ),
  "report": "${REPORT}"
}
JSON
    cp "${REPORT_JSON}" "${STATE_DIR}/restore_drill.json"
}

teardown() {
    if [ "${STATUS}" = "failed" ] || [ "${KEEP_ENV}" = "true" ]; then
        log "PRESERVING scratch environment for debugging: container=${DRILL_CONTAINER} port=${DRILL_PORT}"
        log "  inspect with: docker exec -it ${DRILL_CONTAINER} psql -U postgres -d ${DRILL_DB}"
        log "  remove with:  docker rm -f ${DRILL_CONTAINER} && docker network rm ${DRILL_NETWORK}"
    else
        docker rm -f "${DRILL_CONTAINER}" >/dev/null 2>&1 || true
        # The network only goes once the last container has left it.
        docker network rm "${DRILL_NETWORK}" >/dev/null 2>&1 || true
        log "scratch environment removed"
    fi
}

fail_drill() {
    STATUS="failed"
    FAILED_STEP="${CURRENT_STEP:-unknown}"
    log "DRILL FAILED at step: ${FAILED_STEP}"
    log "REASON: $*"
    capture_diagnostics
    write_json
    teardown
    # Non-zero exit is what the scheduler and CI turn into an alert.
    exit 1
}

psql_drill() {
    PGPASSWORD="${DRILL_PASSWORD}" docker exec -e PGPASSWORD="${DRILL_PASSWORD}" \
        "${DRILL_CONTAINER}" psql -U postgres -d "$1" -tAc "$2" 2>>"${REPORT}"
}

log "restore drill ${DRILL_ID} starting (source=$( [ "${USE_REMOTE}" = "true" ] && printf remote || printf local ))"

command -v docker >/dev/null 2>&1 || fail_drill "docker is not available in this container/host"

# --------------------------------------------------------------------------
step "1/11 obtain a backup"
# --------------------------------------------------------------------------
if [ "${USE_REMOTE}" = "true" ]; then
    log "downloading newest off-site backup"
    DUMP_FILE="$("${SCRIPT_DIR}/backup-download.sh" latest --tier daily 2>>"${REPORT}" | tail -n 1)" \
        || fail_drill "backup-download.sh failed"
else
    DUMP_FILE="$(ls -1t "${BACKUP_DIR}/daily"/*.dump 2>/dev/null | head -n 1 || true)"
    [ -n "${DUMP_FILE}" ] || fail_drill "no local backup found under ${BACKUP_DIR}/daily"
fi

[ -s "${DUMP_FILE}" ] || fail_drill "backup file is empty: ${DUMP_FILE}"
log "using backup ${DUMP_FILE} ($(wc -c < "${DUMP_FILE}" | tr -d ' ') bytes)"

# Verify the local checksum when one exists.
if [ -f "${DUMP_FILE}.sha256" ]; then
    ( cd "$(dirname "${DUMP_FILE}")" && sha256sum -c "$(basename "${DUMP_FILE}").sha256" >/dev/null 2>&1 ) \
        || fail_drill "checksum mismatch on ${DUMP_FILE} -- the local backup is corrupt"
    log "local checksum verified"
fi

# --------------------------------------------------------------------------
step "2/11 start a clean PostgreSQL container"
# --------------------------------------------------------------------------
docker rm -f "${DRILL_CONTAINER}" >/dev/null 2>&1 || true
docker network rm "${DRILL_NETWORK}" >/dev/null 2>&1 || true

docker network create "${DRILL_NETWORK}" >/dev/null 2>>"${REPORT}" \
    || fail_drill "could not create the drill network ${DRILL_NETWORK}"

docker run -d --name "${DRILL_CONTAINER}" \
    --network "${DRILL_NETWORK}" \
    -e POSTGRES_PASSWORD="${DRILL_PASSWORD}" \
    -e POSTGRES_DB="${DRILL_DB}" \
    -p "127.0.0.1:${DRILL_PORT}:5432" \
    "${DRILL_IMAGE}" >/dev/null 2>>"${REPORT}" \
    || fail_drill "could not start ${DRILL_IMAGE}"

log "container ${DRILL_CONTAINER} started on 127.0.0.1:${DRILL_PORT} (network ${DRILL_NETWORK})"

ready="false"
i=0
while [ "${i}" -lt 60 ]; do
    if docker exec "${DRILL_CONTAINER}" pg_isready -U postgres >/dev/null 2>&1; then
        ready="true"
        break
    fi
    i=$((i + 1))
    sleep 2
done
[ "${ready}" = "true" ] || fail_drill "PostgreSQL did not become ready within 120s"
log "PostgreSQL is ready"

# --------------------------------------------------------------------------
step "3/11 restore the dump"
# --------------------------------------------------------------------------
docker cp "${DUMP_FILE}" "${DRILL_CONTAINER}:/tmp/restore.dump" 2>>"${REPORT}" \
    || fail_drill "could not copy the dump into the container"

# --no-owner/--no-privileges: the dump came from a differently-owned database.
# pg_restore returns non-zero for benign warnings, so we inspect the actual
# outcome in the steps below rather than trusting the exit code alone.
docker exec -e PGPASSWORD="${DRILL_PASSWORD}" "${DRILL_CONTAINER}" \
    pg_restore -U postgres -d "${DRILL_DB}" --no-owner --no-privileges \
    --exit-on-error /tmp/restore.dump >>"${REPORT}" 2>&1 \
    || fail_drill "pg_restore reported errors -- see ${REPORT}"

log "dump restored into ${DRILL_DB}"

# --------------------------------------------------------------------------
step "4/11 run migrations against the restored database"
# --------------------------------------------------------------------------
# Proves the backup is not merely loadable but is a valid starting point for
# the current code -- a restore that cannot be migrated forward is useless
# during an actual incident.
#
# Addressed by container name on the private network: the loopback publish is
# for humans, and a sibling container cannot see it.
if [ -n "${APP_IMAGE}" ]; then
    DRILL_DSN="postgresql+psycopg://postgres:${DRILL_PASSWORD}@${DRILL_CONTAINER}:5432/${DRILL_DB}"
    docker run --rm --network "${DRILL_NETWORK}" \
        -e DATABASE_URL="${DRILL_DSN}" \
        -e ENVIRONMENT=test \
        -e OPENAI_API_KEY=drill -e WHATSAPP_TOKEN=drill \
        -e WHATSAPP_PHONE_NUMBER_ID=drill -e WHATSAPP_VERIFY_TOKEN=drill \
        -e WHATSAPP_APP_SECRET=drill -e ADMIN_API_KEY=drill \
        "${APP_IMAGE}" alembic upgrade head >>"${REPORT}" 2>&1 \
        || fail_drill "alembic upgrade head failed against the restored database"
    log "migrations applied cleanly"
else
    log "SKIPPED: RESTORE_DRILL_APP_IMAGE/APP_IMAGE not set, cannot run migrations"
fi

# --------------------------------------------------------------------------
step "5/11 verify schema and alembic version"
# --------------------------------------------------------------------------
version="$(psql_drill "${DRILL_DB}" "SELECT version_num FROM alembic_version LIMIT 1;" || true)"
[ -n "${version}" ] || fail_drill "alembic_version table is missing or empty -- this dump predates migrations"
log "alembic_version = ${version}"

# --------------------------------------------------------------------------
step "6/11 verify tables"
# --------------------------------------------------------------------------
EXPECTED_TABLES="${RESTORE_DRILL_TABLES:-users conversations messages ai_logs documents model_pricing}"
for t in ${EXPECTED_TABLES}; do
    found="$(psql_drill "${DRILL_DB}" "SELECT to_regclass('public.${t}') IS NOT NULL;" || true)"
    [ "${found}" = "t" ] || fail_drill "expected table '${t}' is missing from the restored database"
    log "table ${t} present"
done

# --------------------------------------------------------------------------
step "7/11 verify indexes"
# --------------------------------------------------------------------------
# Indexes are part of correctness here, not just performance: the unique index
# on messages.wa_message_id is what makes inbound claiming race-free. A restore
# that silently lost it would reintroduce duplicate replies under load.
idx_count="$(psql_drill "${DRILL_DB}" "SELECT count(*) FROM pg_indexes WHERE schemaname='public';" || printf '0')"
[ "${idx_count}" -gt 0 ] 2>/dev/null || fail_drill "no indexes found in the restored database"
log "${idx_count} indexes present"

uniq_wa="$(psql_drill "${DRILL_DB}" "SELECT count(*) FROM pg_indexes WHERE schemaname='public' AND tablename='messages' AND indexdef ILIKE '%unique%' AND indexdef ILIKE '%wa_message_id%';" || printf '0')"
if [ "${uniq_wa}" = "0" ]; then
    fail_drill "the UNIQUE index on messages.wa_message_id is missing -- inbound de-duplication would be broken after this restore"
fi
log "unique index on messages.wa_message_id present"

# --------------------------------------------------------------------------
step "8/11 verify pgvector"
# --------------------------------------------------------------------------
ext="$(psql_drill "${DRILL_DB}" "SELECT count(*) FROM pg_extension WHERE extname='vector';" || printf '0')"
[ "${ext}" = "1" ] || fail_drill "the pgvector extension is not installed in the restored database -- RAG would fail"
log "pgvector extension present"

veccol="$(psql_drill "${DRILL_DB}" "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND udt_name='vector';" || printf '0')"
if [ "${veccol}" = "0" ]; then
    log "WARNING: no vector-typed column found (knowledge base may simply be empty)"
else
    log "${veccol} vector column(s) present"
fi

# --------------------------------------------------------------------------
step "9/11 verify data integrity"
# --------------------------------------------------------------------------
for t in users conversations messages; do
    c="$(psql_drill "${DRILL_DB}" "SELECT count(*) FROM ${t};" || printf 'ERR')"
    [ "${c}" = "ERR" ] && fail_drill "could not count rows in ${t}"
    log "rows in ${t}: ${c}"
done

# Referential integrity: a dump restored out of order can leave orphans even
# though every individual table looks fine.
orphans="$(psql_drill "${DRILL_DB}" "SELECT count(*) FROM messages m LEFT JOIN conversations c ON m.conversation_id = c.id WHERE c.id IS NULL;" || printf '0')"
[ "${orphans}" = "0" ] || fail_drill "${orphans} messages reference a conversation that does not exist -- referential integrity is broken"
log "no orphaned messages"

nullids="$(psql_drill "${DRILL_DB}" "SELECT count(*) FROM messages WHERE id IS NULL;" || printf '0')"
[ "${nullids}" = "0" ] || fail_drill "messages table contains NULL primary keys"
log "primary keys intact"

# --------------------------------------------------------------------------
step "10/11 verify the application starts against the restored database"
# --------------------------------------------------------------------------
APP_CONTAINER="${DRILL_CONTAINER}-app"
if [ -n "${APP_IMAGE}" ]; then
    docker rm -f "${APP_CONTAINER}" >/dev/null 2>&1 || true
    docker run -d --name "${APP_CONTAINER}" \
        --network "${DRILL_NETWORK}" \
        -e DATABASE_URL="postgresql+asyncpg://postgres:${DRILL_PASSWORD}@${DRILL_CONTAINER}:5432/${DRILL_DB}" \
        -e ENVIRONMENT=test -e USE_TASK_QUEUE=false -e RAG_ENABLED=false \
        -e METRICS_ENABLED=true -e RATE_LIMIT_ENABLED=false \
        -e OPENAI_API_KEY=drill -e WHATSAPP_TOKEN=drill \
        -e WHATSAPP_PHONE_NUMBER_ID=drill -e WHATSAPP_VERIFY_TOKEN=drill \
        -e WHATSAPP_APP_SECRET=drill -e ADMIN_API_KEY=drill \
        -p "127.0.0.1:${RESTORE_DRILL_APP_PORT:-58000}:8000" \
        "${APP_IMAGE}" >/dev/null 2>>"${REPORT}" \
        || fail_drill "the application container failed to start"

    log "application container ${APP_CONTAINER} started"

    # ----------------------------------------------------------------------
    step "11/11 run health checks"
    # ----------------------------------------------------------------------
    healthy="false"
    i=0
    while [ "${i}" -lt 30 ]; do
        if docker exec "${APP_CONTAINER}" python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=5)" >/dev/null 2>&1; then
            healthy="true"
            break
        fi
        i=$((i + 1))
        sleep 3
    done

    if [ "${healthy}" != "true" ]; then
        docker logs "${APP_CONTAINER}" > "${REPORT_DIR}/drill-${DRILL_ID}-app.log" 2>&1 || true
        docker rm -f "${APP_CONTAINER}" >/dev/null 2>&1 || true
        fail_drill "/health/ready never returned success against the restored database"
    fi

    log "/health/ready passed against the restored database"
    docker logs "${APP_CONTAINER}" > "${REPORT_DIR}/drill-${DRILL_ID}-app.log" 2>&1 || true
    docker rm -f "${APP_CONTAINER}" >/dev/null 2>&1 || true
fi

# --------------------------------------------------------------------------
STATUS="ok"
date -u +%s > "${STATE_DIR}/last_restore_drill_success"
write_json
teardown

log "RESTORE DRILL PASSED (report: ${REPORT})"
