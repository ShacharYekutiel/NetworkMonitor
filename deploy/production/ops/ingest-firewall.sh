#!/usr/bin/env bash
# NetworkMonitor — OPTIONAL sender filter for SNMP traps / syslog (phase 2b-3) and flow
# export (NetFlow / IPFIX / sFlow, phase 3).
#
# Why: the netmon container publishes 162/udp, 514/udp, 514/tcp and [3] 2055/udp, 4739/udp,
# 6343/udp. Docker implements
# published ports with iptables DNAT + its own FORWARD chains, which are evaluated BEFORE
# ufw: `ufw deny 514` does NOT stop anyone from sending to NetworkMonitor. The supported
# place for your own rules is Docker's DOCKER-USER chain. This script keeps one chain,
# NETMON-INGEST, that accepts traps/syslog/flows only from the networks you list and drops
# the rest. The web UI (443 through Caddy) is not affected. One allow list covers all six
# ports (the device networks: switches, firewalls and other exporters).
#
# Nothing here is applied automatically. To use it:
#   1. List the device networks, one IPv4 address or CIDR per line ('#' comments allowed):
#        sudo install -m 0644 /dev/null /etc/netmon-gitops/ingest-allow.conf
#        sudo nano /etc/netmon-gitops/ingest-allow.conf        # e.g. 10.0.0.0/8
#   2. Apply now and at every boot (re-applied when Docker restarts):
#        sudo systemctl enable --now netmon-ingest-firewall.service
#      or once, by hand:  sudo /usr/local/lib/netmon-gitops/ingest-firewall.sh apply
#
# Usage: ingest-firewall.sh apply [CIDR ...]   (CIDRs on the command line replace the file)
#        ingest-firewall.sh remove
#        ingest-firewall.sh status
#
# Rule matched in DOCKER-USER (original destination port, so it works after Docker's DNAT
# 514 -> 1514; --ctstate DNAT limits it to traffic INTO published ports, so the app's own
# outgoing SIEM forwarding to a remote port 514 is never touched). Ports published 1:1
# (2055/4739/6343) go through the same DNAT, so the same match works for them:
#   -p udp -m conntrack --ctstate DNAT --ctdir ORIGINAL --ctorigdstport 514 -j NETMON-INGEST
# IPv4 only (Docker publishes IPv6 only when ip6tables is enabled in daemon.json).
# The app's own ignore list (Settings -> Logs -> Unknown sources) and, for flows, exporter
# acceptance (unknown exporters are dropped until an admin accepts them) are a second layer.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$SELF_DIR/lib.sh"

IPTABLES="${IPTABLES:-iptables}"
CHAIN=NETMON-INGEST
TAG=netmon-ingest
ALLOW_FILE="${NETMON_INGEST_ALLOW_FILE:-$NETMON_ETC_DIR/ingest-allow.conf}"
# protocol:port pairs published by compose.yaml (host side).
PORTS=(udp:162 udp:514 tcp:514 udp:2055 udp:4739 udp:6343)

usage() {
  sed -n '2,/^set -euo/{/^set -euo/d;p}' "$0"
  exit "${1:-0}"
}

valid_cidr() {
  local re='^((25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])(/([0-9]|[12][0-9]|3[0-2]))?$'
  [[ $1 =~ $re ]]
}

jump_rule() { # proto port -> the DOCKER-USER rule spec (without -A/-I/-D)
  printf '%s\n' -p "$1" -m conntrack --ctstate DNAT --ctdir ORIGINAL --ctorigdstport "$2" \
    -m comment --comment "$TAG" -j "$CHAIN"
}

read_allow_list() {
  [[ -r $ALLOW_FILE ]] || die "no networks given and $ALLOW_FILE does not exist (see: $0 --help)"
  sed -e 's/#.*//' -e 's/[[:space:]]//g' "$ALLOW_FILE" | grep -v '^$' || true
}

apply() {
  local cidrs=() c pp spec
  if (($#)); then cidrs=("$@"); else mapfile -t cidrs < <(read_allow_list); fi
  ((${#cidrs[@]})) || die "the allow list is empty: refusing to drop traps/syslog/flows from everyone"
  for c in "${cidrs[@]}"; do valid_cidr "$c" || die "not an IPv4 address/CIDR: '$c'"; done
  "$IPTABLES" -n -L DOCKER-USER >/dev/null 2>&1 || die "iptables chain DOCKER-USER not found (is Docker running?)"

  # Rebuild the filter chain atomically enough: new rules first, then swap the jumps.
  "$IPTABLES" -N "$CHAIN" 2>/dev/null || true
  "$IPTABLES" -F "$CHAIN"
  for c in "${cidrs[@]}"; do "$IPTABLES" -A "$CHAIN" -s "$c" -j RETURN; done
  "$IPTABLES" -A "$CHAIN" -j DROP
  for pp in "${PORTS[@]}"; do
    mapfile -t spec < <(jump_rule "${pp%%:*}" "${pp##*:}")
    "$IPTABLES" -C DOCKER-USER "${spec[@]}" 2>/dev/null || "$IPTABLES" -I DOCKER-USER 1 "${spec[@]}"
  done
  log "traps/syslog/flows accepted only from: ${cidrs[*]} (UDP 162, UDP/TCP 514, UDP 2055/4739/6343)"
}

remove() {
  local pp spec
  for pp in "${PORTS[@]}"; do
    mapfile -t spec < <(jump_rule "${pp%%:*}" "${pp##*:}")
    while "$IPTABLES" -D DOCKER-USER "${spec[@]}" 2>/dev/null; do :; done
  done
  "$IPTABLES" -F "$CHAIN" 2>/dev/null || true
  "$IPTABLES" -X "$CHAIN" 2>/dev/null || true
  log "traps/syslog/flows sender filter removed (every sender accepted again)"
}

status() {
  if ! "$IPTABLES" -n -L "$CHAIN" >/dev/null 2>&1; then
    echo "not active: every sender may send traps/syslog/flows (only the app's ignore list / exporter acceptance applies)"
    return 0
  fi
  echo "DOCKER-USER jumps:"
  "$IPTABLES" -S DOCKER-USER | grep -F -- "$TAG" || echo "  (none - the chain exists but is not used)"
  echo "$CHAIN:"
  "$IPTABLES" -v -n -L "$CHAIN"
}

cmd="${1:-}"
[[ -n $cmd ]] || usage 2
shift
case "$cmd" in
  -h | --help) usage 0 ;;
  apply)
    require_root
    apply "$@"
    ;;
  remove)
    require_root
    remove
    ;;
  status)
    require_root
    status
    ;;
  *) usage 2 ;;
esac
