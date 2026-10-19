#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p backups logs

LOG_FILE="logs/backup_db.log"
log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

trap 'log "FAILED - unexpected error on line $LINENO"' ERR

timestamp=$(date +%Y%m%d_%H%M%S)
backup_file="backups/db_${timestamp}.sql.gz"

if docker compose exec -T db sh -c 'pg_dump --clean --if-exists -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "$backup_file"; then
    size=$(du -h "$backup_file" | cut -f1)
    log "SUCCESS - backup created: $backup_file ($size)"
    # keep only the last 30 days of backups
    find backups -name "db_*.sql.gz" -mtime +30 -delete
else
    rm -f "$backup_file"
    log "FAILED - pg_dump/gzip failed"
    exit 1
fi

# Uploaded files (signing copies, signed PDFs, signature images). The database
# points at these files, so they are backed up together and restored together.
# Done after the dump, so every file the dump refers to is in the archive.
media_file="backups/media_${timestamp}.tar.gz"
if [ ! -d media ]; then
    log "SKIPPED - no media folder to back up"
elif tar -czf "$media_file" media; then
    size=$(du -h "$media_file" | cut -f1)
    log "SUCCESS - media backup created: $media_file ($size)"
    # keep only the last 30 days of backups, as for the database
    find backups -name "media_*.tar.gz" -mtime +30 -delete
else
    rm -f "$media_file"
    log "FAILED - media archive failed"
    exit 1
fi
