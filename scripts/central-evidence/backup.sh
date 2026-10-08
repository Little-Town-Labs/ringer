#!/usr/bin/env bash
# Nightly dump of the Ringer shared evidence database.
# Configure a scheduled job to run this script as the database service user.
set -euo pipefail
umask 077
dir="${RINGER_BACKUP_DIR:-$HOME/ringer-postgres/backups}"
mkdir -p "$dir"
out="$dir/ringer-$(date +%F).sql.gz"
docker exec ringer-postgres pg_dump -U ringer_owner -d ringer --no-owner | gzip > "$out.tmp"
mv "$out.tmp" "$out"
find "$dir" -name 'ringer-*.sql.gz' -mtime +14 -delete
