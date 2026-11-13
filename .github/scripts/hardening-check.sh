#!/usr/bin/env bash
# [1.4.2] CI: assert the container hardening of running compose services.
#   hardening-check.sh "<docker compose ... command>" service...
# For each service: cap_drop ALL, no-new-privileges, read-only root filesystem, a pids
# limit, no effective capability for its main process unless compose adds one back
# (proxy: NET_BIND_SERVICE only), and a write to the root filesystem fails.
set -euo pipefail
COMPOSE=$1
shift
# capability bit masks (linux/capability.h): NET_BIND_SERVICE = bit 10
declare -A ALLOWED_EFF=([proxy]=0000000000000400)
for svc in "$@"; do
  # shellcheck disable=SC2086 # $COMPOSE is a command line
  cid="$($COMPOSE ps -q "$svc")"
  [[ -n $cid ]] || { echo "$svc: not running" >&2; exit 1; }
  docker inspect -f "$svc: user={{.Config.User}} cap_drop={{json .HostConfig.CapDrop}} cap_add={{json .HostConfig.CapAdd}} read_only={{.HostConfig.ReadonlyRootfs}} security_opt={{json .HostConfig.SecurityOpt}} pids={{.HostConfig.PidsLimit}} memory={{.HostConfig.Memory}}" "$cid"
  [[ "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$cid")" == true ]] || { echo "$svc: root fs not read-only" >&2; exit 1; }
  docker inspect -f '{{json .HostConfig.CapDrop}}' "$cid" | grep -q '"ALL"' || { echo "$svc: cap_drop ALL missing" >&2; exit 1; }
  docker inspect -f '{{json .HostConfig.SecurityOpt}}' "$cid" | grep -q 'no-new-privileges' || { echo "$svc: no-new-privileges missing" >&2; exit 1; }
  [[ "$(docker inspect -f '{{.HostConfig.PidsLimit}}' "$cid")" -gt 0 ]] || { echo "$svc: no pids limit" >&2; exit 1; }
  # shellcheck disable=SC2086
  eff="$($COMPOSE exec -T "$svc" sh -c 'grep CapEff /proc/1/status' | awk '{print $2}')"
  want="${ALLOWED_EFF[$svc]:-0000000000000000}"
  [[ $eff == "$want" ]] || { echo "$svc: CapEff=$eff, expected $want" >&2; exit 1; }
  # shellcheck disable=SC2086
  if $COMPOSE exec -T "$svc" sh -c 'echo x > /etc/ci-write-test' 2>/dev/null; then
    echo "$svc: the root filesystem is writable" >&2
    exit 1
  fi
  echo "$svc: hardened (CapEff=$eff)"
done
