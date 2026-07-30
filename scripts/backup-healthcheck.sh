#!/usr/bin/env sh
#
# Docker healthcheck for the backup service.
#
# The interesting failure is not "the container died" -- it is "the container
# is running happily and has not produced a backup in three days". A process
# liveness check cannot see that, so this checks the age of the last SUCCESS
# instead.

set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
STATE_FILE="${BACKUP_DIR}/state/last_success"

# Default allows one missed nightly run before going unhealthy: a single
# transient failure (database restarting during the window) self-heals, two in
# a row is a real problem.
MAX_AGE_HOURS="${BACKUP_MAX_AGE_HOURS:-30}"

# A brand new deployment has not backed up yet. Report healthy during the
# grace period so the stack does not come up unhealthy before 02:00.
GRACE_HOURS="${BACKUP_GRACE_HOURS:-26}"

now="$(date -u +%s)"

if [ ! -f "${STATE_FILE}" ]; then
    marker="${BACKUP_DIR}/state/first_started"
    [ -f "${marker}" ] || date -u +%s > "${marker}"
    age=$(( now - $(cat "${marker}") ))
    if [ "${age}" -lt $(( GRACE_HOURS * 3600 )) ]; then
        echo "no backup yet, still inside the ${GRACE_HOURS}h grace period"
        exit 0
    fi
    echo "UNHEALTHY: no successful backup has ever completed"
    exit 1
fi

last="$(cat "${STATE_FILE}")"
age=$(( now - last ))
max=$(( MAX_AGE_HOURS * 3600 ))

if [ "${age}" -gt "${max}" ]; then
    echo "UNHEALTHY: last successful backup was $(( age / 3600 ))h ago (limit ${MAX_AGE_HOURS}h)"
    exit 1
fi

echo "last successful backup $(( age / 3600 ))h ago"
exit 0
