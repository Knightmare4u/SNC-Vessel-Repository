#!/usr/bin/env bash
# Back up the PostgreSQL database with pg_dump, verify it, and apply GFS retention.
# Output goes to stdout; cron should redirect it, e.g. >> /var/log/backup/<system>.log 2>&1
# Reports the result to Gatus when GATUS_URL and GATUS_TOKEN are set (environment or .env).
set -euo pipefail

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1"
}

# Record the reason for on_exit to report, then stop
fail() {
    FAIL_REASON="$1"
    log "FAILED - $1"
    exit 1
}

trap 'FAIL_REASON="unexpected error on line $LINENO"; log "FAILED - $FAIL_REASON"' ERR

# Gatus external endpoint this job reports to
GATUS_KEY="backups_snseafiles-database"

# Clean up and report the result to Gatus; reporting is skipped when Gatus is not configured
on_exit() {
    local status=$?
    if [ -n "${TMP_FILE:-}" ]; then
        rm -f "$TMP_FILE"
    fi
    if [ -z "${GATUS_URL:-}" ] || [ -z "${GATUS_TOKEN:-}" ]; then
        return
    fi
    local args=(--data-urlencode "success=true")
    if [ "$status" -ne 0 ]; then
        args=(--data-urlencode "success=false" --data-urlencode "error=${FAIL_REASON:-exit code $status}")
    fi
    args+=(--data-urlencode "duration=${SECONDS}s")
    curl -fsS -m 10 --retry 3 -o /dev/null -G -X POST "${args[@]}" \
        -H "Authorization: Bearer $GATUS_TOKEN" \
        "$GATUS_URL/api/v1/endpoints/$GATUS_KEY/external" \
        || log "WARNING - failed to report result to Gatus"
}
trap on_exit EXIT

# Project root directory (where this script is located); docker compose needs it as cwd
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
BACKUP_DIR="$PROJECT_DIR/backups"

# Read one value from the project's .env; the file is not shell syntax, so it is not sourced
env_value() {
    [ -f "$PROJECT_DIR/.env" ] || return 0
    sed -n "s/^$1=//p" "$PROJECT_DIR/.env" | tail -n 1 | tr -d "\r\"'"
}

# The environment takes precedence over .env
GATUS_URL="${GATUS_URL:-$(env_value GATUS_URL)}"
GATUS_TOKEN="${GATUS_TOKEN:-$(env_value GATUS_TOKEN)}"

# GFS retention: every backup for 14 days, one per week for 8 weeks, one per month for 12 months
KEEP_DAILY_DAYS=14
KEEP_WEEKLY_DAYS=56
KEEP_MONTHLY_DAYS=365

log "START - backup started"

mkdir -p "$BACKUP_DIR"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILE="$BACKUP_DIR/db_${TIMESTAMP}.sql.gz"
TMP_FILE="$BACKUP_FILE.tmp"

if ! docker compose exec -T db sh -c 'pg_dump --clean --if-exists -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "$TMP_FILE"; then
    fail "pg_dump/gzip failed"
fi

# Verify the archive is intact and the dump ran to completion
# (pg_dump 16.10+ appends a \unrestrict line after the end marker, so check more than the last line)
if ! gzip -t "$TMP_FILE"; then
    fail "archive is corrupt"
fi
tail_lines=$(gzip -dc "$TMP_FILE" | tail -n 20)
if [[ "$tail_lines" != *"PostgreSQL database dump complete"* ]]; then
    fail "dump is incomplete: end marker not found"
fi

mv "$TMP_FILE" "$BACKUP_FILE"
size=$(du -h "$BACKUP_FILE" | cut -f1)
log "SUCCESS - backup created: $BACKUP_FILE ($size)"

# Uploaded files (signing copies, signed PDFs, signature images). The database
# points at these files, so they are backed up together and restored together.
# Done after the dump, so every file the dump refers to is in the archive.
MEDIA_FILE="$BACKUP_DIR/media_${TIMESTAMP}.tar.gz"
if [ ! -d media ]; then
    log "SKIPPED - no media folder to back up"
else
    TMP_FILE="$MEDIA_FILE.tmp"
    if ! tar -czf "$TMP_FILE" media; then
        fail "media archive failed"
    fi
    mv "$TMP_FILE" "$MEDIA_FILE"
    size=$(du -h "$MEDIA_FILE" | cut -f1)
    log "SUCCESS - media backup created: $MEDIA_FILE ($size)"
fi

# Runs only after a verified backup, so a failing job never prunes old backups
prune_backups() {
    local f name stamp ts age week month keep
    local -A seen_week=() seen_month=()
    local now
    now=$(date +%s)

    # Glob order is chronological, so the first file seen per week/month is the oldest one
    for f in "$BACKUP_DIR"/db_*.sql.gz; do
        [ -e "$f" ] || continue
        name=$(basename "$f")
        stamp=${name:3:8}
        ts=$(date -d "$stamp" +%s 2>/dev/null) || continue
        age=$(( (now - ts) / 86400 ))
        week=$(date -d "$stamp" +%G%V)
        month=${stamp:0:6}
        keep=false

        if [ "$age" -lt "$KEEP_DAILY_DAYS" ]; then
            keep=true
        fi
        if [ -z "${seen_week[$week]:-}" ]; then
            seen_week[$week]=1
            if [ "$age" -lt "$KEEP_WEEKLY_DAYS" ]; then
                keep=true
            fi
        fi
        if [ -z "${seen_month[$month]:-}" ]; then
            seen_month[$month]=1
            if [ "$age" -lt "$KEEP_MONTHLY_DAYS" ]; then
                keep=true
            fi
        fi

        if [ "$keep" = false ]; then
            # The media archive from the same run goes with its dump
            rm -f -- "$f" "$BACKUP_DIR/media_${name:3:15}.tar.gz"
            log "PRUNED - $f"
        fi
    done
}

prune_backups
