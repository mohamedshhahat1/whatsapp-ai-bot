#!/usr/bin/env sh
#
# Entrypoint for the `backup` service: a supervised loop, not cron.
#
# cron inside a container means a second process manager, logs that go to a
# file nobody tails instead of `docker logs`, and a silent failure mode where
# the daemon is up but the crontab never got installed. A loop is visible in
# the process table, writes to stdout, and stops when the container stops.
#
# Two schedules:
#   - a backup every BACKUP_INTERVAL_HOURS (default 24), aligned to BACKUP_HOUR
#   - a restore drill every RESTORE_DRILL_DAYS (default 7)

set -eu

BACKUP_HOUR="${BACKUP_HOUR:-2}"                     # UTC hour for the daily run
RESTORE_DRILL_DAYS="${RESTORE_DRILL_DAYS:-7}"
RESTORE_DRILL_ENABLED="${RESTORE_DRILL_ENABLED:-true}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
STATE_DIR="${BACKUP_DIR}/state"

mkdir -p "${STATE_DIR}"

log() {
    printf '%s [scheduler] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

# Stop promptly on `docker compose down` instead of sitting out the rest of a
# sleep. Without this the container is SIGKILLed after the grace period, which
# is survivable here but produces a confusing exit code in the logs.
terminate() {
    log "received TERM, shutting down"
    exit 0
}
trap terminate TERM INT

seconds_until_hour() {
    target="$1"
    now_h="$(date -u +%-H)"
    now_m="$(date -u +%-M)"
    now_s="$(date -u +%-S)"
    now_total=$(( now_h * 3600 + now_m * 60 + now_s ))
    target_total=$(( target * 3600 ))
    if [ "${target_total}" -le "${now_total}" ]; then
        target_total=$(( target_total + 86400 ))
    fi
    echo $(( target_total - now_total ))
}

should_run_drill() {
    [ "${RESTORE_DRILL_ENABLED}" = "true" ] || return 1
    marker="${STATE_DIR}/last_drill"
    [ -f "${marker}" ] || return 0
    age=$(( $(date -u +%s) - $(cat "${marker}") ))
    [ "${age}" -ge $(( RESTORE_DRILL_DAYS * 86400 )) ]
}

log "started -- daily backup at ${BACKUP_HOUR}:00 UTC, restore drill every ${RESTORE_DRILL_DAYS}d"

# --------------------------------------------------------------------------
# Take one immediately if there has never been a backup. A host that comes up
# at 03:00 should not sit unprotected until 02:00 tomorrow.
# --------------------------------------------------------------------------
if [ ! -f "${STATE_DIR}/last_success" ]; then
    log "no previous backup found, taking one now"
    /scripts/backup.sh || log "initial backup failed, will retry on schedule"
fi

while true; do
    wait_for="$(seconds_until_hour "${BACKUP_HOUR}")"
    log "sleeping ${wait_for}s until the next run"

    # Backgrounded sleep + wait, so the TERM trap fires immediately rather
    # than after the sleep finishes.
    sleep "${wait_for}" &
    wait $! || true

    if /scripts/backup.sh; then
        log "scheduled backup succeeded"
    else
        log "scheduled backup FAILED -- healthcheck will flag this if it repeats"
    fi

    if should_run_drill; then
        log "running the weekly restore drill"
        if /scripts/verify-restore.sh latest; then
            date -u +%s > "${STATE_DIR}/last_drill"
            log "restore drill passed"
        else
            log "restore drill FAILED -- the backups may not be restorable"
        fi
    fi
done
