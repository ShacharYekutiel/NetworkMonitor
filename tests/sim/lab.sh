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
#   tests/sim/lab.sh swap NAME [DATA_DIR]   restart one SNMP component, optionally with other data
#                                           (e.g. swap snmp-access-2 tests/sim/hpe/access2_noprinter)
#   tests/sim/lab.sh kill NAME / revive NAME   stop / start one component (outage simulation)
#
# Inventory (fixed addresses; scans use options.snmp_port = 1161):
#   127.0.0.21  udp/1161  OPNsense      community public    tests/sim/discovery/opnsense/public.snmprec
#   127.0.0.22  udp/1161  UniFi U6-Lite community public    tests/sim/discovery/unifi_ap/public.snmprec
#   127.0.0.23  udp/1161  Brother       community printers  tests/sim/discovery/printer/printers.snmprec
#   127.0.0.23  tcp/9100  printer raw port (accept + close)
#   127.0.0.24  udp/1161  Cisco 2960-X  community public    tests/sim/discovery/switch/public.snmprec
#   127.0.0.25  tcp/8080  HTTP-only host (tests/sim/http_target.py, GET / -> 200)
#   127.0.0.1   tcp/$TELEGRAM_PORT  Telegram Bot API mock (tests/sim/telegram_mock.py)
#   Phase 2b-1 (LAB_2B1=1, default; docs/architecture.md §10.4):
#   127.0.0.31-.36 tcp/8080  toggle HTTP targets (http_target.py: POST /fail, /ok): .31 parent, .32/.33
#                            children (dependencies), .34 maintenance, .35 acknowledge, .36 escalation
#   127.0.0.41-.49, .61-.67  fingerprint hosts (tests/sim/fingerprint/fp_hosts.py, one process, --list)
#   127.0.0.51  udp/1161  ArubaOS-Switch "core-sw"  tests/sim/hpe/arubaos/public.snmprec
#   127.0.0.52  udp/1161  Comware "access-1"        tests/sim/hpe/comware/public.snmprec
#   127.0.0.53  udp/1161  ArubaOS "access-2"        tests/sim/hpe/access2/public.snmprec (variant access2_noprinter)
#   127.0.0.54  udp/1161  UniFi AP + LLDP           tests/sim/hpe/unifi_lldp/public.snmprec
#   (all community public, v2c; 127.0.0.99 = the unmanaged LLDP neighbour, nothing listens there)
#   Phase 2b-2 (LAB_2B2=1, default; docs/architecture.md §11.6; all udp/1161, community public, v2c):
#   127.0.0.81  ArubaOS-Switch port sim "port-aos"  tests/sim/ports/arubaos (variant arubaos_nomac7)
#   127.0.0.82  Comware port sim "port-cw"          tests/sim/ports/comware  (LLDP: port 2 -> U6-Lite-Office .54)
#   127.0.0.83  Aruba CX port sim "port-cx"         tests/sim/ports/arubacx
#   127.0.0.84  ARP router (ipNetToPhysical)        tests/sim/ports/arp_router
#   127.0.0.85  legacy ARP (ipNetToMedia)           tests/sim/ports/arp_legacy
#   127.0.0.86  52-port ArubaOS "port-aos52"        tests/sim/ports/arubaos52
#   (the contract's .61-.65 collide with the 2b-1 fingerprint hosts, which spec 23 scans with SNMP on)
#   127.0.0.71  Check Point "gw-hq"   .72 ClusterXL member "gw-cl1"   .73 Aruba Instant "instant-vc"
#   127.0.0.101-.110  synthetic sims, one per new vendor template (tests/sim/vendors/synthetic/<id>)
#   Phase 2b-3 (LAB_2B3=1, default; docs/architecture.md §12.14; udp/1161, community public, v2c):
#   127.0.0.87  Comware LAG "lag-cw"  (Bridge-Aggregation1 = 49+50, ifStackTable)   tests/sim/ports/lag_cw
#   127.0.0.88  ArubaOS LAG "lag-aos" (Trk1 = 3+4, LAG-MIB)  tests/sim/ports/lag_aos  (contract .86 = port-aos52)
#   Phase 3 (LAB_3=1, default; docs/architecture.md §13.11; udp/1161, community public, v2c):
#   127.0.0.90  new-device access switch "newdev-access" tests/sim/newdev/run1 (variant run2); contract .88 is
#               already the 2b-3 lag-aos sim and .89 the 2b-3 SNMPv3 trap source (lib/lab2b3.ts SRC.v3dev). The SSH device lab (tests/sim/ssh, 127.0.0.111-.122
#               tcp/8022) is started by tests/e2e/run.sh, not here.
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
LAB_2B1="${LAB_2B1:-1}"
LAB_2B2="${LAB_2B2:-1}"
LAB_2B3="${LAB_2B3:-1}"
LAB_3="${LAB_3:-1}"
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
  if [ "$LAB_2B1" = "1" ]; then
    local h="$HERE/hpe" n
    for n in 31 32 33 34 35 36; do
      echo "http-$n|tcp|127.0.0.$n:8080|http://127.0.0.$n:8080/health|$PY $HERE/http_target.py --host 127.0.0.$n --port 8080"
    done
    echo "fingerprint|tcp|127.0.0.41:80|log|$PY $HERE/fingerprint/fp_hosts.py"
    echo "snmp-core-sw|udp|127.0.0.51:1161|log|$PY $HERE/snmp_agent.py --host 127.0.0.51 --port 1161 --data ${DATA_snmp_core_sw:-$h/arubaos}"
    echo "snmp-access-1|udp|127.0.0.52:1161|log|$PY $HERE/snmp_agent.py --host 127.0.0.52 --port 1161 --data ${DATA_snmp_access_1:-$h/comware}"
    echo "snmp-access-2|udp|127.0.0.53:1161|log|$PY $HERE/snmp_agent.py --host 127.0.0.53 --port 1161 --data ${DATA_snmp_access_2:-$h/access2}"
    echo "snmp-ap-lldp|udp|127.0.0.54:1161|log|$PY $HERE/snmp_agent.py --host 127.0.0.54 --port 1161 --data ${DATA_snmp_ap_lldp:-$h/unifi_lldp}"
  fi
  if [ "$LAB_2B2" = "1" ]; then
    local p="$HERE/ports" v="$HERE/vendors" ip name dir t
    while read -r ip name dir; do
      local var="DATA_${name//-/_}"
      echo "$name|udp|$ip:1161|log|$PY $HERE/snmp_agent.py --host $ip --port 1161 --data ${!var:-$dir}"
    done <<COMPONENTS_EOF
127.0.0.81 snmp-port-aos $p/arubaos
127.0.0.82 snmp-port-cw $p/comware
127.0.0.83 snmp-port-cx $p/arubacx
127.0.0.84 snmp-arp-router $p/arp_router
127.0.0.85 snmp-arp-legacy $p/arp_legacy
127.0.0.86 snmp-port-aos52 $p/arubaos52
127.0.0.71 snmp-checkpoint $v/checkpoint
127.0.0.72 snmp-cp-cluster $v/checkpoint_cluster
127.0.0.73 snmp-aruba-instant $v/aruba_instant
COMPONENTS_EOF
    local n=101
    for t in aruba_cx aruba_wifi checkpoint_gaia fortinet_fortigate cisco_ios paloalto_panos mikrotik_routeros apc_ups hpe_ilo dell_idrac; do
      echo "snmp-syn-${t//_/-}|udp|127.0.0.$n:1161|log|$PY $HERE/snmp_agent.py --host 127.0.0.$n --port 1161 --data $v/synthetic/$t"
      n=$((n + 1))
    done
  fi
  if [ "$LAB_2B3" = "1" ]; then
    echo "snmp-lag-cw|udp|127.0.0.87:1161|log|$PY $HERE/snmp_agent.py --host 127.0.0.87 --port 1161 --data ${DATA_snmp_lag_cw:-$HERE/ports/lag_cw}"
    echo "snmp-lag-aos|udp|127.0.0.88:1161|log|$PY $HERE/snmp_agent.py --host 127.0.0.88 --port 1161 --data ${DATA_snmp_lag_aos:-$HERE/ports/lag_aos}"
  fi
  if [ "$LAB_3" = "1" ]; then
    echo "snmp-newdev-access|udp|127.0.0.90:1161|log|$PY $HERE/snmp_agent.py --host 127.0.0.90 --port 1161 --data ${DATA_snmp_newdev_access:-$HERE/newdev/run1}"
  fi
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

component() { # name -> its line
  local name rest
  while IFS='|' read -r name rest; do
    if [ "$name" = "$1" ]; then echo "$name|$rest"; fi
  done < <(components)
}

stop_one() {
  local f="$PIDS_DIR/$1.pid" pid
  [ -f "$f" ] || return 0
  pid="$(cat "$f")"
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 25); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
  kill -KILL -- "-$pid" 2>/dev/null || true
  rm -f "$f"
}

start_one() { # name; DATA_<name> may override the data dir (SNMP components)
  local line name proto bind rd cmd
  line="$(component "$1")"
  [ -n "$line" ] || { echo "lab: unknown component $1" >&2; exit 2; }
  IFS='|' read -r name proto bind rd cmd <<<"$line"
  mkdir -p "$PIDS_DIR" "$LOG_DIR"
  if alive "$name" && ready "$name" "$rd"; then echo "lab: $name already running"; return 0; fi
  for _ in $(seq 1 50); do port_free "$proto" "$bind" && break; sleep 0.1; done
  : >"$LOG_DIR/$name.log"
  # shellcheck disable=SC2086
  setsid $cmd >"$LOG_DIR/$name.log" 2>&1 < /dev/null &
  echo $! >"$PIDS_DIR/$name.pid"
  for _ in $(seq 1 150); do
    if ready "$name" "$rd"; then echo "lab: $name ready ($proto $bind)"; return 0; fi
    alive "$name" || break
    sleep 0.2
  done
  echo "lab: $name FAILED" >&2; tail -20 "$LOG_DIR/$name.log" >&2; exit 1
}

do_swap() { # name [data dir]
  local var="DATA_${1//-/_}"
  stop_one "$1"
  if [ -n "${2:-}" ]; then export "$var=$(cd "$2" && pwd)"; fi
  start_one "$1"
}

do_status() {
  local rc=0 name proto bind rd cmd state
  while IFS='|' read -r name proto bind rd cmd; do
    if alive "$name"; then
      if ready "$name" "$rd"; then state="ready"; else state="alive, not ready"; rc=1; fi
      printf '%-20s %-4s %-22s pid %-8s %s\n' "$name" "$proto" "$bind" "$(cat "$PIDS_DIR/$name.pid")" "$state"
    else
      printf '%-20s %-4s %-22s %-12s %s\n' "$name" "$proto" "$bind" "-" "stopped"; rc=1
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
  swap) do_swap "${2:?name}" "${3:-}" ;;
  kill) stop_one "${2:?name}"; echo "lab: stopped ${2}" ;;
  revive) start_one "${2:?name}" ;;
  *) echo "usage: $0 start|stop|status|restart|env" >&2; exit 2 ;;
esac
