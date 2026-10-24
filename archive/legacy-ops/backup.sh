#!/usr/bin/env bash
set -euo pipefail
umask 077

: "${DB_USER:?DB_USER is required}"
: "${DB_PASSWORD:?DB_PASSWORD is required}"
: "${DB_NAME:?DB_NAME is required}"
backup_dir=/var/backups/mushroom-king-timer
mkdir -p "$backup_dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
tmp="$backup_dir/.mysql-$stamp.sql.gz.tmp"
final="$backup_dir/mysql-$stamp.sql.gz"
trap 'rm -f "$tmp"' EXIT
MYSQL_PWD="$DB_PASSWORD" mysqldump --host="${DB_HOST:-127.0.0.1}" --port="${DB_PORT:-3306}" \
    --user="$DB_USER" --single-transaction --no-tablespaces --routines --triggers \
    "$DB_NAME" | gzip -9 > "$tmp"
gzip -t "$tmp"
test "$(gzip -dc "$tmp" | grep -c 'CREATE TABLE')" -ge 5
mv "$tmp" "$final"
trap - EXIT
find "$backup_dir" -maxdepth 1 -type f -name 'mysql-*.sql.gz' -mtime +14 -delete
echo "Backup written: $final"
