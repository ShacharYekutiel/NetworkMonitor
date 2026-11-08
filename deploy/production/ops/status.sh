#!/usr/bin/env bash
# One-screen status of the production server:
#   sudo /opt/netmon/deploy/production/ops/status.sh
# deployed revision vs origin/main, pending/bad deploy/production changes, image version,
# container health, /api/health, last backups, certificate expiry, timers.
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$SELF_DIR/lib.sh"
require_root
load_site_env

h() { printf '\n== %s\n' "$*"; }
st() { cat "$NETMON_STATE_DIR/$1" 2>/dev/null || true; }
g() { git -C "$NETMON_REPO_DIR" "$@" 2>/dev/null; }

h "Revision"
dep_rev="$(st deployed_rev)" dep_tree="$(st deployed_tree)"
origin="$(g rev-parse -q --verify "refs/remotes/origin/$NETMON_BRANCH")"
origin_tree="$(g rev-parse -q --verify "$origin:$NETMON_PROD_SUBDIR")"
printf 'deployed:   %s  %s\n' "${dep_rev:-none}" "$( [[ -n $dep_rev ]] && g log -1 --format='%cd %s' --date=short "$dep_rev")"
printf 'origin/%s: %s  (fetched %s)\n' "$NETMON_BRANCH" "${origin:-?}" \
  "$(stat -c %y "$NETMON_REPO_DIR/.git/FETCH_HEAD" 2>/dev/null | cut -d. -f1 || echo never)"
if [[ -n $dep_tree && $dep_tree == "$origin_tree" ]]; then
  echo "deploy/production: up to date with origin/$NETMON_BRANCH"
elif grep -q "^$origin_tree " "$NETMON_STATE_DIR/bad_trees" 2>/dev/null; then
  echo "deploy/production: origin/$NETMON_BRANCH has a change that FAILED to deploy (marked bad):"
  grep "^$origin_tree " "$NETMON_STATE_DIR/bad_trees" | sed 's/^/  /'
  echo "  retry after fixing the server: sudo $NETMON_LIB_DIR/sync.sh --force"
else
  echo "deploy/production: a change is pending (deployed by the next sync run, within ~2 min)"
fi

h "Version"
echo "release.env (checkout): $(env_get "$NETMON_PROD_DIR/release.env" NETMON_IMAGE)"
echo "running netmon image:   $(running_image netmon || echo 'not running')"

h "Containers"
compose ps -a --format 'table {{.Service}}\t{{.State}}\t{{.Status}}\t{{.Image}}' 2>&1
for s in "${NETMON_SERVICES[@]}"; do printf '%s=%s ' "$s" "$(service_state "$s")"; done
echo

h "Health (https://${NETMON_FQDN:-?}/api/health)"
"$CURL" -sSk --noproxy '*' --max-time 5 --resolve "${NETMON_FQDN:-netmonitor}:443:127.0.0.1" \
  "https://${NETMON_FQDN:-netmonitor}/api/health" 2>&1 || true
echo

h "Traps / syslog (host 162/udp, 514/udp+tcp -> container 1162/1514)"
# Receiver states are in /api/health ("ingest") above: "disabled" until the owner turns
# the Traps / Syslog features on in Settings -> Features.
echo "host listeners (docker-proxy = published; anything else conflicts):"
ss -H -lunp '( sport = :162 or sport = :514 )' 2>/dev/null | sed 's/^/  /' || true
ss -H -ltnp '( sport = :514 )' 2>/dev/null | sed 's/^/  /' || true
"$SELF_DIR/ingest-firewall.sh" status 2>&1 | sed 's/^/sender filter: /' | head -n 12 || true

h "Backups ($NETMON_BACKUP_DIR)"
for pat in 'daily/netmon-db-*.dump' 'daily/netmon-data-*.tar.gz' 'pre-deploy-*.dump'; do
  last="$(find "$NETMON_BACKUP_DIR" -maxdepth 2 -path "$NETMON_BACKUP_DIR/$pat" -printf '%T@ %p\n' 2>/dev/null |
    sort -rn | head -n1 | cut -d' ' -f2-)"
  printf '%-28s %s\n' "$pat" "${last:+$(date -r "$last" '+%F %T')  $(du -h "$last" | cut -f1)  $last}"
done
echo "disk: $(df -h "$NETMON_BACKUP_DIR" 2>/dev/null | awk 'NR==2 {print $4 " free of " $2}')"

h "Certificate ($NETMON_ETC_DIR/tls/cert.pem)"
if [[ -s $NETMON_ETC_DIR/tls/cert.pem ]]; then
  openssl x509 -in "$NETMON_ETC_DIR/tls/cert.pem" -noout -subject -issuer -enddate
  openssl x509 -in "$NETMON_ETC_DIR/tls/cert.pem" -noout -checkend 2592000 >/dev/null ||
    echo "WARNING: expires within 30 days"
  [[ -e $NETMON_ETC_DIR/tls/TEMPORARY-SELF-SIGNED ]] &&
    echo "TEMPORARY self-signed certificate - install the CA-signed one (make-csr.sh, install-cert.sh)"
else
  echo "missing"
fi

h "Timers"
systemctl list-timers 'netmon*' --no-pager 2>&1 | head -n 5

h "Recent deploy history"
tail -n 5 "$NETMON_STATE_DIR/history.log" 2>/dev/null || echo "(none)"
