#!/usr/bin/env bash
# Daily backup (netmon-backup.timer, 02:17): into /var/backups/netmon/daily/
#   netmon-db-<ts>.dump        pg_dump -Fc of the netmon database (TimescaleDB)
#   netmon-data-<ts>.tar.gz    the app data volume (db_password, secret.key, templates, ...)
#                              without run/ ([3] materialised Kerberos keytab, krb5.conf, replay
#                              cache: recreated at start; the keytab is encrypted in the DB)
#   netmon-meta-<ts>.txt       deployed revision + image (which app version wrote the dump)
# Keeps NETMON_BACKUP_KEEP_DAYS (default 14) days. Backups contain secrets: root-only.
# Restore: ops/restore.sh (manual, never automatic).
#
# Usage: backup.sh [--keep-days N]
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$SELF_DIR/lib.sh"

KEEP_DAYS="${NETMON_BACKUP_KEEP_DAYS:-14}"
while (($#)); do
  case "$1" in
    --keep-days) KEEP_DAYS="${2:?}"; shift 2 ;;
    -h | --help) sed -n '2,13p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ $KEEP_DAYS =~ ^[0-9]+$ && $KEEP_DAYS -ge 1 ]] || die "--keep-days must be a positive number"

require_root
# Never run in the middle of a deploy (shares the lock with sync.sh); wait up to 30 min.
exec 9>"$NETMON_LOCK_FILE"
flock -w 1800 9 || die "could not get $NETMON_LOCK_FILE within 30 minutes"

DIR="$NETMON_BACKUP_DIR/daily"
install -d -m 0700 "$NETMON_BACKUP_DIR" "$DIR"
TS="$(date '+%Y%m%d-%H%M%S')"

on_fail() {
  err "backup failed"
  notify "Daily backup FAILED - see journalctl -u netmon-backup"
}
trap on_fail ERR

db_dump "$DIR/netmon-db-$TS.dump"

(
  umask 077
  compose run --rm --no-deps -T --entrypoint tar init -C /data -czf - --exclude ./run . >"$DIR/netmon-data-$TS.tar.gz.partial"
)
tar -tzf "$DIR/netmon-data-$TS.tar.gz.partial" >/dev/null
mv "$DIR/netmon-data-$TS.tar.gz.partial" "$DIR/netmon-data-$TS.tar.gz"
log "data volume archived: $DIR/netmon-data-$TS.tar.gz"

{
  echo "time=$(date -Is)"
  echo "deployed_rev=$(cat "$NETMON_STATE_DIR/deployed_rev" 2>/dev/null || true)"
  echo "release_image=$(env_get "$NETMON_PROD_DIR/release.env" NETMON_IMAGE)"
  echo "running_image=$(running_image netmon || true)"
} >"$DIR/netmon-meta-$TS.txt"
chmod 0600 "$DIR"/*"-$TS".*

# Retention: whole days, by modification time.
find "$DIR" -maxdepth 1 -type f -name 'netmon-*' -mmin +$((KEEP_DAYS * 1440)) -print -delete |
  while IFS= read -r f; do log "pruned $f"; done
log "backup done ($(du -sh "$DIR" | cut -f1) in $DIR, keeping $KEEP_DAYS days)"
