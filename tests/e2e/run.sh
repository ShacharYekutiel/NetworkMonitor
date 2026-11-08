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
#   NETMON_TEST_MODE is always 1 here (PUT /api/test/clock, docs/api.md [2b-1] Test clock).
#   NETMON_TEST_PTR_FILE=tests/sim/ports/ptr.json ([2b-2] reverse DNS test hook, no DNS stub needed).
#   Specs can restart the backend with $BACKEND_RESTART (same DB, data dir, port and env; waits for
#   /api/health) and control single lab components with "$LAB_SH swap|kill|revive NAME" (LAB_DIR exported).
#   LAB_EXTERNAL=1 do not start the Phase 2a discovery lab (tests/sim/lab.sh); use one that is already
#                  running (TELEGRAM_MOCK_URL, default http://127.0.0.1:18081)
#   E2E_PHASE3=0   do not start the phase-3 simulators (default 1; docs/architecture.md §13.11):
#                  SSH device lab (tests/sim/ssh/fake_device.py --lab, 127.0.0.111-.122 tcp/8022, control
#                  $SSH_SIM_URL), SMTP sink ($SMTP_PORT, $SMTP_SINK_URL), OIDC mock IdP ($OIDC_MOCK_URL ->
#                  NETMON_OIDC_AUTHORITY), Teams Workflows mock (https $TEAMS_MOCK_PORT, control $TEAMS_CONTROL_URL,
#                  CA via NETMON_EXTRA_CA_FILE), fake AD (ldaps $LDAPS_PORT, control $FAKE_AD_URL), MIT KDC when
#                  krb5kdc exists ($KDC_DIR, $KRB5_CONFIG for the browser), free UDP ports for syslog/traps
#                  ($SYSLOG_PORT, $TRAP_PORT) and flows ($NETFLOW_PORT, $IPFIX_PORT, $SFLOW_PORT).
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
TELEGRAM_PORT="$(free_port tcp)"
LAB_DIR="$WORK/lab"
E2E_PHASE3="${E2E_PHASE3:-1}"
if [ "$E2E_PHASE3" = "1" ]; then
  SYSLOG_PORT="$(free_port udp)"; TRAP_PORT="$(free_port udp)"
  NETFLOW_PORT="$(free_port udp)"; IPFIX_PORT="$(free_port udp)"; SFLOW_PORT="$(free_port udp)"
  SSH_CTL_PORT="$(free_port tcp)"; SMTP_PORT="$(free_port tcp)"; SMTP_HTTP_PORT="$(free_port tcp)"
  OIDC_PORT="$(free_port tcp)"; TEAMS_MOCK_PORT="$(free_port tcp)"; TEAMS_CTL_PORT="$(free_port tcp)"
  LDAPS_PORT="$(free_port tcp)"; LDAP_PORT="$(free_port tcp)"; AD_CTL_PORT="$(free_port tcp)"
fi
DB_URL="$("$PY" - "$PG_ADMIN_URL" "$DB" <<'EOF'
import sys
from urllib.parse import urlsplit, urlunsplit
p = urlsplit(sys.argv[1]); print(urlunsplit((p.scheme, p.netloc, "/" + sys.argv[2], p.query, "")))
EOF
)"

PIDS=()
cleanup() {
  local rc=$?
  if [ -f "$WORK/backend.pid" ]; then kill "$(cat "$WORK/backend.pid")" 2>/dev/null || true; fi
  for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done
  for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && wait "$pid" 2>/dev/null || true; done
  if [ "${LAB_EXTERNAL:-0}" != "1" ]; then
    LAB_DIR="$LAB_DIR" "$ROOT/tests/sim/lab.sh" stop >/dev/null 2>&1 || true
    mkdir -p "$LOGS/lab" && cp "$LAB_DIR"/logs/*.log "$LOGS/lab/" 2>/dev/null || true
  fi
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
  if [ -f "$WORK/p3/kdc/kdc.pid" ]; then kill "$(cat "$WORK/p3/kdc/kdc.pid")" 2>/dev/null || true; fi
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

# Phase 2a discovery lab: snmpsim agents on 127.0.0.21-24:1161, HTTP host 127.0.0.25:8080, printer
# tcp/9100, Telegram Bot API mock (docs/architecture.md §9.6), plus the Phase 2b-1 lab (§10.4): toggle
# HTTP targets 127.0.0.31-36:8080, fingerprint hosts 127.0.0.41-49/61-67, HPE/LLDP snmpsim 127.0.0.51-54.
# Phase 2b-2 (§11.6): port sims 127.0.0.81-86, vendor sims 127.0.0.71-73, synthetic 127.0.0.101-110.
# Fixed addresses, so only one lab per box.
if [ "${LAB_EXTERNAL:-0}" != "1" ]; then
  echo "e2e: discovery lab (tests/sim/lab.sh, telegram mock :$TELEGRAM_PORT)"
  LAB_DIR="$LAB_DIR" LAB_GENERIC=0 TELEGRAM_PORT="$TELEGRAM_PORT" PY="$PY" \
    "$ROOT/tests/sim/lab.sh" start >"$LOGS/lab.log" 2>&1 || { cat "$LOGS/lab.log"; exit 1; }
  TELEGRAM_MOCK_URL="http://127.0.0.1:$TELEGRAM_PORT"
else
  TELEGRAM_MOCK_URL="${TELEGRAM_MOCK_URL:-http://127.0.0.1:18081}"
fi

P3_ENV=""
if [ "$E2E_PHASE3" = "1" ]; then
  echo "e2e: phase-3 simulators (ssh lab ctl :$SSH_CTL_PORT, smtp :$SMTP_PORT, oidc :$OIDC_PORT, teams :$TEAMS_MOCK_PORT)"
  P3="$WORK/p3"; mkdir -p "$P3"
  # one CA for the Teams mock and the fake AD (both would otherwise create it concurrently)
  "$PY" -c "import sys; sys.path.insert(0, sys.argv[1]); from make_certs import make_certs; make_certs(sys.argv[2])" \
    "$ROOT/tests/sim/ldap" "$P3/certs" >/dev/null
  "$PY" "$ROOT/tests/sim/ssh/fake_device.py" --lab --state-dir "$P3/ssh" --control-port "$SSH_CTL_PORT" \
    >"$LOGS/ssh_sim.log" 2>&1 & PIDS+=($!)
  "$PY" "$ROOT/tests/sim/smtp_sink.py" --smtp-port "$SMTP_PORT" --http-port "$SMTP_HTTP_PORT" --out "$P3/mails.jsonl" \
    >"$LOGS/smtp_sink.log" 2>&1 & PIDS+=($!)
  "$PY" "$ROOT/tests/sim/oidc/mock_idp.py" --port "$OIDC_PORT" >"$LOGS/oidc_mock.log" 2>&1 & PIDS+=($!)
  "$PY" "$ROOT/tests/sim/teams_mock.py" --port "$TEAMS_MOCK_PORT" --control-port "$TEAMS_CTL_PORT" \
    --certs "$P3/certs" --out "$P3/teams.jsonl" >"$LOGS/teams_mock.log" 2>&1 & PIDS+=($!)
  "$PY" "$ROOT/tests/sim/ldap/fake_ad.py" --data "$ROOT/tests/sim/ldap/mnidom.yaml" --certs "$P3/certs" \
    --ldaps-port "$LDAPS_PORT" --ldap-port "$LDAP_PORT" --control-port "$AD_CTL_PORT" >"$LOGS/fake_ad.log" 2>&1 & PIDS+=($!)
  for _ in $(seq 1 100); do
    grep -q READY "$LOGS/teams_mock.log" 2>/dev/null && grep -q READY "$LOGS/ssh_sim.log" 2>/dev/null && break; sleep 0.2
  done
  KDC_DIR=""
  if command -v krb5kdc >/dev/null 2>&1 || [ -x /usr/sbin/krb5kdc ]; then
    if "$ROOT/tests/sim/kdc/setup_kdc.sh" start "$P3/kdc" >"$P3/kdc.env" 2>"$LOGS/kdc.log"; then
      KDC_DIR="$P3/kdc"
    else
      echo "e2e: KDC failed to start (spec 56 will skip); see $LOGS/kdc.log"
    fi
  fi
  P3_ENV="export NETMON_SYSLOG_PORT=$SYSLOG_PORT NETMON_TRAP_PORT=$TRAP_PORT NETMON_INGEST_HOST=127.0.0.1
export NETMON_NETFLOW_PORT=$NETFLOW_PORT NETMON_IPFIX_PORT=$IPFIX_PORT NETMON_SFLOW_PORT=$SFLOW_PORT NETMON_FLOW_HOST=127.0.0.1
export NETMON_OIDC_AUTHORITY=http://127.0.0.1:$OIDC_PORT NETMON_EXTRA_CA_FILE=$P3/certs/ca.pem"
fi

echo "e2e: netmon serve on :$APP_PORT (NETMON_TEST_MODE=1)"
# The backend runs from a script so specs can restart it ($BACKEND_RESTART, used by the escalation
# persistence spec). The current pid is in $WORK/backend.pid; the log is appended across restarts.
cat >"$WORK/backend.sh" <<BACKEND_EOF
#!/usr/bin/env bash
export DATABASE_URL="$DB_URL" NETMON_DATA_DIR="$WORK/data" NETMON_HOST=127.0.0.1 NETMON_PORT="$APP_PORT"
export NETMON_LOG_LEVEL=info NETMON_TELEGRAM_API_BASE="$TELEGRAM_MOCK_URL" NETMON_TEST_MODE=1
export NETMON_TEST_PTR_FILE="$ROOT/tests/sim/ports/ptr.json"
$P3_ENV
exec "$NETMON" serve >>"$LOGS/backend.log" 2>&1
BACKEND_EOF
cat >"$WORK/restart_backend.sh" <<RESTART_EOF
#!/usr/bin/env bash
# Stops the e2e backend (SIGTERM, graceful) and starts it again; exits 0 once /api/health answers.
set -u
pid=\$(cat "$WORK/backend.pid" 2>/dev/null || true)
if [ -n "\$pid" ]; then
  kill -TERM "\$pid" 2>/dev/null || true
  for _ in \$(seq 1 100); do kill -0 "\$pid" 2>/dev/null || break; sleep 0.2; done
  kill -KILL "\$pid" 2>/dev/null || true
fi
echo "=== e2e: backend restart \$(date -u +%FT%TZ) ===" >>"$LOGS/backend.log"
bash "$WORK/backend.sh" </dev/null >/dev/null 2>&1 &
echo \$! >"$WORK/backend.pid"
for _ in \$(seq 1 120); do
  curl -fsS -o /dev/null "http://127.0.0.1:$APP_PORT/api/health" 2>/dev/null && exit 0
  sleep 0.5
done
echo "backend did not come back" >&2; exit 1
RESTART_EOF
chmod +x "$WORK/backend.sh" "$WORK/restart_backend.sh"
: >"$LOGS/backend.log"
bash "$WORK/backend.sh" </dev/null >/dev/null 2>&1 &
echo $! >"$WORK/backend.pid"

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
if [ "$E2E_PHASE3" = "1" ]; then
  wait_for ssh_sim curl -fsS "http://127.0.0.1:$SSH_CTL_PORT/health"
  wait_for smtp_sink curl -fsS "http://127.0.0.1:$SMTP_HTTP_PORT/health"
  wait_for oidc_mock curl -fsS "http://127.0.0.1:$OIDC_PORT/health"
  wait_for teams_mock curl -fsS "http://127.0.0.1:$TEAMS_CTL_PORT/health"
  wait_for fake_ad curl -fsS "http://127.0.0.1:$AD_CTL_PORT/health"
fi

if [ ! -d "$HERE/node_modules/@playwright/test" ]; then
  (cd "$HERE" && npm install --silent)
fi

export BASE_URL="http://127.0.0.1:$APP_PORT"
export SNMP_PORT HTTP_TARGET_URL="http://127.0.0.1:$HTTP_PORT" WEBHOOK_URL="http://127.0.0.1:$HOOK_PORT"
export SCREENSHOT_DIR="${SCREENSHOT_DIR:-$HERE/test-results/screenshots}"
export TELEGRAM_MOCK_URL DISCOVERY_SNMP_PORT=1161 BACKEND_LOG="$LOGS/backend.log" SIM_DIR="$ROOT/tests/sim" PY
export BACKEND_RESTART="$WORK/restart_backend.sh" LAB_SH="$ROOT/tests/sim/lab.sh" LAB_DIR TELEGRAM_PORT
export QA2B1_SHOT_DIR="${QA2B1_SHOT_DIR:-$SCREENSHOT_DIR/qa-2b1}"
export QA2B2_SHOT_DIR="${QA2B2_SHOT_DIR:-$SCREENSHOT_DIR/qa-2b2}"
# [2b-2] specs write user template files into ${NETMON_DATA_DIR}/templates and reload (spec 34).
export E2E_DATA_DIR="$WORK/data"
export QA3_SHOT_DIR="${QA3_SHOT_DIR:-$SCREENSHOT_DIR/qa-3}"
if [ "$E2E_PHASE3" = "1" ]; then
  export SYSLOG_PORT TRAP_PORT NETFLOW_PORT IPFIX_PORT SFLOW_PORT SMTP_PORT LDAPS_PORT TEAMS_MOCK_PORT
  export SSH_SIM_URL="http://127.0.0.1:$SSH_CTL_PORT" SMTP_SINK_URL="http://127.0.0.1:$SMTP_HTTP_PORT"
  export OIDC_MOCK_URL="http://127.0.0.1:$OIDC_PORT" TEAMS_CONTROL_URL="http://127.0.0.1:$TEAMS_CTL_PORT"
  export FAKE_AD_URL="http://127.0.0.1:$AD_CTL_PORT" FAKE_AD_CA="$P3/certs/ca.pem" P3_DIR="$P3"
  if [ -n "$KDC_DIR" ]; then
    export KDC_DIR KRB5_CONFIG="$KDC_DIR/krb5.conf" KDC_KEYTAB="$KDC_DIR/http.keytab"
  fi
fi
echo "e2e: running playwright against $BASE_URL"
set +e
(cd "$HERE" && npx playwright test "$@")
rc=$?
set -e
cp "$WORK/webhooks.jsonl" "$LOGS/webhooks.jsonl" 2>/dev/null || true
if [ "$E2E_PHASE3" = "1" ]; then
  cp "$P3/ssh/commands.jsonl" "$LOGS/ssh_commands.jsonl" 2>/dev/null || true
  cp "$P3/mails.jsonl" "$LOGS/mails.jsonl" 2>/dev/null || true
  cp "$P3/teams.jsonl" "$LOGS/teams.jsonl" 2>/dev/null || true
  [ -n "${KDC_DIR:-}" ] && "$ROOT/tests/sim/kdc/setup_kdc.sh" stop "$KDC_DIR" >/dev/null 2>&1 || true
fi
exit $rc
