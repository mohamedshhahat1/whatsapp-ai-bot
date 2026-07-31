#!/usr/bin/env sh
#
# Turn the backup state directory into Prometheus metrics.
#
# Writes a textfile-collector .prom file that node-exporter picks up. This is
# the bridge between shell scripts that run once a day and a monitoring system
# that scrapes every fifteen seconds.
#
# It exists because the alternative -- alert rules referencing metrics nothing
# ever exports -- fails silently. A Prometheus rule whose metric is absent does
# not error, it simply never fires, which on a dashboard is indistinguishable
# from a rule that keeps passing. Believing you have backup alerting when you
# have none is strictly worse than knowing you have none.
#
# Run after every backup, upload, verification and drill, and periodically by
# the scheduler so the timestamps keep ageing even when nothing runs.

set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
STATE_DIR="${BACKUP_DIR}/state"
METRICS_DIR="${BACKUP_METRICS_DIR:-${BACKUP_DIR}/metrics}"
OUT="${METRICS_DIR}/backup.prom"

mkdir -p "${METRICS_DIR}"

# Read a unix timestamp from a state file, or 0 when it has never happened.
# 0 rather than omitting the metric: "never" is a fact worth alerting on, and
# an absent series cannot be compared against time().
read_ts() {
    if [ -f "$1" ]; then
        _v="$(tr -dc '0-9' < "$1")"
        [ -n "${_v}" ] && printf '%s' "${_v}" || printf '0'
    else
        printf '0'
    fi
}

# Extract "status": "ok" from a small JSON file without needing jq, which the
# postgres image does not ship.
read_status() {
    if [ -f "$1" ] && grep -q '"status"[[:space:]]*:[[:space:]]*"ok"' "$1"; then
        printf '1'
    elif [ -f "$1" ]; then
        printf '0'
    else
        printf '0'
    fi
}

count_tier() {
    ls -1 "${BACKUP_DIR}/$1"/*.dump 2>/dev/null | wc -l | tr -d ' '
}

newest_size() {
    _f="$(ls -1t "${BACKUP_DIR}/daily"/*.dump 2>/dev/null | head -n 1 || true)"
    if [ -n "${_f}" ] && [ -f "${_f}" ]; then
        wc -c < "${_f}" | tr -d ' '
    else
        printf '0'
    fi
}

# Total bytes held locally. Useful for the disk alerts: it explains WHY the
# disk is filling, which is otherwise a five-minute investigation at 3am.
local_bytes() {
    du -sb "${BACKUP_DIR}/daily" "${BACKUP_DIR}/weekly" "${BACKUP_DIR}/monthly" 2>/dev/null \
        | awk '{s += $1} END {printf "%d", s+0}'
}

TMP="${OUT}.tmp"

{
    printf '# HELP backup_last_status Whether the most recent local backup run succeeded (1=ok, 0=failed).\n'
    printf '# TYPE backup_last_status gauge\n'
    printf 'backup_last_status %s\n' "$(read_status "${STATE_DIR}/last_result.json")"

    printf '# HELP backup_last_success_timestamp_seconds Unix time of the last successful local backup.\n'
    printf '# TYPE backup_last_success_timestamp_seconds gauge\n'
    printf 'backup_last_success_timestamp_seconds %s\n' "$(read_ts "${STATE_DIR}/last_success")"

    printf '# HELP backup_last_size_bytes Size of the newest local backup file.\n'
    printf '# TYPE backup_last_size_bytes gauge\n'
    printf 'backup_last_size_bytes %s\n' "$(newest_size)"

    printf '# HELP backup_local_bytes_total Total bytes consumed by local backups across all tiers.\n'
    printf '# TYPE backup_local_bytes_total gauge\n'
    printf 'backup_local_bytes_total %s\n' "$(local_bytes)"

    printf '# HELP backup_files_count Number of retained backup files per tier.\n'
    printf '# TYPE backup_files_count gauge\n'
    printf 'backup_files_count{tier="daily"} %s\n' "$(count_tier daily)"
    printf 'backup_files_count{tier="weekly"} %s\n' "$(count_tier weekly)"
    printf 'backup_files_count{tier="monthly"} %s\n' "$(count_tier monthly)"

    printf '# HELP backup_offsite_last_status Whether the most recent off-site upload succeeded (1=ok, 0=failed).\n'
    printf '# TYPE backup_offsite_last_status gauge\n'
    printf 'backup_offsite_last_status %s\n' "$(read_status "${STATE_DIR}/last_upload.json")"

    printf '# HELP backup_last_offsite_upload_timestamp_seconds Unix time of the last successful off-site upload.\n'
    printf '# TYPE backup_last_offsite_upload_timestamp_seconds gauge\n'
    printf 'backup_last_offsite_upload_timestamp_seconds %s\n' "$(read_ts "${STATE_DIR}/last_upload_success")"

    printf '# HELP backup_remote_verify_status Whether the last off-site verification passed (1=ok, 0=failed).\n'
    printf '# TYPE backup_remote_verify_status gauge\n'
    printf 'backup_remote_verify_status %s\n' "$(read_status "${STATE_DIR}/remote_verify.json")"

    printf '# HELP backup_remote_verify_last_success_timestamp_seconds Unix time of the last passing off-site verification.\n'
    printf '# TYPE backup_remote_verify_last_success_timestamp_seconds gauge\n'
    printf 'backup_remote_verify_last_success_timestamp_seconds %s\n' "$(read_ts "${STATE_DIR}/last_remote_verify_success")"

    printf '# HELP restore_drill_status Whether the last automated restore drill passed (1=ok, 0=failed).\n'
    printf '# TYPE restore_drill_status gauge\n'
    printf 'restore_drill_status %s\n' "$(read_status "${STATE_DIR}/restore_drill.json")"

    printf '# HELP restore_drill_last_success_timestamp_seconds Unix time of the last passing restore drill.\n'
    printf '# TYPE restore_drill_last_success_timestamp_seconds gauge\n'
    printf 'restore_drill_last_success_timestamp_seconds %s\n' "$(read_ts "${STATE_DIR}/last_restore_drill_success")"

    printf '# HELP backup_metrics_written_timestamp_seconds Unix time this metrics file was last written.\n'
    printf '# TYPE backup_metrics_written_timestamp_seconds gauge\n'
    printf 'backup_metrics_written_timestamp_seconds %s\n' "$(date -u +%s)"
} > "${TMP}"

# Atomic swap. node-exporter reads this file on its own schedule and a
# half-written .prom is a parse error that drops every metric in it.
mv "${TMP}" "${OUT}"
