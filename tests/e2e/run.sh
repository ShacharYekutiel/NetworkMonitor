#!/usr/bin/env bash
# One-command E2E run: fresh DB + built SPA + netmon serve + simulators -> Playwright -> teardown.
#
#   tests/e2e/run.sh [playwright args...]      e.g. tests/e2e/run.sh specs/04-http-alert.spec.ts
#
# Environment (all optional):
#   PG_ADMIN_URL   postgresql://postgres:postgres@localhost:5432/postgres  (needs CREATE DATABASE)
#   SKIP_BUILD=1   do not rebuild the frontend (use the existing backend/netmon/static or frontend/dist)
#   KEEP_DB=1      keep the e2e database after the run
#   SCREENSHOT_DIR where the screenshot spec writes PNGs (default tests/e2e/test-results/screenshots)
#   NETMON_DISABLE_TIMESCALE=1  run the backend in fallback mode
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PY="${PY:-$ROOT/backend/.venv/bin/python}"
NETMON="${NETMON:-$ROOT/backend/.venv/bin/netmon}"
PG_ADMIN_URL="${PG_ADMIN_URL:-postgresql://postgres:postgres@localhost:5432/postgres}"

free_port() { "$PY" -c 'import socket,sys; t=socket.SOCK_DGRAM if sys.argv[1]=="udp" else socket.SOCK_STREAM; s=socket.socket(socket.AF_INET,t); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])' "$1"; }

TAG="$(date +%s)_$$"
DB="netmon_e2e_${TAG}"
WORK="$(mktemp -d -t netmon-e2e-XXXXXX)"
LOGS="$HERE/test-results/logs"
mkdir -p "$LOGS"
APP_PORT="$(free_port tcp)"
HTTP_PORT="$(free_port tcp)"
HOOK_PORT="$(free_port tcp)"
SNMP_PORT="$(free_port udp)"
DB_URL="$("$PY" - "$PG_ADMIN_URL" "$DB" <<'EOF'
import sys
from urllib.parse import urlsplit, urlunsplit
p = urlsplit(sys.argv[1]); print(urlunsplit((p.scheme, p.netloc, "/" + sys.argv[2], p.query, "")))
EOF
)"

PIDS=()
cleanup() {
  local rc=$?
  for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done
  for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && wait "$pid" 2>/dev/null || true; done
  if [ "${KEEP_DB:-0}" != "1" ]; then
    "$PY" - "$PG_ADMIN_URL" "$DB" <<'EOF' || true
import asyncio, sys, asyncpg
async def main():
    c = await asyncpg.connect(sys.argv[1])
    await c.execute(f'DROP DATABASE IF EXISTS "{sys.argv[2]}" WITH (FORCE)')
    await c.close()
asyncio.run(main())
EOF
  fi
  rm -rf "$WORK"
  echo "e2e: logs in $LOGS (exit $rc)"
  exit $rc
}
trap cleanup EXIT INT TERM

echo "e2e: database $DB"
"$PY" - "$PG_ADMIN_URL" "$DB" <<'EOF'
import asyncio, sys, asyncpg
async def main():
    c = await asyncpg.connect(sys.argv[1])
    await c.execute(f'CREATE DATABASE "{sys.argv[2]}"')
    await c.close()
asyncio.run(main())
EOF

if [ "${SKIP_BUILD:-0}" != "1" ]; then
  echo "e2e: building frontend"
  (cd "$ROOT/frontend" && npm run --silent build:copy) >"$LOGS/frontend-build.log" 2>&1 \
    || { cat "$LOGS/frontend-build.log"; exit 1; }
  # copy-dist.mjs wipes backend/netmon/static including the tracked .gitkeep; restore it.
  touch "$ROOT/backend/netmon/static/.gitkeep"
fi

echo "e2e: simulators (snmp udp/$SNMP_PORT, http :$HTTP_PORT, webhook :$HOOK_PORT)"
"$PY" "$ROOT/tests/sim/snmp_agent.py" --port "$SNMP_PORT" >"$LOGS/snmpsim.log" 2>&1 & PIDS+=($!)
"$PY" "$ROOT/tests/sim/http_target.py" --port "$HTTP_PORT" >"$LOGS/http_target.log" 2>&1 & PIDS+=($!)
"$PY" "$ROOT/tests/sim/webhook_receiver.py" --port "$HOOK_PORT" --out "$WORK/webhooks.jsonl" \
  >"$LOGS/webhook_receiver.log" 2>&1 & PIDS+=($!)

echo "e2e: netmon serve on :$APP_PORT"
DATABASE_URL="$DB_URL" NETMON_DATA_DIR="$WORK/data" NETMON_HOST=127.0.0.1 NETMON_PORT="$APP_PORT" \
  NETMON_LOG_LEVEL=info "$NETMON" serve >"$LOGS/backend.log" 2>&1 & PIDS+=($!)

wait_for() { # name, command...
  local name=$1; shift
  for _ in $(seq 1 120); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 0.5
  done
  echo "e2e: $name did not become ready"; tail -50 "$LOGS"/*.log; exit 1
}
wait_for backend curl -fsS "http://127.0.0.1:$APP_PORT/api/health"
wait_for http_target curl -fsS "http://127.0.0.1:$HTTP_PORT/health"
wait_for webhook_receiver curl -fsS "http://127.0.0.1:$HOOK_PORT/health"
wait_for snmpsim grep -q READY "$LOGS/snmpsim.log"

if [ ! -d "$HERE/node_modules/@playwright/test" ]; then
  (cd "$HERE" && npm install --silent)
fi

export BASE_URL="http://127.0.0.1:$APP_PORT"
export SNMP_PORT HTTP_TARGET_URL="http://127.0.0.1:$HTTP_PORT" WEBHOOK_URL="http://127.0.0.1:$HOOK_PORT"
export SCREENSHOT_DIR="${SCREENSHOT_DIR:-$HERE/test-results/screenshots}"
echo "e2e: running playwright against $BASE_URL"
set +e
(cd "$HERE" && npx playwright test "$@")
rc=$?
set -e
cp "$WORK/webhooks.jsonl" "$LOGS/webhooks.jsonl" 2>/dev/null || true
exit $rc
