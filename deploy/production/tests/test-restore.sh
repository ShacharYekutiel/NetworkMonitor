#!/usr/bin/env bash
# [1.5.1] ops/restore.sh with REAL PostgreSQL + TimescaleDB (docker compose itself is faked by
# tests/fake-bin/docker-pg): a dump written by an OLDER TimescaleDB extension version is
# restored into that version, then updated to the image's version; the data, compressed
# chunks, continuous aggregate and policy jobs survive and the jobs run. A dump whose version
# the image does not ship stops the script before anything is touched; a dump without a
# recorded version needs --ts-version.
#
# Docker mode (CI, production.yml job "restore-xver"): two containers already running,
#   SRC_CONTAINER (old image, e.g. timescale/timescaledb:2.31.0-pg16) and
#   TGT_CONTAINER (new image, e.g. 2.32.1-pg16), POSTGRES_USER=netmon, POSTGRES_DB=netmon:
#     SRC_CONTAINER=ts-old TGT_CONTAINER=ts-new TGT_IMAGE=timescale/timescaledb:2.32.1-pg16 \
#       deploy/production/tests/test-restore.sh
# Local mode (a PostgreSQL 16 with several TimescaleDB versions installed, e.g. the Debian
# package): PGHOST/PGPORT/PGUSER/PGPASSWORD for a superuser, and OLD_TS = an installed version
# older than the default one (own databases rx_src_* / rx_tgt_*, dropped at the end):
#     PGHOST=127.0.0.1 PGUSER=postgres PGPASSWORD=... OLD_TS=2.30.2 tests/test-restore.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROD_SRC="$(cd "$HERE/.." && pwd)"
T="$(mktemp -d)"
export FAKE_STATE="$T/fake" NETMON_ALLOW_NON_ROOT=1
export NETMON_ETC_DIR="$T/etc" NETMON_STATE_DIR="$T/state" NETMON_BACKUP_DIR="$T/backups"
export NETMON_LOCK_FILE="$T/lock" NETMON_HEALTH_TIMEOUT=5 NETMON_HEALTH_POLL=1
export NETMON_PROD_DIR="$PROD_SRC" NETMON_REPO_DIR="$T/repo"
export DOCKER="$HERE/fake-bin/docker-pg" CURL="$T/curl"
unset JOURNAL_STREAM
mkdir -p "$FAKE_STATE" "$NETMON_ETC_DIR" "$NETMON_STATE_DIR" "$NETMON_BACKUP_DIR"
echo NETMON_FQDN=netmonitor.test >"$NETMON_ETC_DIR/site.env"
printf '#!/bin/sh\necho %s\n' "'{\"status\": \"ok\"}'" >"$CURL"
chmod +x "$CURL"

pass=0
ok() { pass=$((pass + 1)); echo "ok $pass - $*"; }
fail() { echo "FAIL - $*" >&2; exit 1; }

if [[ -n ${TGT_CONTAINER:-} ]]; then
  : "${SRC_CONTAINER:?}" "${TGT_IMAGE:?}"
  MODE=docker
  export FAKE_PG_CONTAINER="$TGT_CONTAINER" FAKE_DB_IMAGE="$TGT_IMAGE" NETMON_DB_NAME=netmon NETMON_DB_USER=netmon
  SRC=netmon
  src_sql() { local db=$1; shift; docker exec -i "$SRC_CONTAINER" psql -X -q -A -t -v ON_ERROR_STOP=1 -U netmon -d "$db" "$@"; }
  src_dump() { docker exec "$SRC_CONTAINER" pg_dump -U netmon -d "$1" -Fc >"$2" 2>/dev/null; }
  tgt_sql() { docker exec -i "$TGT_CONTAINER" psql -X -q -A -t -v ON_ERROR_STOP=1 -U netmon -d "${2:-netmon}" -c "$1"; }
  admin_db=netmon
  cleanup() { rm -rf "$T"; }
else
  MODE=local
  : "${PGHOST:?}" "${PGUSER:?}" "${OLD_TS:?set OLD_TS to an installed TimescaleDB version older than the default}"
  export PGHOST PGUSER PGPASSWORD PGPORT
  id="$$"
  SRC="rx_src_$id"
  export FAKE_DB_IMAGE="timescale/timescaledb:local-pg16" NETMON_DB_NAME="rx_tgt_$id" NETMON_DB_USER="$PGUSER"
  src_sql() { local db=$1; shift; psql -X -q -A -t -v ON_ERROR_STOP=1 -d "$db" "$@"; }
  src_dump() { pg_dump -d "$1" -Fc -f "$2" 2>/dev/null; }
  tgt_sql() { psql -X -q -A -t -v ON_ERROR_STOP=1 -d "${2:-$NETMON_DB_NAME}" -c "$1"; }
  admin_db=postgres
  cleanup() {
    for d in "$SRC" "${SRC}_nover" "${SRC}_future" "$NETMON_DB_NAME"; do
      psql -X -q -d postgres -c "DROP DATABASE IF EXISTS \"$d\" WITH (FORCE)" 2>/dev/null || true
    done
    rm -rf "$T"
  }
  for d in "$SRC" "${SRC}_nover" "${SRC}_future"; do
    psql -X -q -v ON_ERROR_STOP=1 -d postgres -c "CREATE DATABASE \"$d\" TEMPLATE template0"
    psql -X -q -v ON_ERROR_STOP=1 -d "$d" -c "CREATE EXTENSION timescaledb VERSION '$OLD_TS'"
  done
fi
trap cleanup EXIT
if [[ $MODE == docker ]]; then
  for d in "${SRC}_future" "${SRC}_nover"; do
    src_sql "$SRC" -c "CREATE DATABASE ${d}" >/dev/null
    src_sql "$d" -c "CREATE EXTENSION IF NOT EXISTS timescaledb" >/dev/null
  done
fi

RESTORE="$PROD_SRC/ops/restore.sh"
DEFAULT_TS="$(tgt_sql "SELECT default_version FROM pg_available_extensions WHERE name = 'timescaledb'" "$admin_db")"
OLD="$(src_sql "$SRC" -c "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")"
echo "# mode=$MODE dump written by TimescaleDB $OLD, target default $DEFAULT_TS"
[[ -n $OLD && $OLD != "$DEFAULT_TS" ]] || fail "the source must be on an older TimescaleDB than the target ($OLD vs $DEFAULT_TS)"

# --- a source database shaped like NetworkMonitor's (hypertable, compression, retention,
# continuous aggregate, policies, compressed chunks)
src_sql "$SRC" >/dev/null <<'SQL'
SET client_min_messages = error;
CREATE TABLE measurements (time timestamptz NOT NULL, sensor_id int NOT NULL, channel int NOT NULL, value float8);
SELECT create_hypertable('measurements', 'time', chunk_time_interval => INTERVAL '1 day');
INSERT INTO measurements
  SELECT t, s, 1, s * 10 + extract(epoch FROM t)::bigint % 7
  FROM generate_series(now() - INTERVAL '20 days', now(), INTERVAL '5 minutes') t, generate_series(1, 3) s;
ALTER TABLE measurements SET (timescaledb.compress, timescaledb.compress_segmentby = 'sensor_id, channel');
SELECT add_compression_policy('measurements', INTERVAL '7 days');
SELECT add_retention_policy('measurements', INTERVAL '30 days');
CREATE MATERIALIZED VIEW measurements_hourly WITH (timescaledb.continuous) AS
  SELECT time_bucket('1 hour', time) AS bucket, sensor_id, avg(value) AS avg_value
  FROM measurements GROUP BY 1, 2 WITH NO DATA;
SELECT add_continuous_aggregate_policy('measurements_hourly', start_offset => INTERVAL '3 days',
  end_offset => INTERVAL '1 hour', schedule_interval => INTERVAL '1 hour');
CALL refresh_continuous_aggregate('measurements_hourly', NULL, NULL);
SELECT count(compress_chunk(c)) FROM show_chunks('measurements', older_than => INTERVAL '8 days') c;
SQL
fingerprint() { # sql-runner db -> rows|sum|compressed chunks|cagg rows|policy jobs
  "$@" "SELECT (SELECT count(*) || '|' || round(sum(value)) FROM measurements) || '|' ||
    (SELECT count(*) FROM timescaledb_information.chunks WHERE hypertable_name = 'measurements' AND is_compressed) || '|' ||
    (SELECT count(*) FROM measurements_hourly) || '|' ||
    (SELECT string_agg(proc_name, ',' ORDER BY proc_name) FROM timescaledb_information.jobs WHERE job_id >= 1000)"
}
src_fp() { src_sql "$SRC" -c "$1"; }
WANT="$(fingerprint src_fp)"
echo "# source: $WANT"
src_dump "$SRC" "$T/old.dump"

# --- the target: an existing database with other content (it gets replaced)
if [[ $MODE == local ]]; then
  psql -X -q -v ON_ERROR_STOP=1 -d postgres -c "CREATE DATABASE \"$NETMON_DB_NAME\""
fi
tgt_sql "CREATE TABLE IF NOT EXISTS marker (x int); INSERT INTO marker VALUES (1)"

run_restore() { # expected-exit args...
  local want=$1 rc=0
  shift
  "$RESTORE" "$@" >"$T/out" 2>&1 || rc=$?
  [[ $rc == "$want" ]] || { cat "$T/out" >&2; fail "restore.sh $* exited $rc, expected $want"; }
}
marker_intact() { [[ "$(tgt_sql "SELECT count(*) FROM marker" 2>/dev/null)" == 1 ]]; }

# 1. a dump whose TimescaleDB version the image does not ship: stop before touching anything
src_sql "${SRC}_future" -c "UPDATE _timescaledb_catalog.metadata SET value = '9.9.9' WHERE key = 'timescaledb_version'" >/dev/null
src_dump "${SRC}_future" "$T/future.dump"
: >"$FAKE_STATE/calls"
run_restore 1 --dump "$T/future.dump" --yes
grep -q "written by TimescaleDB 9.9.9" "$T/out" || { cat "$T/out"; fail "no clear message"; }
grep -q "timescale/timescaledb:9.9.9-pg16" "$T/out" || { cat "$T/out"; fail "message does not name the image tag to use"; }
marker_intact || fail "the target database was touched"
grep -qE ' (stop|up) ' "$FAKE_STATE/calls" && fail "services were stopped/started"
ls "$NETMON_BACKUP_DIR"/pre-restore-* >/dev/null 2>&1 && fail "a safety dump was taken"
ok "dump from a TimescaleDB version the image does not ship: refused before anything is touched, names the image"

# 2. a dump that does not record its version: --ts-version needed (and checked against the image)
src_sql "${SRC}_nover" -c "DELETE FROM _timescaledb_catalog.metadata WHERE key = 'timescaledb_version'" >/dev/null
src_dump "${SRC}_nover" "$T/nover.dump"
run_restore 1 --dump "$T/nover.dump" --yes
grep -q -- "--ts-version" "$T/out" || { cat "$T/out"; fail "no --ts-version hint"; }
marker_intact || fail "the target database was touched"
run_restore 1 --dump "$T/old.dump" --ts-version 9.9.9 --yes
grep -q "contradicts" "$T/out" || { cat "$T/out"; fail "--ts-version contradicting the dump accepted"; }
run_restore 0 --dump "$T/nover.dump" --ts-version "$OLD" --yes
grep -q "creating the TimescaleDB extension at the dump's version $OLD" "$T/out" || { cat "$T/out"; fail "--ts-version not used"; }
ok "dump without a recorded version: --ts-version required and used; a contradicting --ts-version refused"

# confirmation unchanged: anything but RESTORE aborts and leaves the database alone
tgt_sql "DELETE FROM marker; INSERT INTO marker VALUES (1)" >/dev/null 2>&1 || tgt_sql "CREATE TABLE marker (x int); INSERT INTO marker VALUES (1)"
rc=0
echo no | "$RESTORE" --dump "$T/old.dump" >"$T/out" 2>&1 || rc=$?
if [[ $rc != 1 ]] || ! grep -q aborted "$T/out"; then cat "$T/out"; fail "no abort without RESTORE"; fi
marker_intact || fail "the target database was touched without confirmation"
ok "without typing RESTORE nothing happens"

# 3. the real cross-version restore
run_restore 0 --dump "$T/old.dump" --yes
sed 's/^/#   /' "$T/out"
grep -q "creating the TimescaleDB extension at the dump's version $OLD" "$T/out" || { cat "$T/out"; fail "not created at $OLD"; }
grep -q "updating the TimescaleDB extension $OLD -> $DEFAULT_TS" "$T/out" || { cat "$T/out"; fail "no update"; }
ls "$NETMON_BACKUP_DIR"/pre-restore-*.dump >/dev/null || fail "no safety dump"
got_ver="$(tgt_sql "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")"
[[ $got_ver == "$DEFAULT_TS" ]] || fail "extension at $got_ver, expected $DEFAULT_TS"
marker_intact && fail "the old target content survived"
tgt_fp() { tgt_sql "$1"; }
GOT="$(fingerprint tgt_fp)"
[[ $GOT == "$WANT" ]] || fail "data differs: $GOT vs $WANT"
[[ "$(tgt_sql "SELECT current_setting('timescaledb.restoring', true)")" != on ]] || fail "still in restoring mode"
ok "dump on $OLD restored under $DEFAULT_TS: created at $OLD, restored, updated to $DEFAULT_TS; data/compression/cagg/jobs identical ($GOT)"

# 4. the background jobs work after the update (retention, compression, continuous aggregate)
for j in $(tgt_sql "SELECT job_id FROM timescaledb_information.jobs WHERE job_id >= 1000 ORDER BY 1"); do
  tgt_sql "CALL run_job($j)" >/dev/null || fail "job $j failed"
done
tgt_sql "INSERT INTO measurements VALUES (now() - INTERVAL '2 hours', 9, 1, 1)" >/dev/null
ok "every policy job runs after the restore (CALL run_job)"

BEFORE5="$(fingerprint tgt_fp)" # incl. the row step 4 added; step 5's safety dump holds this
# 5. idempotent: restoring the same dump again gives the same result
run_restore 0 --dump "$T/old.dump" --yes
[[ "$(fingerprint tgt_fp)" == "$WANT" ]] || fail "second restore differs"
ok "restoring again (idempotent) gives the same database"

# 6. a dump already on the image's version (the safety dump step 5 took): no update step
dumps=("$NETMON_BACKUP_DIR"/pre-restore-*.dump) # names sort by time
same="${dumps[-1]}"
run_restore 0 --dump "$same" --yes
grep -q "create $DEFAULT_TS, restore\$" "$T/out" || { cat "$T/out"; fail "plan for a same-version dump"; }
grep -q "updating the TimescaleDB extension" "$T/out" && fail "updated a database already on $DEFAULT_TS"
[[ "$(fingerprint tgt_fp)" == "$BEFORE5" ]] || fail "same-version restore differs: $(fingerprint tgt_fp) vs $BEFORE5"
ok "dump already on $DEFAULT_TS: created at $DEFAULT_TS, restored, no update"

echo "all $pass checks passed"
