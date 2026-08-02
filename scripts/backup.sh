#!/usr/bin/env sh
#
# Take one verified PostgreSQL backup, replicate it off-site, and rotate the
# old ones.
#
# Runs inside the `backup` service in docker-compose.prod.yml (a postgres:16
# image, so pg_dump matches the server major version -- a newer server dumped
# by an older pg_dump fails outright, which is why the image is pinned).
#
# POSIX sh, not bash: the postgres image ships no bash-only guarantees worth
# relying on and this script has no need of arrays.
#
# Layout under ${BACKUP_DIR}:
#
#   daily/whatsapp_ai_bot-20260731T0200Z.dump
#   daily/whatsapp_ai_bot-20260731T0200Z.dump.sha256
#   weekly/...     hard links to the daily file, pruned on their own schedule
#   monthly/...
#   state/last_success        unix timestamp, read by the healthcheck
#   state/last_result.json    machine-readable outcome of the last run
#   state/backup.log
#   metrics/backup.prom       Prometheus textfile collector output

set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

BACKUP_DIR="${BACKUP_DIR:-/backups}"
PGHOST="${PGHOST:-db}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-postgres}"
PGDATABASE="${PGDATABASE:-whatsapp_ai_bot}"

# Retention, in units of each tier.
RETAIN_DAILY="${RETAIN_DAILY:-14}"
RETAIN_WEEKLY="${RETAIN_WEEKLY:-8}"
RETAIN_MONTHLY="${RETAIN_MONTHLY:-12}"

# Tables that must be present in a dump for it to count as valid. A dump of
# the wrong database, or one taken before the migrations ran, parses fine and
# is worthless -- this is what distinguishes the two.
REQUIRED_TABLES="users conversations messages"

STATE_DIR="${BACKUP_DIR}/state"
LOG_FILE="${STATE_DIR}/backup.log"

mkdir -p "${BACKUP_DIR}/daily" "${BACKUP_DIR}/weekly" "${BACKUP_DIR}/monthly" "${STATE_DIR}"

log() {
    # Timestamped to both stdout (docker logs) and the log file (survives a
    # container restart, which is when you most want to read it).
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "${LOG_FILE}"
}

# Refresh Prometheus metrics from whatever state currently exists. Safe to
# call on both the success and failure paths.
publish_metrics() {
    if [ -x "${SCRIPT_DIR}/backup-metrics.sh" ]; then
        "${SCRIPT_DIR}/backup-metrics.sh" || log "WARNING: could not write backup metrics"
    fi
}

fail() {
    log "ERROR: $*"
    cat > "${STATE_DIR}/last_result.json" <<EOF
{"status":"failed","finished_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","error":"$*"}
EOF
    # Deliberately does NOT touch last_success. The healthcheck goes unhealthy
    # once the newest success ages past BACKUP_MAX_AGE_HOURS, which is the
    # behaviour we want: one transient failure is noise, a day of them is an
    # incident.
    publish_metrics
    exit 1
}

# --------------------------------------------------------------------------
# Credentials. The compose file mounts the same Docker secret the database
# itself uses, so the password is never in an environment variable where
# `docker inspect` would print it.
# --------------------------------------------------------------------------
if [ -n "${PGPASSWORD_FILE:-}" ] && [ -f "${PGPASSWORD_FILE}" ]; then
    PGPASSWORD="$(cat "${PGPASSWORD_FILE}")"
    export PGPASSWORD
fi
export PGHOST PGPORT PGUSER PGDATABASE

STAMP="$(date -u +%Y%m%dT%H%MZ)"
DAY_OF_WEEK="$(date -u +%u)"   # 1=Monday .. 7=Sunday
DAY_OF_MONTH="$(date -u +%d)"

DAILY_DIR="${BACKUP_DIR}/daily"
TARGET="${DAILY_DIR}/${PGDATABASE}-${STAMP}.dump"
TMP="${TARGET}.partial"

log "backup starting database=${PGDATABASE} host=${PGHOST} target=${TARGET}"

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# --------------------------------------------------------------------------
# Dump.
#
# Written to .partial first and renamed only after it verifies. A crash or a
# full disk mid-dump would otherwise leave a truncated file sitting in daily/
# looking exactly like a real backup, and `restore.sh latest` would pick it.
#
# -Fc is the custom format: compressed, selectively restorable, and listable.
# -Z 9 trades CPU for space; the dump runs at 02:00 against a database that is
# not under load, so the CPU is free.
# --no-owner / --no-privileges keep the dump restorable into a database owned
# by a different role, which is what the scratch-database verification does.
# --------------------------------------------------------------------------
if ! pg_dump \
        --format=custom \
        --compress=9 \
        --no-owner \
        --no-privileges \
        --file="${TMP}" \
        "${PGDATABASE}"; then
    rm -f "${TMP}"
    fail "pg_dump failed"
fi

size_bytes="$(wc -c < "${TMP}" | tr -d ' ')"

# --------------------------------------------------------------------------
# Verify before trusting it.
# --------------------------------------------------------------------------
log "verifying archive"

listing="$(pg_restore --list "${TMP}" 2>/dev/null)" || {
    rm -f "${TMP}"
    fail "pg_restore --list could not parse the archive (corrupt dump)"
}

for table in ${REQUIRED_TABLES}; do
    if ! printf '%s' "${listing}" | grep -q "TABLE DATA public ${table}"; then
        rm -f "${TMP}"
        fail "archive is missing table '${table}' -- wrong database or migrations not applied"
    fi
done

# A dump smaller than this is structurally valid but almost certainly empty.
MIN_BYTES="${BACKUP_MIN_BYTES:-2048}"
if [ "${size_bytes}" -lt "${MIN_BYTES}" ]; then
    rm -f "${TMP}"
    fail "archive is only ${size_bytes} bytes, below the ${MIN_BYTES} byte floor"
fi

mv "${TMP}" "${TARGET}"

# Checksum for detecting bit rot later. Verified by restore.sh before it will
# restore anything.
( cd "${DAILY_DIR}" && sha256sum "$(basename "${TARGET}")" > "$(basename "${TARGET}").sha256" )

log "backup verified size=${size_bytes} bytes"

# --------------------------------------------------------------------------
# Promote into the weekly and monthly tiers.
#
# Hard links, not copies: the same bytes on disk appear in each tier and each
# tier prunes on its own clock. The inode survives until the last link to it
# is removed, so pruning daily/ does not take the monthly copy with it.
# --------------------------------------------------------------------------
promote() {
    tier_dir="${BACKUP_DIR}/$1"
    base="$(basename "${TARGET}")"
    if ln "${TARGET}" "${tier_dir}/${base}" 2>/dev/null; then
        cp "${TARGET}.sha256" "${tier_dir}/${base}.sha256"
        log "promoted to $1"
    else
        # Filesystems that refuse hard links (some bind mounts, some network
        # storage) fall back to a real copy rather than silently skipping a
        # retention tier.
        cp "${TARGET}" "${tier_dir}/${base}"
        cp "${TARGET}.sha256" "${tier_dir}/${base}.sha256"
        log "promoted to $1 (copied -- hard links unsupported here)"
    fi
}

PROMOTED_WEEKLY="false"
PROMOTED_MONTHLY="false"

if [ "${DAY_OF_WEEK}" = "7" ]; then
    promote weekly
    PROMOTED_WEEKLY="true"
fi

if [ "${DAY_OF_MONTH}" = "01" ]; then
    promote monthly
    PROMOTED_MONTHLY="true"
fi

# --------------------------------------------------------------------------
# Rotation. Prune by count, not by age: an age rule silently empties the
# directory if the scheduler has been down, and the whole point of retention
# is to still have something after a bad week.
# --------------------------------------------------------------------------
prune() {
    tier_dir="${BACKUP_DIR}/$1"
    keep="$2"
    # Newest first; everything past the keep count goes, with its checksum.
    ls -1t "${tier_dir}"/*.dump 2>/dev/null | tail -n "+$((keep + 1))" | while read -r old; do
        rm -f "${old}" "${old}.sha256"
        log "pruned $1/$(basename "${old}")"
    done
}

prune daily "${RETAIN_DAILY}"
prune weekly "${RETAIN_WEEKLY}"
prune monthly "${RETAIN_MONTHLY}"

# --------------------------------------------------------------------------
# Publish the outcome for the healthcheck and for the admin dashboard.
# --------------------------------------------------------------------------
date -u +%s > "${STATE_DIR}/last_success"

daily_count="$(ls -1 "${BACKUP_DIR}/daily"/*.dump 2>/dev/null | wc -l | tr -d ' ')"
weekly_count="$(ls -1 "${BACKUP_DIR}/weekly"/*.dump 2>/dev/null | wc -l | tr -d ' ')"
monthly_count="$(ls -1 "${BACKUP_DIR}/monthly"/*.dump 2>/dev/null | wc -l | tr -d ' ')"

cat > "${STATE_DIR}/last_result.json" <<EOF
{
  "status": "ok",
  "database": "${PGDATABASE}",
  "started_at": "${started_at}",
  "finished_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "file": "daily/$(basename "${TARGET}")",
  "size_bytes": ${size_bytes},
  "counts": {
    "daily": ${daily_count},
    "weekly": ${weekly_count},
    "monthly": ${monthly_count}
  }
}
EOF

log "backup complete daily=${daily_count} weekly=${weekly_count} monthly=${monthly_count}"

# --------------------------------------------------------------------------
# Off-site replication.
#
# Runs AFTER last_success is written, and its failure does not fail this
# script. That split is deliberate: a local dump that verified IS a real
# backup even when the network is down. Treating an upload failure as a backup
# failure would stop the retention clock and discard a perfectly good dump
# because of a DNS blip.
#
# The upload keeps its own state file and its own alert
# (OffsiteUploadMissing), so the failure is loud without being conflated.
# --------------------------------------------------------------------------
if [ "${BACKUP_REMOTE_PROVIDER:-none}" != "none" ]; then
    base="$(basename "${TARGET}")"
    log "replicating off-site provider=${BACKUP_REMOTE_PROVIDER}"

    if "${SCRIPT_DIR}/backup-upload.sh" "daily/${base}"; then
        log "off-site upload succeeded"
    else
        log "ERROR: off-site upload FAILED -- the only copy of this backup is on this server"
    fi

    # Tier copies are uploaded under their own keys so remote retention can
    # expire them independently, exactly as the local tiers do.
    if [ "${PROMOTED_WEEKLY}" = "true" ]; then
        "${SCRIPT_DIR}/backup-upload.sh" "weekly/${base}" \
            || log "WARNING: weekly off-site upload failed"
    fi

    if [ "${PROMOTED_MONTHLY}" = "true" ]; then
        "${SCRIPT_DIR}/backup-upload.sh" "monthly/${base}" \
            || log "WARNING: monthly off-site upload failed"
    fi
else
    log "off-site replication disabled (BACKUP_REMOTE_PROVIDER=none) -- backups exist only on this server"
fi

publish_metrics
