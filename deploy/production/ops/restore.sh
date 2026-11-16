#!/usr/bin/env bash
# MANUAL restore of the NetworkMonitor database (and optionally the app data volume).
# Nothing restores the database automatically — migrations are forward-only, so this is
# a deliberate operator decision. See README.md ("שחזור").
#
#   sudo ./ops/restore.sh --dump /var/backups/netmon/pre-deploy-<rev>-<ts>.dump
#   sudo ./ops/restore.sh --dump .../daily/netmon-db-<ts>.dump --data .../daily/netmon-data-<ts>.tar.gz
#
# Steps: stop the GitOps timer -> stop netmon + proxy -> safety dump of the current DB
# (/var/backups/netmon/pre-restore-<ts>.dump) -> [restore the data volume, keeping the
# current db_password if the DB already exists] -> drop + recreate the netmon database ->
# timescaledb_pre_restore() -> pg_restore -> timescaledb_post_restore() -> start the stack
# -> wait for health -> start the timer again.
#
# The app version that runs after the restore must be >= the version that wrote the dump
# (an older app cannot read a newer schema; a newer app migrates it forward on start).
# Typical rollback across a migration: merge a revert of release.env first, then restore
# the pre-deploy dump taken before the bad upgrade (its name holds that revision).
#
# Options: --dump FILE (required) --data FILE.tar.gz --yes (no prompt)
#          --no-safety-dump (when the current DB is broken and cannot be dumped)
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$SELF_DIR/lib.sh"

DUMP="" DATA="" YES=0 SAFETY=1
while (($#)); do
  case "$1" in
    --dump) DUMP="${2:?}"; shift 2 ;;
    --data) DATA="${2:?}"; shift 2 ;;
    --yes) YES=1; shift ;;
    --no-safety-dump) SAFETY=0; shift ;;
    -h | --help) sed -n '2,23p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
require_root
[[ -n $DUMP ]] || die "--dump FILE is required (see --help)"
[[ -s $DUMP ]] || die "$DUMP not found or empty"
[[ -z $DATA || -s $DATA ]] || die "$DATA not found or empty"
if [[ -n $DATA ]]; then
  tar -tzf "$DATA" >/dev/null 2>&1 || die "$DATA is not a .tar.gz"
  # [1.4.2] Only plain relative members below /data: no absolute path, no "..", no links
  # (backup.sh never writes those; a crafted archive could otherwise write outside /data).
  bad="$(tar -tzf "$DATA" | grep -E '(^/|(^|/)\.\.(/|$))' | head -n1 || true)"
  [[ -z $bad ]] || die "$DATA has an unsafe member path: $bad"
  bad="$(tar -tvzf "$DATA" | grep -E '^[lh]' | head -n1 || true)"
  [[ -z $bad ]] || die "$DATA contains a link, refused: $bad"
fi

meta="${DUMP%.dump}"
meta="${meta/netmon-db-/netmon-meta-}.txt"
echo "Dump:  $DUMP ($(du -h "$DUMP" | cut -f1), $(date -r "$DUMP" '+%F %T'))"
[[ -r $meta ]] && sed 's/^/       /' "$meta"
echo "Data:  ${DATA:-(not restored)}"
echo "Now:   $(running_image netmon || echo 'netmon not running')"
echo
echo "This REPLACES the current netmon database. A safety dump is taken first."
if ((!YES)); then
  read -r -p "Type RESTORE to continue: " answer
  [[ $answer == RESTORE ]] || die "aborted"
fi

timer_was_active=0
if systemctl is-active --quiet netmon-gitops.timer 2>/dev/null; then
  timer_was_active=1
  systemctl stop netmon-gitops.timer
  log "stopped netmon-gitops.timer"
fi
restart_timer() {
  if ((timer_was_active)); then systemctl start netmon-gitops.timer && log "started netmon-gitops.timer again"; fi
}
trap restart_timer EXIT

exec 9>"$NETMON_LOCK_FILE"
flock -w 900 9 || die "a sync/backup run holds $NETMON_LOCK_FILE"

had_db=0
[[ -n "$("$DOCKER" volume ls -q --filter label=com.docker.compose.project=netmon \
  --filter label=com.docker.compose.volume=netmon-db)" ]] && had_db=1

log "stopping netmon and proxy"
compose stop netmon proxy || true

if [[ -n $DATA ]]; then
  log "restoring the data volume from $DATA"
  # The DB role password lives in the existing DB cluster: keep the current db_password
  # if the DB already exists; on a fresh server take the one from the archive.
  # shellcheck disable=SC2016 # expanded by the container's sh
  compose run --rm --no-deps -T --entrypoint sh -e KEEP_PW="$had_db" init -euc '
    if [ "$KEEP_PW" = 1 ] && [ -f /data/db_password ]; then cp -p /data/db_password /tmp/pw; fi
    tar -xzf - -C /data
    if [ -f /tmp/pw ]; then cp -p /tmp/pw /data/db_password; fi' <"$DATA"
fi

log "starting db"
compose up -d db
deadline=$((SECONDS + 300))
until [[ "$(service_state db)" == healthy ]]; do
  ((SECONDS < deadline)) || die "db not healthy"
  sleep 3
done
cid="$(service_cid db)"

if ((SAFETY && had_db)); then
  db_dump "$NETMON_BACKUP_DIR/pre-restore-$(date '+%Y%m%d-%H%M%S').dump" ||
    die "safety dump failed (use --no-safety-dump if the current DB is unusable)"
fi

psql_() { "$DOCKER" exec -i -e PGOPTIONS='-c client_min_messages=warning' "$cid" psql -X -q -v ON_ERROR_STOP=1 -U netmon "$@"; }
log "recreating the netmon database"
psql_ -d postgres -c "DROP DATABASE IF EXISTS netmon WITH (FORCE);" -c "CREATE DATABASE netmon OWNER netmon;"
psql_ -d netmon -c "CREATE EXTENSION IF NOT EXISTS timescaledb;" -c "SELECT timescaledb_pre_restore();" >/dev/null

log "pg_restore (TimescaleDB pre/post restore mode)"
"$DOCKER" cp "$DUMP" "$cid:/tmp/netmon-restore.dump"
rc=0
"$DOCKER" exec "$cid" pg_restore -U netmon -d netmon --no-owner --role=netmon /tmp/netmon-restore.dump \
  >"$NETMON_STATE_DIR/restore-$$.log" 2>&1 || rc=$?
psql_ -d netmon -c "SELECT timescaledb_post_restore();" >/dev/null
"$DOCKER" exec "$cid" rm -f /tmp/netmon-restore.dump
if ((rc != 0)); then
  # "extension timescaledb already exists" and similar are expected; show the rest.
  warn "pg_restore reported errors (exit $rc); review:"
  grep -v 'already exists' "$NETMON_STATE_DIR/restore-$$.log" | tail -n 30 >&2 || true
fi
rm -f "$NETMON_STATE_DIR/restore-$$.log"

log "starting the stack"
compose up -d --remove-orphans
if wait_healthy; then
  log "restore finished: healthy"
else
  die "the stack is not healthy after the restore: docker compose logs netmon"
fi
