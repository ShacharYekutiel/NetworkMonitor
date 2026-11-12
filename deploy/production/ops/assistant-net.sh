#!/usr/bin/env bash
# [3b] Network helper for the "Ask the network" assistant (Amazon Bedrock).
#
#   sudo ./ops/assistant-net.sh check [--region R]... [--host H|URL]... [--no-connect]
#   sudo ./ops/assistant-net.sh no-proxy [--restart] ENTRY...
#
# check     For every AWS host the assistant may use (bedrock-runtime, rolesanywhere,
#           bedrock-agent-runtime, bedrock control plane) in each region (default:
#           us-east-1 and il-central-1) plus every --host (e.g. the VPC endpoint DNS name
#           vpce-0abc...-xyz.bedrock-runtime.us-east-1.vpce.amazonaws.com, or the URL pasted
#           from the endpoint page), shows: DNS answer and address class (private/public),
#           whether the app would go DIRECT or through the PROXY according to HTTPS_PROXY /
#           NO_PROXY in /etc/netmon-gitops/app.env, and (unless --no-connect) whether an
#           HTTPS request gets an answer that way. Warns when a VPC endpoint name or a
#           private address would be sent to the proxy. Read-only.
#           The test runs on the host; the netmon container uses the same DNS and routes.
# no-proxy  Adds ENTRY (a host name, or a domain suffix with a leading dot such as
#           .vpce.amazonaws.com) to NO_PROXY in app.env, once, keeping the file 0600.
#           --restart recreates the netmon container so the new environment is used;
#           without it the command to do so is printed.
#
# The assistant form also has "Proxy: direct" (Settings -> Assistant), which bypasses the
# proxy for the assistant's AWS calls only; NO_PROXY is the alternative that keeps
# "Proxy: from environment". See README.md, "Assistant (Amazon Bedrock)".
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$SELF_DIR/lib.sh"

GETENT="${GETENT:-getent}"
APP_ENV="$NETMON_ETC_DIR/app.env"

usage() {
  sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

addr_class() { # ip -> private | public
  local ip=$1
  case "$ip" in
    10.* | 192.168.* | 127.* | 169.254.*) echo private; return ;;
    172.*)
      local o2=${ip#172.}
      o2=${o2%%.*}
      ((o2 >= 16 && o2 <= 31)) && { echo private; return; } ;;
    100.*)
      local o2=${ip#100.}
      o2=${o2%%.*}
      ((o2 >= 64 && o2 <= 127)) && { echo private; return; } ;;
    [fF][cCdD]* | [fF][eE]80:*) echo private; return ;;
  esac
  echo public
}

host_of() { # URL or host -> lower-case host
  local h=${1,,}
  h="${h#*://}"
  h="${h%%/*}"
  h="${h%%:*}"
  printf '%s' "$h"
}

app_env_get() { # key -> value from app.env (empty when absent)
  [[ -r $APP_ENV ]] || return 0
  env_get "$APP_ENV" "$1"
}

cmd_check() {
  local connect=1 r h
  local -a regions=() hosts=()
  while (($#)); do
    case "$1" in
      --region) regions+=("${2:?--region needs a value}"); shift 2 ;;
      --host) hosts+=("$(host_of "${2:?--host needs a value}")"); shift 2 ;;
      --no-connect) connect=0; shift ;;
      -h | --help) usage ;;
      *) die "unknown argument: $1 (see --help)" ;;
    esac
  done
  ((${#regions[@]})) || regions=(us-east-1 il-central-1)
  for r in "${regions[@]}"; do
    [[ $r =~ ^[a-z]{2}(-gov)?-[a-z]+-[0-9]$ ]] || die "not an AWS region: $r"
    hosts+=("bedrock-runtime.$r.amazonaws.com" "rolesanywhere.$r.amazonaws.com"
      "bedrock-agent-runtime.$r.amazonaws.com" "bedrock.$r.amazonaws.com")
  done

  local proxy no_proxy
  proxy="$(app_env_get HTTPS_PROXY)"
  [[ -n $proxy ]] || proxy="$(app_env_get https_proxy)"
  no_proxy="$(app_env_get NO_PROXY)"
  [[ -n $no_proxy ]] || no_proxy="$(app_env_get no_proxy)"
  echo "app.env:   $APP_ENV"
  if [[ -n $proxy ]]; then echo "proxy:     $(redact_url "$proxy")"; else echo "proxy:     none (the app connects directly)"; fi
  echo "NO_PROXY:  ${no_proxy:-(empty)}"
  echo "(applies with 'Proxy: from environment' in Settings -> Assistant; 'Proxy: direct' always connects directly)"
  echo

  local warnings=0 ip class route result code
  printf '%-64s %-16s %-8s %-7s %s\n' HOST ADDRESS CLASS ROUTE RESULT
  for h in "${hosts[@]}"; do
    [[ -n $h ]] || continue
    ip="$("$GETENT" ahostsv4 "$h" 2>/dev/null | awk 'NR==1 {print $1}')" || true
    [[ -n $ip ]] || ip="$("$GETENT" ahosts "$h" 2>/dev/null | awk 'NR==1 {print $1}')" || true
    if [[ -n $ip ]]; then class="$(addr_class "$ip")"; else ip="-" class="no-dns"; fi
    if [[ -n $proxy ]] && ! no_proxy_match "$h" "$no_proxy"; then route=proxy; else route=direct; fi
    result="-"
    if ((connect)); then
      local -a copt=(--noproxy '*')
      [[ $route == proxy ]] && copt=(--proxy "$proxy" --noproxy '')
      if code="$("$CURL" -sS -o /dev/null --max-time 10 --connect-timeout 5 -w '%{http_code}' \
        "${copt[@]}" "https://$h/" 2>&1)"; then
        result="HTTP $code (reachable)"
      else
        result="FAILED: $(tail -n1 <<<"$code" | cut -c1-80)"
      fi
    fi
    printf '%-64s %-16s %-8s %-7s %s\n' "$h" "$ip" "$class" "$route" "$result"
    if [[ $route == proxy && $h == *.vpce.amazonaws.com ]]; then
      echo "  WARNING: VPC endpoint name goes to the proxy (wrong path). Fix: $0 no-proxy .vpce.amazonaws.com"
      warnings=$((warnings + 1))
    elif [[ $route == proxy && $class == private ]]; then
      echo "  WARNING: resolves to a private address (VPC endpoint with private DNS) but goes to the proxy. Fix: $0 no-proxy $h"
      warnings=$((warnings + 1))
    elif [[ $route == direct && $class == public && -n $proxy ]]; then
      echo "  note: public address, direct (no proxy) - works only if the server may reach AWS public endpoints directly"
    fi
  done
  echo
  if ((warnings)); then
    echo "$warnings warning(s)."
  else
    echo "No routing problems found. The hosts you do not use (e.g. the other region) may fail - that is fine."
  fi
}

cmd_no_proxy() {
  local restart=0 e
  local -a add=()
  while (($#)); do
    case "$1" in
      --restart) restart=1; shift ;;
      -h | --help) usage ;;
      -*) die "unknown option: $1" ;;
      *)
        e="${1,,}"
        e="$(host_of "$e")"
        [[ $e =~ ^(\*?\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$ ]] ||
          die "not a host name or .domain suffix: $1"
        add+=("$e")
        shift
        ;;
    esac
  done
  ((${#add[@]})) || die "usage: assistant-net.sh no-proxy [--restart] ENTRY..."
  require_root
  if [[ ! -r $APP_ENV ]] || [[ -z "$(app_env_get HTTPS_PROXY)$(app_env_get https_proxy)" ]]; then
    echo "No HTTPS_PROXY in $APP_ENV: the app already connects directly. Nothing to change."
    return 0
  fi

  local tmp="$APP_ENV.tmp" changed=0 key line cur new
  local -a seen=()
  (umask 077 && : >"$tmp")
  while IFS= read -r line || [[ -n $line ]]; do
    key="${line%%=*}"
    if [[ $line == *=* && ($key == NO_PROXY || $key == no_proxy) ]]; then
      seen+=("$key")
      cur="${line#*=}"
      cur="${cur//\"/}"
      new="$cur"
      for e in "${add[@]}"; do
        if [[ ",${new// /}," != *",$e,"* ]]; then
          new="${new:+$new,}$e"
          changed=1
        fi
      done
      line="$key=$new"
    fi
    printf '%s\n' "$line" >>"$tmp"
  done <"$APP_ENV"
  if ((${#seen[@]} == 0)); then
    local IFS=,
    printf 'NO_PROXY=localhost,127.0.0.1,::1,db,netmon,proxy,%s\n' "${add[*]}" >>"$tmp"
    unset IFS
    changed=1
  fi
  if ((changed == 0)); then
    rm -f "$tmp"
    echo "NO_PROXY in $APP_ENV already has: ${add[*]}"
    return 0
  fi
  chmod 0600 "$tmp"
  mv "$tmp" "$APP_ENV"
  echo "Updated $APP_ENV:"
  grep -E '^(NO_PROXY|no_proxy)=' "$APP_ENV" | sed 's/^/  /'
  if ((restart)); then
    local before
    before="$(service_cid netmon)"
    compose up -d netmon
    flush_ingest_conntrack "$before"
    echo "netmon recreated with the new environment."
  else
    echo "Apply with: cd $NETMON_PROD_DIR && sudo docker compose --env-file release.env up -d netmon"
    echo "        or: sudo $0 no-proxy --restart ${add[*]}"
  fi
}

case "${1:-}" in
  check) shift; cmd_check "$@" ;;
  no-proxy) shift; cmd_no_proxy "$@" ;;
  -h | --help | help) usage ;;
  *) usage 2 ;;
esac
