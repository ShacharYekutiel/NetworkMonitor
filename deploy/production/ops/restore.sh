#!/usr/bin/env bash
# MANUAL restore of the NetworkMonitor database (and optionally the app data volume).
# Nothing restores the database automatically — migrations are forward-only, so this is
# a deliberate operator decision. See README.md ("שחזור").
#
#   sudo ./ops/restore.sh --dump /var/backups/netmon/pre-deploy-<rev>-<ts>.dump
#   sudo ./ops/restore.sh --dump .../daily/netmon-db-<ts>.dump --data .../daily/netmon-data-<ts>.tar.gz
#
# Steps: check the dump against the db image (below; nothing is touched when it does not fit)
# -> confirm -> stop the GitOps timer -> stop netmon + proxy -> safety dump of the current DB
# (/var/backups/netmon/pre-restore-<ts>.dump) -> [restore the data volume, keeping the
# current db_password if the DB already exists] -> drop + recreate the netmon database (from
# template0) -> CREATE EXTENSION timescaledb VERSION '<the dump's version>' ->
# timescaledb_pre_restore() -> pg_restore -> timescaledb_post_restore() -> [1.5.1] ALTER
# EXTENSION timescaledb UPDATE (fresh session) when the image's version is newer -> start the
# stack -> wait for health -> start the timer again.
#
# [1.5.1] TimescaleDB versions: a dump must be restored into the extension version that wrote
# it, then updated (Timescale: "Version mismatch when dumping and restoring"). The version is
# read from the dump (_timescaledb_catalog.metadata). The db image of compose.yaml must ship
# that version (newer timescale/timescaledb images ship the earlier releases too); otherwise
# the script stops before touching anything and names the image tag to use. --ts-version X.Y.Z
# only for a dump that does not record its version.
#
# The app version that runs after the restore must be >= the version that wrote the dump
# (an older app cannot read a newer schema; a newer app migrates it forward on start).
# Typical rollback across a migration: merge a revert of release.env first, then restore
# the pre-deploy dump taken before the bad upgrade (its name holds that revision).
#
# Options: --dump FILE (required) --data FILE.tar.gz --yes (no prompt)
#          --no-safety-dump (when the current DB is broken and cannot be dumped)
#          --ts-version X.Y.Z (TimescaleDB version of a dump that does not record it)
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$SELF_DIR/lib.sh"

DUMP="" DATA="" YES=0 SAFETY=1 TS_VERSION=""
while (($#)); do
  case "$1" in
    --dump) DUMP="${2:?}"; shift 2 ;;
    --data) DATA="${2:?}"; shift 2 ;;
    --yes) YES=1; shift ;;
    --no-safety-dump) SAFETY=0; shift ;;
    --ts-version) TS_VERSION="${2:?}"; shift 2 ;;
    -h | --help) sed -n '2,32p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
require_root
[[ -n $DUMP ]] || die "--dump FILE is required (see --help)"
[[ -s $DUMP ]] || die "$DUMP not found or empty"
[[ -z $DATA || -s $DATA ]] || die "$DATA not found or empty"
[[ -z $TS_VERSION || $TS_VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "--ts-version must look like 2.31.0"
DB="$NETMON_DB_NAME" DBUSER="$NETMON_DB_USER"
if [[ -n $DATA ]]; then
  tar -tzf "$DATA" >/dev/null 2>&1 || die "$DATA is not a .tar.gz"
  # [1.4.2] Only plain relative members below /data: no absolute path, no "..", no links
  # (backup.sh never writes those; a crafted archive could otherwise write outside /data).
  bad="$(tar -tzf "$DATA" | grep -E '(^/|(^|/)\.\.(/|$))' | head -n1 || true)"
  [[ -z $bad ]] || die "$DATA has an unsafe member path: $bad"
  bad="$(tar -tvzf "$DATA" | grep -E '^[lh]' | head -n1 || true)"
  [[ -z $bad ]] || die "$DATA contains a link, refused: $bad"
fi

# [1.5.1] Which TimescaleDB version wrote the dump, and can the db image create it? Checked
# before anything is stopped or dropped.
DB_IMAGE="$(compose_db_image)"
[[ -n $DB_IMAGE ]] || die "cannot read the db image from compose.yaml (docker compose config --images db)"
probe="$(ts_probe "$DB_IMAGE" "$DUMP")" || die "could not inspect $DUMP with $DB_IMAGE (image pull failed?)"
grep -qx 'dump_error=1' <<<"$probe" && die "$DUMP is not a pg_dump custom-format file that $DB_IMAGE can read"
ts_default="$(sed -n 's/^default=//p' <<<"$probe" | head -n1)"
[[ -n $ts_default ]] || die "$DB_IMAGE has no TimescaleDB extension"
dump_ts=0 dump_ver=""
if grep -qx 'dump_ts=1' <<<"$probe"; then
  dump_ts=1
  dump_ver="$(sed -n 's/^dump=//p' <<<"$probe" | head -n1)"
  if [[ -n $TS_VERSION && -n $dump_ver && $TS_VERSION != "$dump_ver" ]]; then
    die "--ts-version $TS_VERSION contradicts the dump, which records TimescaleDB $dump_ver"
  fi
  dump_ver="${dump_ver:-$TS_VERSION}"
  [[ -n $dump_ver ]] || die "$DUMP does not record its TimescaleDB version; pass --ts-version X.Y.Z (the version of the server that wrote it)"
  if ! grep -qx "have=$dump_ver" <<<"$probe"; then
    shipped="$(sed -n 's/^have=//p' <<<"$probe" | sort -V | tail -n 5 | tr '\n' ' ')"
    die "$DUMP was written by TimescaleDB $dump_ver, but $DB_IMAGE (compose.yaml) cannot create that version (newest it ships: ${shipped:-none}). Nothing was changed. Restore it with a db image that ships $dump_ver, e.g. $(ts_image_for "$dump_ver" "$DB_IMAGE") or a newer timescale/timescaledb tag: check out a deploy/production revision whose compose.yaml has it (docs/production.md, \"[1.5.1] Upgrade and rollback\")."
  fi
  ts_plan="dump on $dump_ver -> create $dump_ver, restore"
  [[ $dump_ver == "$ts_default" ]] || ts_plan+=", update to $ts_default"
else
  ts_plan="no TimescaleDB catalog in the dump (plain PostgreSQL mode): extension created at $ts_default"
fi

meta="${DUMP%.dump}"
meta="${meta/netmon-db-/netmon-meta-}.txt"
echo "Dump:  $DUMP ($(du -h "$DUMP" | cut -f1), $(date -r "$DUMP" '+%F %T'))"
[[ -r $meta ]] && sed 's/^/       /' "$meta"
echo "Data:  ${DATA:-(not restored)}"
echo "Now:   $(running_image netmon || echo 'netmon not running')"
echo "DB:    $DB_IMAGE (TimescaleDB $ts_default); $ts_plan"
echo
echo "This REPLACES the current $DB database. A safety dump is taken first."
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

# Safety dump from the db container as it runs now (before it may be recreated on another image).
safety_done=0
if ((SAFETY && had_db)) && [[ "$(service_state db)" == healthy ]]; then
  db_dump "$NETMON_BACKUP_DIR/pre-restore-$(date '+%Y%m%d-%H%M%S').dump" ||
    die "safety dump failed (use --no-safety-dump if the current DB is unusable)"
  safety_done=1
fi

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

if ((SAFETY && had_db && !safety_done)); then
  db_dump "$NETMON_BACKUP_DIR/pre-restore-$(date '+%Y%m%d-%H%M%S').dump" ||
    die "safety dump failed (use --no-safety-dump if the current DB is unusable)"
fi

psql_() { "$DOCKER" exec -i -e PGOPTIONS='-c client_min_messages=warning' "$cid" psql -X -q -v ON_ERROR_STOP=1 -U "$DBUSER" "$@"; }
log "recreating the $DB database"
# template0: the timescale image installs the extension into template1 at initdb time (at the
# version of that day), which would pre-empt the version chosen below.
psql_ -d postgres -c "DROP DATABASE IF EXISTS \"$DB\" WITH (FORCE);" -c "CREATE DATABASE \"$DB\" OWNER \"$DBUSER\" TEMPLATE template0;"
if ((dump_ts)); then
  log "creating the TimescaleDB extension at the dump's version $dump_ver"
  psql_ -d "$DB" -c "CREATE EXTENSION timescaledb VERSION '$dump_ver';" -c "SELECT timescaledb_pre_restore();" >/dev/null
else
  psql_ -d "$DB" -c "CREATE EXTENSION IF NOT EXISTS timescaledb;" -c "SELECT timescaledb_pre_restore();" >/dev/null
fi

log "pg_restore (TimescaleDB pre/post restore mode)"
"$DOCKER" cp "$DUMP" "$cid:/tmp/netmon-restore.dump"
rc=0
"$DOCKER" exec "$cid" pg_restore -U "$DBUSER" -d "$DB" --no-owner --role="$DBUSER" /tmp/netmon-restore.dump \
  >"$NETMON_STATE_DIR/restore-$$.log" 2>&1 || rc=$?
psql_ -d "$DB" -c "SELECT timescaledb_post_restore();" >/dev/null
"$DOCKER" exec "$cid" rm -f /tmp/netmon-restore.dump
if ((rc != 0)); then
  # "extension timescaledb already exists" and similar are expected; show the rest.
  warn "pg_restore reported errors (exit $rc); review:"
  grep -v 'already exists' "$NETMON_STATE_DIR/restore-$$.log" | tail -n 30 >&2 || true
fi
rm -f "$NETMON_STATE_DIR/restore-$$.log"

# [1.5.1] Then update to the image's version: the first statement of a fresh session (psql -X,
# no .psqlrc), as TimescaleDB requires. The app (>= 1.5.1) would also do it at start; doing it
# here makes the result independent of the app version (an older library under a newer
# PostgreSQL minor can fail every background job: TimescaleDB 2.31.1/2.32.1 release notes).
ts_now="$(psql_ -d "$DB" -A -t -c "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';" | tr -d '[:space:]')"
if [[ -n $ts_now && $ts_now != "$ts_default" ]]; then
  log "updating the TimescaleDB extension $ts_now -> $ts_default"
  if psql_ -d "$DB" -c "ALTER EXTENSION timescaledb UPDATE;"; then
    ts_now="$(psql_ -d "$DB" -A -t -c "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';" | tr -d '[:space:]')"
  else
    warn "ALTER EXTENSION timescaledb UPDATE failed; the data is restored on TimescaleDB $ts_now. Run it by hand: docker exec $cid psql -X -U $DBUSER -d $DB -c 'ALTER EXTENSION timescaledb UPDATE'"
  fi
fi
log "TimescaleDB in $DB: ${ts_now:-none} (image default $ts_default)"

log "starting the stack"
compose up -d --remove-orphans
if wait_healthy; then
  log "restore finished: healthy"
else
  die "the stack is not healthy after the restore: docker compose logs netmon"
fi
