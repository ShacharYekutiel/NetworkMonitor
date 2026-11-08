# shellcheck shell=bash
# Shared helpers for the NetworkMonitor production (GitOps) scripts. Sourced, not run.
#
# Every path can be overridden from the environment (used by tests/test-sync.sh).

# The clone location is recorded by bootstrap.sh (also read by the systemd units).
if [[ -z ${NETMON_REPO_DIR:-} && -r ${NETMON_ETC_DIR:-/etc/netmon-gitops}/gitops.env ]]; then
  NETMON_REPO_DIR="$(sed -n 's/^NETMON_REPO_DIR=//p' "${NETMON_ETC_DIR:-/etc/netmon-gitops}/gitops.env" | tail -n1)"
fi
NETMON_REPO_DIR="${NETMON_REPO_DIR:-/opt/netmon}"
NETMON_PROD_SUBDIR="${NETMON_PROD_SUBDIR:-deploy/production}"
NETMON_PROD_DIR="${NETMON_PROD_DIR:-$NETMON_REPO_DIR/$NETMON_PROD_SUBDIR}"
NETMON_BRANCH="${NETMON_BRANCH:-main}"
NETMON_ETC_DIR="${NETMON_ETC_DIR:-/etc/netmon-gitops}"
NETMON_STATE_DIR="${NETMON_STATE_DIR:-/var/lib/netmon-gitops}"
NETMON_BACKUP_DIR="${NETMON_BACKUP_DIR:-/var/backups/netmon}"
NETMON_LOCK_FILE="${NETMON_LOCK_FILE:-/run/netmon-gitops.lock}"
NETMON_LIB_DIR="${NETMON_LIB_DIR:-/usr/local/lib/netmon-gitops}"
NETMON_SYSTEMD_DIR="${NETMON_SYSTEMD_DIR:-/etc/systemd/system}"
NETMON_HEALTH_TIMEOUT="${NETMON_HEALTH_TIMEOUT:-300}"
NETMON_HEALTH_POLL="${NETMON_HEALTH_POLL:-5}"
DOCKER="${DOCKER:-docker}"
CURL="${CURL:-curl}"
CONNTRACK="${CONNTRACK:-conntrack}"
export NETMON_ETC_DIR

# Services that must be running (and healthy) after a deploy. `init` is a one-shot.
NETMON_SERVICES=(db netmon proxy)

# ------------------------------------------------------------------ logging
# Under systemd (JOURNAL_STREAM set) use sd-daemon priority prefixes so journald stores
# the right level; interactively print a timestamp instead.
_log() {
  local prio=$1
  shift
  if [[ -n "${JOURNAL_STREAM:-}" ]]; then
    printf '<%s>%s\n' "$prio" "$*" >&2
  else
    printf '%s %s\n' "$(date '+%F %T')" "$*" >&2
  fi
}
log() { _log 6 "$*"; }
warn() { _log 4 "WARNING: $*"; }
err() { _log 3 "ERROR: $*"; }
die() {
  err "$*"
  exit 1
}

require_root() {
  [[ ${NETMON_ALLOW_NON_ROOT:-0} == 1 || $EUID -eq 0 ]] || die "run as root (sudo)"
}

# ------------------------------------------------------------------ environment
# Outbound proxy for git / curl (written by bootstrap.sh only when a proxy was set).
load_proxy_env() {
  local f="$NETMON_ETC_DIR/proxy.env"
  if [[ -r $f ]]; then
    set -a
    # shellcheck disable=SC1090
    . "$f"
    set +a
  fi
}

# NETMON_FQDN (written by bootstrap.sh).
load_site_env() {
  local f="$NETMON_ETC_DIR/site.env"
  NETMON_FQDN="${NETMON_FQDN:-}"
  if [[ -r $f ]]; then
    NETMON_FQDN="$(sed -n 's/^NETMON_FQDN=//p' "$f" | tail -n1 | tr -d '"'"'"' \r')"
  fi
}

# Read KEY from an env file without executing it.
env_get() { # file key
  sed -n "s/^$2=//p" "$1" | tail -n1 | tr -d '"'"'"' \r'
}

# ------------------------------------------------------------------ docker compose
compose() {
  "$DOCKER" compose \
    --project-directory "$NETMON_PROD_DIR" \
    -f "$NETMON_PROD_DIR/compose.yaml" \
    --env-file "$NETMON_PROD_DIR/release.env" \
    "$@"
}

service_cid() { # service -> container id of the (first) container, may be empty
  compose ps -a -q "$1" 2>/dev/null | head -n1
}

# "healthy" / "unhealthy" / "starting" for containers with a healthcheck,
# otherwise the state ("running", "exited", ...), or "missing".
service_state() {
  local cid
  cid="$(service_cid "$1")"
  if [[ -z $cid ]]; then
    echo missing
    return
  fi
  "$DOCKER" inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo missing
}

service_running() {
  local cid
  cid="$(service_cid "$1")"
  [[ -n $cid ]] && [[ "$("$DOCKER" inspect -f '{{.State.Running}}' "$cid" 2>/dev/null)" == true ]]
}

running_image() { # service -> image reference the container was created from
  local cid
  cid="$(service_cid "$1")"
  [[ -n $cid ]] && "$DOCKER" inspect -f '{{.Config.Image}}' "$cid" 2>/dev/null
}

# [2b-3] A device that kept sending traps/syslog (UDP, fixed 5-tuple: syslog uses source
# port 514) while the netmon container was being replaced can keep a conntrack entry that
# points at the OLD container, so its messages vanish. After `up` recreated the netmon
# container, delete the UDP entries for the published ingest ports (best effort; needs the
# `conntrack` tool: apt install conntrack). $1 = netmon container id before `up`.
flush_ingest_conntrack() {
  local before=${1:-} after p
  after="$(service_cid netmon)"
  [[ -n $after && $after != "$before" ]] || return 0
  if ! command -v "$CONNTRACK" >/dev/null 2>&1; then
    log "netmon container recreated; conntrack not installed, skipping the UDP 162/514 conntrack flush"
    return 0
  fi
  for p in 162 514; do
    # Exit status 1 = nothing matched: not an error.
    "$CONNTRACK" -D -p udp --dport "$p" >/dev/null 2>&1 || true
  done
  log "netmon container recreated: cleared stale UDP conntrack entries for ports 162/514"
}

# Wait until every service is healthy and https://<fqdn>/api/health reports "ok".
wait_healthy() {
  local timeout=$NETMON_HEALTH_TIMEOUT
  local deadline=$((SECONDS + timeout)) svc st all body
  load_site_env
  while ((SECONDS < deadline)); do
    all=1
    for svc in "${NETMON_SERVICES[@]}"; do
      st="$(service_state "$svc")"
      [[ $st == healthy ]] || {
        all=0
        break
      }
    done
    if ((all)); then
      # Through Caddy, with the real name (SNI) resolved to this host; -k because the
      # cert may still be the temporary self-signed one.
      if body="$("$CURL" -fsSk --noproxy '*' --max-time 5 \
        --resolve "${NETMON_FQDN:-netmonitor}:443:127.0.0.1" \
        "https://${NETMON_FQDN:-netmonitor}/api/health" 2>/dev/null)" &&
        [[ $body =~ \"status\":[[:space:]]*\"ok\" ]]; then
        return 0
      fi
    fi
    sleep "$NETMON_HEALTH_POLL"
  done
  err "not healthy after ${timeout}s: $(for s in "${NETMON_SERVICES[@]}"; do printf '%s=%s ' "$s" "$(service_state "$s")"; done)"
  return 1
}

# ------------------------------------------------------------------ database dumps
# pg_dump (custom format) from the running db container into $1 (atomic, verified).
db_dump() {
  local dest=$1 tmp="$1.partial" cid
  cid="$(service_cid db)"
  service_running db || {
    err "db container is not running; cannot dump"
    return 1
  }
  mkdir -p "$(dirname "$dest")"
  chmod 0700 "$(dirname "$dest")" 2>/dev/null || true
  (
    umask 077
    "$DOCKER" exec "$cid" pg_dump -U netmon -d netmon -Fc >"$tmp"
  ) || {
    rm -f "$tmp"
    err "pg_dump failed"
    return 1
  }
  if [[ ! -s $tmp ]] || ! "$DOCKER" exec -i "$cid" pg_restore -l <"$tmp" >/dev/null; then
    rm -f "$tmp"
    err "dump $dest is empty or unreadable"
    return 1
  fi
  mv "$tmp" "$dest"
  log "database dump written: $dest ($(du -h "$dest" | cut -f1))"
}

# ------------------------------------------------------------------ notification
# Optional: /etc/netmon-gitops/notify.env with
#   NOTIFY_WEBHOOK_URL=https://api.telegram.org/bot<token>/sendMessage?chat_id=<id>
#   NOTIFY_FORMAT=form      # "form" (text=..., Telegram) or "json" ({"text": ...})
notify() {
  local msg f="$NETMON_ETC_DIR/notify.env" url fmt
  msg="[NetMonitor $(hostname -s)] $*"
  [[ -r $f ]] || return 0
  url="$(env_get "$f" NOTIFY_WEBHOOK_URL)"
  fmt="$(env_get "$f" NOTIFY_FORMAT)"
  [[ -n $url ]] || return 0
  if [[ ${fmt:-form} == json ]]; then
    local esc=${msg//\\/\\\\}
    esc=${esc//\"/\\\"}
    esc=${esc//$'\n'/\\n}
    "$CURL" -fsS --max-time 15 -H 'Content-Type: application/json' \
      -d "{\"text\": \"$esc\"}" "$url" >/dev/null 2>&1 || warn "notification failed"
  else
    "$CURL" -fsS --max-time 15 --data-urlencode "text=$msg" "$url" >/dev/null 2>&1 || warn "notification failed"
  fi
}
