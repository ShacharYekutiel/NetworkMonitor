#!/usr/bin/env bash
# Phase 2a simulator lab (docs/architecture.md §9.6): one snmpsim agent per simulated device on its own
# loopback IP, an HTTP-only host, the printer's raw 9100 port, the Telegram Bot API mock, and (optionally)
# the Phase 1 simulators.
#
#   tests/sim/lab.sh start      start everything in the background, wait until ready, print the env
#   tests/sim/lab.sh stop       stop everything started by `start` (same LAB_DIR)
#   tests/sim/lab.sh status     per-component pid / alive / ready
#   tests/sim/lab.sh env        print the export lines for the backend and the specs
#   tests/sim/lab.sh restart
#
# Inventory (fixed addresses; scans use options.snmp_port = 1161):
#   127.0.0.21  udp/1161  OPNsense      community public    tests/sim/discovery/opnsense/public.snmprec
#   127.0.0.22  udp/1161  UniFi U6-Lite community public    tests/sim/discovery/unifi_ap/public.snmprec
#   127.0.0.23  udp/1161  Brother       community printers  tests/sim/discovery/printer/printers.snmprec
#   127.0.0.23  tcp/9100  printer raw port (accept + close)
#   127.0.0.24  udp/1161  Cisco 2960-X  community public    tests/sim/discovery/switch/public.snmprec
#   127.0.0.25  tcp/8080  HTTP-only host (tests/sim/http_target.py, GET / -> 200)
#   127.0.0.1   tcp/$TELEGRAM_PORT  Telegram Bot API mock (tests/sim/telegram_mock.py)
#   with LAB_GENERIC=1 (default): Phase 1 SNMP agent 127.0.0.1 udp/$LAB_SNMP_PORT, HTTP toggle target
#   127.0.0.1:$LAB_HTTP_PORT, webhook receiver 127.0.0.1:$LAB_HOOK_PORT.
#
# Environment:
#   LAB_DIR        state + logs (default ${TMPDIR:-/tmp}/netmon-lab)
#   PY             python with snmpsim (default backend/.venv/bin/python)
#   TELEGRAM_PORT  default 18081
#   LAB_GENERIC    1 = also start the Phase 1 simulators (run.sh sets 0; it starts its own on free ports)
#   LAB_SNMP_PORT (11161)  LAB_HTTP_PORT (18083)  LAB_HOOK_PORT (18082)
# Linux routes all of 127.0.0.0/8 to lo; on macOS add aliases first (sudo ifconfig lo0 alias 127.0.0.21 ...).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PY="${PY:-$ROOT/backend/.venv/bin/python}"
LAB_DIR="${LAB_DIR:-${TMPDIR:-/tmp}/netmon-lab}"
TELEGRAM_PORT="${TELEGRAM_PORT:-18081}"
LAB_GENERIC="${LAB_GENERIC:-1}"
LAB_SNMP_PORT="${LAB_SNMP_PORT:-11161}"
LAB_HTTP_PORT="${LAB_HTTP_PORT:-18083}"
LAB_HOOK_PORT="${LAB_HOOK_PORT:-18082}"
PIDS_DIR="$LAB_DIR/pids"
LOG_DIR="$LAB_DIR/logs"

# name|proto|bind|readiness ("log" = READY line in the log, else a URL for curl)|command...
components() {
  local d="$HERE/discovery"
  echo "snmp-opnsense|udp|127.0.0.21:1161|log|$PY $HERE/snmp_agent.py --host 127.0.0.21 --port 1161 --data $d/opnsense"
  echo "snmp-unifi-ap|udp|127.0.0.22:1161|log|$PY $HERE/snmp_agent.py --host 127.0.0.22 --port 1161 --data $d/unifi_ap"
  echo "snmp-printer|udp|127.0.0.23:1161|log|$PY $HERE/snmp_agent.py --host 127.0.0.23 --port 1161 --data $d/printer"
  echo "snmp-switch|udp|127.0.0.24:1161|log|$PY $HERE/snmp_agent.py --host 127.0.0.24 --port 1161 --data $d/switch"
  echo "printer-9100|tcp|127.0.0.23:9100|log|$PY $HERE/tcp_listener.py --host 127.0.0.23 --port 9100"
  echo "http-25|tcp|127.0.0.25:8080|http://127.0.0.25:8080/health|$PY $HERE/http_target.py --host 127.0.0.25 --port 8080"
  echo "telegram|tcp|127.0.0.1:$TELEGRAM_PORT|http://127.0.0.1:$TELEGRAM_PORT/health|$PY $HERE/telegram_mock.py --port $TELEGRAM_PORT"
  if [ "$LAB_GENERIC" = "1" ]; then
    echo "snmp-generic|udp|127.0.0.1:$LAB_SNMP_PORT|log|$PY $HERE/snmp_agent.py --port $LAB_SNMP_PORT"
    echo "http-target|tcp|127.0.0.1:$LAB_HTTP_PORT|http://127.0.0.1:$LAB_HTTP_PORT/health|$PY $HERE/http_target.py --port $LAB_HTTP_PORT"
    echo "webhook|tcp|127.0.0.1:$LAB_HOOK_PORT|http://127.0.0.1:$LAB_HOOK_PORT/health|$PY $HERE/webhook_receiver.py --port $LAB_HOOK_PORT --out $LAB_DIR/webhooks.jsonl"
  fi
}

alive() { [ -f "$PIDS_DIR/$1.pid" ] && kill -0 "$(cat "$PIDS_DIR/$1.pid")" 2>/dev/null; }

port_free() { # proto host:port
  "$PY" - "$1" "$2" <<'EOF'
import socket, sys
proto, hp = sys.argv[1], sys.argv[2]
host, port = hp.rsplit(":", 1)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if proto == "udp" else socket.SOCK_STREAM)
if proto == "tcp":  # ignore TIME_WAIT leftovers; Linux still refuses a port that has a listener
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind((host, int(port)))
except OSError:
    sys.exit(1)
finally:
    s.close()
EOF
}

ready() { # name readiness
  if [ "$2" = "log" ]; then grep -q READY "$LOG_DIR/$1.log" 2>/dev/null; else curl -fsS -o /dev/null "$2" 2>/dev/null; fi
}

print_env() {
  echo "export NETMON_TELEGRAM_API_BASE=http://127.0.0.1:$TELEGRAM_PORT"
  echo "export TELEGRAM_MOCK_URL=http://127.0.0.1:$TELEGRAM_PORT"
  echo "export DISCOVERY_SNMP_PORT=1161"
  if [ "$LAB_GENERIC" = "1" ]; then
    echo "export SNMP_PORT=$LAB_SNMP_PORT"
    echo "export HTTP_TARGET_URL=http://127.0.0.1:$LAB_HTTP_PORT"
    echo "export WEBHOOK_URL=http://127.0.0.1:$LAB_HOOK_PORT"
  fi
}

do_start() {
  mkdir -p "$PIDS_DIR" "$LOG_DIR"
  local busy=0 line name proto bind rd cmd
  while IFS='|' read -r name proto bind rd cmd; do
    if alive "$name"; then echo "lab: $name already running (pid $(cat "$PIDS_DIR/$name.pid"))"; busy=1; continue; fi
    if ! port_free "$proto" "$bind"; then echo "lab: $proto $bind is in use (another lab running? try: LAB_DIR=... $0 stop)" >&2; busy=2; fi
  done < <(components)
  [ "$busy" = "2" ] && exit 1
  while IFS='|' read -r name proto bind rd cmd; do
    alive "$name" && continue
    : >"$LOG_DIR/$name.log"
    # setsid: the component becomes its own process group, so stop also reaches snmpsim children.
    # shellcheck disable=SC2086
    setsid $cmd >"$LOG_DIR/$name.log" 2>&1 < /dev/null &
    echo $! >"$PIDS_DIR/$name.pid"
  done < <(components)
  local failed=0
  while IFS='|' read -r name proto bind rd cmd; do
    local ok=0
    for _ in $(seq 1 150); do
      if ready "$name" "$rd"; then ok=1; break; fi
      alive "$name" || break
      sleep 0.2
    done
    if [ "$ok" = 1 ]; then echo "lab: $name ready ($proto $bind)"; else
      echo "lab: $name FAILED ($proto $bind), log:" >&2; tail -20 "$LOG_DIR/$name.log" >&2; failed=1
    fi
  done < <(components)
  if [ "$failed" = 1 ]; then do_stop; exit 1; fi
  echo "lab: logs in $LOG_DIR"
  print_env
}

do_stop() {
  [ -d "$PIDS_DIR" ] || { echo "lab: nothing to stop ($PIDS_DIR missing)"; return 0; }
  local f pid name
  for f in "$PIDS_DIR"/*.pid; do
    [ -e "$f" ] || continue
    pid="$(cat "$f")"; name="$(basename "$f" .pid)"
    if kill -0 "$pid" 2>/dev/null; then
      kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    fi
    for _ in $(seq 1 25); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
    kill -KILL -- "-$pid" 2>/dev/null || true
    rm -f "$f"
    echo "lab: stopped $name"
  done
}

do_status() {
  local rc=0 name proto bind rd cmd state
  while IFS='|' read -r name proto bind rd cmd; do
    if alive "$name"; then
      if ready "$name" "$rd"; then state="ready"; else state="alive, not ready"; rc=1; fi
      printf '%-15s %-4s %-22s pid %-8s %s\n' "$name" "$proto" "$bind" "$(cat "$PIDS_DIR/$name.pid")" "$state"
    else
      printf '%-15s %-4s %-22s %-12s %s\n' "$name" "$proto" "$bind" "-" "stopped"; rc=1
    fi
  done < <(components)
  return $rc
}

case "${1:-}" in
  start) do_start ;;
  stop) do_stop ;;
  restart) do_stop; do_start ;;
  status) do_status ;;
  env) print_env ;;
  *) echo "usage: $0 start|stop|status|restart|env" >&2; exit 2 ;;
esac
