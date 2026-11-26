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
# The application database and its role (the POSTGRES_USER of the db service, a superuser).
NETMON_DB_NAME="${NETMON_DB_NAME:-netmon}"
NETMON_DB_USER="${NETMON_DB_USER:-netmon}"
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

# [3b] NO_PROXY matching with the rules of httpx (the app's HTTP client): "*" = everything;
# ".example.com" or "*.example.com" = subdomains of example.com; "example.com" =
# example.com and its subdomains. Scheme and :port are ignored; an IP entry matches only
# that literal address.
no_proxy_match() { # host no_proxy_list -> 0 when the host bypasses the proxy
  local host=${1,,} list=${2:-} e
  local -a entries
  IFS=',' read -ra entries <<<"$list"
  for e in "${entries[@]}"; do
    e="${e//[[:space:]]/}"
    e="${e,,}"
    [[ -n $e ]] || continue
    [[ $e == '*' ]] && return 0
    e="${e#*://}"
    [[ $e =~ ^(.*[^:]):[0-9]+$ && $e != *::* ]] && e="${BASH_REMATCH[1]}"
    [[ $e == '*.'* ]] && e="${e:1}"
    if [[ $e == .* ]]; then
      [[ $host == *"$e" ]] && return 0
    else
      [[ $host == "$e" || $host == *".$e" ]] && return 0
    fi
  done
  return 1
}

# A proxy URL for display: credentials (user:password@) hidden.
redact_url() {
  if [[ $1 == *@* ]]; then printf '%s://***@%s' "${1%%://*}" "${1##*@}"; else printf '%s' "$1"; fi
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
# points at the OLD container, so its messages vanish. [3] Flow exporters are the worst case
# (a datagram every few seconds from one fixed source port, so the entry never times out).
# After `up` recreated the netmon container, delete the UDP entries for the published ingest
# ports (best effort; needs the `conntrack` tool: apt install conntrack).
# $1 = netmon container id before `up`.
# UDP ports published by compose.yaml for the ingest receivers (host side).
NETMON_INGEST_UDP_PORTS=(162 514 2055 4739 6343)
flush_ingest_conntrack() {
  local before=${1:-} after p
  after="$(service_cid netmon)"
  [[ -n $after && $after != "$before" ]] || return 0
  if ! command -v "$CONNTRACK" >/dev/null 2>&1; then
    log "netmon container recreated; conntrack not installed, skipping the UDP ${NETMON_INGEST_UDP_PORTS[*]} conntrack flush"
    return 0
  fi
  for p in "${NETMON_INGEST_UDP_PORTS[@]}"; do
    # Exit status 1 = nothing matched: not an error.
    "$CONNTRACK" -D -p udp --dport "$p" >/dev/null 2>&1 || true
  done
  log "netmon container recreated: cleared stale UDP conntrack entries for ports ${NETMON_INGEST_UDP_PORTS[*]}"
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
    "$DOCKER" exec "$cid" pg_dump -U "$NETMON_DB_USER" -d "$NETMON_DB_NAME" -Fc >"$tmp"
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

# ------------------------------------------------------------------ [1.5.1] TimescaleDB versions
# A newer timescale/timescaledb image ships the libraries and install scripts of the earlier
# releases too (it is built FROM the previous image), so it can load a database that is still
# on an older extension version and can CREATE EXTENSION ... VERSION '<older>'. An OLDER image
# cannot load a database whose extension was updated past its own version, and an extension
# update cannot be undone (Timescale supports a downgrade script for one step only).

# The db image the checked-out compose.yaml asks for: `image:` of services.db in the normalized
# `compose config` output. (Not `config --images db`: it also prints the images of db's
# dependencies (init: busybox), in no fixed order.)
compose_db_image() {
  compose config 2>/dev/null | awk '
    /^services:/ { s = 1; next }
    s && /^[^ ]/ { s = 0 }
    s && /^  db:$/ { d = 1; next }
    d && /^  [^ ]/ { d = 0 }
    d && /^    image: / { print $2; exit }'
}

# The timescaledb version installed in the application database of the running db container
# (empty: no extension, or the database cannot be read).
db_ts_version() {
  local cid
  cid="$(service_cid db)"
  [[ -n $cid ]] || return 0
  "$DOCKER" exec "$cid" psql -X -A -t -q -U "$NETMON_DB_USER" -d "$NETMON_DB_NAME" \
    -c "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'" 2>/dev/null | tr -d '[:space:]' || true
}

# What a db image ships, without starting PostgreSQL; with a dump file, also which timescaledb
# version wrote it (read from the dumped _timescaledb_catalog.metadata row
# "timescaledb_version", which TimescaleDB keeps equal to the installed version). One line each:
#   default=<v>  the version CREATE EXTENSION / ALTER EXTENSION ... UPDATE installs
#   lib=<v>      a version whose library is present (a database on <v> can be opened)
#   have=<v>     a version that can be created (library + install script)
#   dump_ts=1    the dump contains TimescaleDB catalog data;  dump=<v>  its version
# Exit status != 0 when the image could not be run (e.g. pull failed).
ts_probe() { # image [dump]
  local img=$1 dump=${2:-}
  # shellcheck disable=SC2016 # expanded by the container's sh
  local script='
    if [ "$1" = 1 ]; then
      out="$(pg_restore -a -n _timescaledb_catalog -t metadata -f - 2>/dev/null)" || echo dump_error=1
      case "$out" in *"COPY _timescaledb_catalog.metadata"*) echo dump_ts=1 ;; esac
      printf "%s\n" "$out" | awk -F "\t" "\$1 == \"timescaledb_version\" { print \"dump=\" \$2 }"
    fi
    lib="$(pg_config --pkglibdir)"; ext="$(pg_config --sharedir)/extension"
    sed -n "s/^default_version *= *.\([0-9.]*\).*/default=\1/p" "$ext/timescaledb.control"
    for f in "$lib"/timescaledb-[0-9]*.so; do
      [ -e "$f" ] || continue
      v="${f##*/timescaledb-}"; v="${v%.so}"
      echo "lib=$v"
      if [ -f "$ext/timescaledb--$v.sql" ]; then echo "have=$v"; fi
    done'
  if [[ -n $dump ]]; then
    "$DOCKER" run --rm -i --network none --entrypoint sh "$img" -c "$script" sh 1 <"$dump"
  else
    "$DOCKER" run --rm --network none --entrypoint sh "$img" -c "$script" sh 0 </dev/null
  fi
}

# "timescale/timescaledb:2.31.0-pg16" for version 2.31.0, keeping the PostgreSQL major of $2.
ts_image_for() { # version current-image
  local pg="pg16"
  [[ $2 =~ -(pg[0-9]+)$ ]] && pg="${BASH_REMATCH[1]}"
  printf 'timescale/timescaledb:%s-%s' "$1" "$pg"
}

# `compose up -d --remove-orphans`, except that the db container stays on the image it runs
# when the checked-out compose.yaml asks for a db image that cannot load the TimescaleDB
# version the database already uses (a rollback/drift repair to a revision with an older db
# image after the extension was updated: recreating db would make the database unusable).
# Returns 0 after a normal up, 1 when up failed; sets DB_KEPT to the kept image (else empty).
DB_KEPT=""
compose_up_guarded() {
  local want have v probe svcs=()
  DB_KEPT=""
  want="$(compose_db_image)"
  have="$(running_image db || true)"
  if [[ -n $want && -n $have && $want != "$have" ]] && service_running db; then
    v="$(db_ts_version)"
    # An image that cannot be inspected (pull failed) counts as "cannot load": keep db.
    [[ -z $v ]] || probe="$(ts_probe "$want" 2>/dev/null)" || probe=""
    if [[ -n $v ]] && ! grep -qx "lib=$v" <<<"${probe:-}"; then
      warn "compose.yaml asks for db image $want, which cannot load TimescaleDB $v (the version in the database); keeping db on $have"
      # shellcheck disable=SC2034 # read by sync.sh
      DB_KEPT="$have"
      mapfile -t svcs < <(compose config --services | grep -vx db)
      compose up -d --no-deps --remove-orphans "${svcs[@]}"
      return
    fi
  fi
  compose up -d --remove-orphans
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
