#!/usr/bin/env bash
# First-time (and re-runnable) setup of the NetworkMonitor production server.
# Fresh Ubuntu 22.04 / 24.04, run as root FROM THE CLONE of the code repo:
#
#   # optional, before: corporate CA in /usr/local/share/ca-certificates/ + update-ca-certificates
#   # optional, before: export HTTPS_PROXY=http://proxy:8080 HTTP_PROXY=... NO_PROXY=...
#   apt-get update && apt-get install -y git
#   git clone https://github.com/shacharyekutiel/networkmonitor.git /opt/netmon
#   cd /opt/netmon/deploy/production && sudo -E NETMON_FQDN=netmonitor.example.local ./ops/bootstrap.sh
#
# Environment (all optional; prompted for when missing and a terminal is available):
#   NETMON_FQDN        full DNS name of the server (stored in /etc/netmon-gitops/site.env)
#   HTTPS_PROXY / HTTP_PROXY / NO_PROXY   persisted for apt, the docker daemon and git
#   GITHUB_USER        GitHub user for `docker login ghcr.io`
#   NETMON_REPO_TOKEN  fine-grained token, Contents: read-only on networkmonitor
#   GHCR_TOKEN         classic token with read:packages
#   NETMON_CA_DIR      extra folder of corporate CA files to import into the system store
#   NETMON_NO_DEPLOY=1 do everything except the first sync (e.g. before restoring a backup)
#
# Idempotent: re-running keeps stored tokens, TLS files and data.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(git -C "$SELF_DIR" rev-parse --show-toplevel 2>/dev/null)" ||
  { echo "bootstrap.sh must be run from a git clone of networkmonitor" >&2; exit 1; }
export NETMON_REPO_DIR="$REPO_DIR"
# shellcheck source=lib.sh
. "$SELF_DIR/lib.sh"

require_root
step() { printf '\n==> %s\n' "$*"; }
ask() { # var prompt [secret]
  local __v
  [[ -t 0 ]] || die "$1 is not set and there is no terminal to ask (set it in the environment)"
  if [[ ${3:-} == secret ]]; then
    read -rs -p "$2: " __v
    echo
  else
    read -r -p "$2: " __v
  fi
  [[ -n $__v ]] || die "$1 must not be empty"
  printf -v "$1" '%s' "$__v"
}
write_root_file() { # path content mode
  (umask 077 && printf '%s\n' "$2" >"$1.tmp") && chmod "$3" "$1.tmp" && mv "$1.tmp" "$1"
}

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ ${ID:-} == ubuntu ]] || warn "tested on Ubuntu 22.04/24.04 only (this is ${PRETTY_NAME:-unknown})"
fi
[[ $REPO_DIR == /opt/netmon ]] || warn "the clone is at $REPO_DIR (expected /opt/netmon); using it"

install -d -m 0700 "$NETMON_ETC_DIR" "$NETMON_ETC_DIR/tls" "$NETMON_STATE_DIR"
install -d -m 0755 "$NETMON_ETC_DIR/ca" "$NETMON_LIB_DIR"
install -d -m 0700 "$NETMON_BACKUP_DIR"
write_root_file "$NETMON_ETC_DIR/gitops.env" "NETMON_REPO_DIR=$REPO_DIR" 0600

# ------------------------------------------------------------------ site name
step "Site name"
NETMON_FQDN="${NETMON_FQDN:-}"
if [[ -z $NETMON_FQDN && -r $NETMON_ETC_DIR/site.env ]]; then
  NETMON_FQDN="$(env_get "$NETMON_ETC_DIR/site.env" NETMON_FQDN)"
fi
[[ -n $NETMON_FQDN ]] || ask NETMON_FQDN "Full DNS name of this server (e.g. netmonitor.corp.local)"
NETMON_FQDN="${NETMON_FQDN,,}"
[[ $NETMON_FQDN =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$ ]] ||
  die "NETMON_FQDN=$NETMON_FQDN is not a valid DNS name"
write_root_file "$NETMON_ETC_DIR/site.env" "NETMON_FQDN=$NETMON_FQDN" 0644
echo "NETMON_FQDN=$NETMON_FQDN"

# ------------------------------------------------------------------ proxy
step "Outbound proxy"
PROXY_HTTPS="${HTTPS_PROXY:-${https_proxy:-}}"
PROXY_HTTP="${HTTP_PROXY:-${http_proxy:-$PROXY_HTTPS}}"
PROXY_HTTPS="${PROXY_HTTPS:-$PROXY_HTTP}"
PROXY_NO="${NO_PROXY:-${no_proxy:-}}"
if [[ -n $PROXY_HTTPS ]]; then
  base_no="localhost,127.0.0.1,::1,$NETMON_FQDN,netmonitor"
  PROXY_NO="${PROXY_NO:+$PROXY_NO,}$base_no"
  write_root_file "$NETMON_ETC_DIR/proxy.env" "HTTPS_PROXY=$PROXY_HTTPS
HTTP_PROXY=$PROXY_HTTP
NO_PROXY=$PROXY_NO
https_proxy=$PROXY_HTTPS
http_proxy=$PROXY_HTTP
no_proxy=$PROXY_NO" 0600
  write_root_file /etc/apt/apt.conf.d/95netmon-proxy "Acquire::http::Proxy \"$PROXY_HTTP\";
Acquire::https::Proxy \"$PROXY_HTTPS\";" 0644
  install -d -m 0755 /etc/systemd/system/docker.service.d
  write_root_file /etc/systemd/system/docker.service.d/http-proxy.conf "[Service]
Environment=\"HTTPS_PROXY=$PROXY_HTTPS\" \"HTTP_PROXY=$PROXY_HTTP\" \"NO_PROXY=$PROXY_NO\"" 0644
  git -C "$REPO_DIR" config http.proxy "$PROXY_HTTPS"
  # The app's outbound alerts (Telegram, e-mail relays, webhooks) need the proxy too.
  # Created once; edit /etc/netmon-gitops/app.env afterwards if internal targets need NO_PROXY.
  if [[ ! -e $NETMON_ETC_DIR/app.env ]]; then
    write_root_file "$NETMON_ETC_DIR/app.env" "# Server-specific settings for the netmon container (not in git).
HTTPS_PROXY=$PROXY_HTTPS
HTTP_PROXY=$PROXY_HTTP
NO_PROXY=$PROXY_NO,db,netmon,proxy" 0600
  fi
  export HTTPS_PROXY="$PROXY_HTTPS" HTTP_PROXY="$PROXY_HTTP" NO_PROXY="$PROXY_NO"
  echo "proxy: $PROXY_HTTPS (apt, docker daemon, git; app.env for the app)"
else
  echo "no HTTPS_PROXY/HTTP_PROXY in the environment: direct internet access"
fi

# ------------------------------------------------------------------ corporate CA
step "Corporate CA certificates"
if [[ -n ${NETMON_CA_DIR:-} ]]; then
  [[ -d $NETMON_CA_DIR ]] || die "NETMON_CA_DIR=$NETMON_CA_DIR is not a directory"
  install -d -m 0755 /usr/local/share/ca-certificates/netmon
  for f in "$NETMON_CA_DIR"/*.crt "$NETMON_CA_DIR"/*.pem "$NETMON_CA_DIR"/*.cer; do
    [[ -f $f ]] || continue
    b="$(basename "${f%.*}")"
    if grep -q 'BEGIN CERTIFICATE' "$f"; then cp "$f" "/usr/local/share/ca-certificates/netmon/$b.crt"
    else openssl x509 -inform DER -in "$f" -out "/usr/local/share/ca-certificates/netmon/$b.crt"; fi
  done
  update-ca-certificates
fi
# The app container trusts every CA in /etc/netmon-gitops/ca (mounted at /etc/netmon/certs).
n=0
while IFS= read -r -d '' f; do
  cp "$f" "$NETMON_ETC_DIR/ca/$(basename "$f")"
  chmod 0644 "$NETMON_ETC_DIR/ca/$(basename "$f")"
  n=$((n + 1))
done < <(find /usr/local/share/ca-certificates -type f -name '*.crt' -print0 2>/dev/null)
echo "$n local CA certificate(s) made available to the app"

# ------------------------------------------------------------------ packages
step "Packages (docker.io, docker-compose-v2 from the Ubuntu archive)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q docker.io docker-compose-v2 git curl openssl ca-certificates util-linux conntrack
systemctl daemon-reload
systemctl enable docker >/dev/null
# Restart so the daemon picks up the proxy drop-in and the (possibly new) CA store.
systemctl restart docker
cv="$(docker compose version --short 2>/dev/null || echo 0)"
cv="${cv#v}"
if [[ "$(printf '%s\n2.24.0\n' "$cv" | sort -V | head -n1)" != 2.24.0 ]]; then
  die "docker compose $cv is too old (need >= 2.24 for optional env files)"
fi
echo "docker $(docker version -f '{{.Server.Version}}'), compose $cv"

# ------------------------------------------------------------------ tokens
step "Tokens (stored root-only in $NETMON_ETC_DIR)"
if [[ ! -s $NETMON_ETC_DIR/repo-token ]]; then
  tok="${NETMON_REPO_TOKEN:-}"
  [[ -n $tok ]] || ask tok "GitHub fine-grained token for networkmonitor (Contents: read-only)" secret
  write_root_file "$NETMON_ETC_DIR/repo-token" "$tok" 0600
fi
if [[ ! -s $NETMON_ETC_DIR/ghcr-token ]]; then
  tok="${GHCR_TOKEN:-}"
  [[ -n $tok ]] || ask tok "GitHub classic token for ghcr.io (read:packages)" secret
  write_root_file "$NETMON_ETC_DIR/ghcr-token" "$tok" 0600
fi
unset tok
GH_USER="${GITHUB_USER:-}"
[[ -n $GH_USER || ! -s $NETMON_ETC_DIR/ghcr-user ]] || GH_USER="$(cat "$NETMON_ETC_DIR/ghcr-user")"
[[ -n $GH_USER ]] || ask GH_USER "GitHub user name (for docker login ghcr.io)"
write_root_file "$NETMON_ETC_DIR/ghcr-user" "$GH_USER" 0600

# git: credential helper reads the token file; the remote URL carries no credentials.
install -m 0755 "$SELF_DIR/git-credential.sh" "$NETMON_LIB_DIR/git-credential.sh"
url="$(git -C "$REPO_DIR" remote get-url origin)"
clean_url="$(printf '%s' "$url" | sed -E 's#^(https://)[^@/]+@#\1#')"
[[ $clean_url == "$url" ]] || { git -C "$REPO_DIR" remote set-url origin "$clean_url"; echo "removed credentials from the remote URL"; }
git -C "$REPO_DIR" config --unset-all credential.helper 2>/dev/null || true
git -C "$REPO_DIR" config --add credential.helper ""
git -C "$REPO_DIR" config --add credential.helper "$NETMON_LIB_DIR/git-credential.sh"
git -C "$REPO_DIR" ls-remote --exit-code origin "refs/heads/$NETMON_BRANCH" >/dev/null ||
  die "cannot read origin/$NETMON_BRANCH with the stored token (fix: rm $NETMON_ETC_DIR/repo-token and re-run)"
echo "git: origin readable with the stored token"

docker login ghcr.io -u "$GH_USER" --password-stdin <"$NETMON_ETC_DIR/ghcr-token" >/dev/null ||
  die "docker login ghcr.io failed (fix: rm $NETMON_ETC_DIR/ghcr-token and re-run)"
echo "docker: logged in to ghcr.io as $GH_USER"

# ------------------------------------------------------------------ TLS
step "TLS certificate"
if [[ ! -s $NETMON_ETC_DIR/tls/cert.pem || ! -s $NETMON_ETC_DIR/tls/key.pem ]]; then
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  san="DNS:$NETMON_FQDN,DNS:netmonitor,DNS:localhost${ip:+,IP:$ip}"
  (umask 077 && openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 30 \
    -subj "/CN=$NETMON_FQDN" -addext "subjectAltName=$san" \
    -keyout "$NETMON_ETC_DIR/tls/key.pem" -out "$NETMON_ETC_DIR/tls/cert.pem" 2>/dev/null)
  chmod 0644 "$NETMON_ETC_DIR/tls/cert.pem"
  touch "$NETMON_ETC_DIR/tls/TEMPORARY-SELF-SIGNED"
  echo "created a TEMPORARY self-signed certificate (30 days) for $san"
  echo "replace it with the CA-signed one: ops/make-csr.sh, then ops/install-cert.sh"
else
  echo "keeping the existing certificate: $(openssl x509 -in "$NETMON_ETC_DIR/tls/cert.pem" -noout -subject -enddate | tr '\n' ' ')"
fi

# ------------------------------------------------------------------ systemd
step "systemd units"
for f in "$SELF_DIR"/*.sh; do
  install -m 0755 "$f" "$NETMON_LIB_DIR/$(basename "$f").new"
  mv -f "$NETMON_LIB_DIR/$(basename "$f").new" "$NETMON_LIB_DIR/$(basename "$f")"
done
install -m 0644 "$NETMON_PROD_DIR"/systemd/* "$NETMON_SYSTEMD_DIR/"
systemctl daemon-reload
systemctl enable netmon-gitops.timer netmon-backup.timer >/dev/null
echo "installed: $(cd "$NETMON_PROD_DIR/systemd" && echo *)"

# ------------------------------------------------------------------ first sync
if [[ ${NETMON_NO_DEPLOY:-0} == 1 ]]; then
  step "NETMON_NO_DEPLOY=1: skipping the first deploy (timers NOT started)"
  echo "start later with: systemctl start netmon-gitops.service && systemctl start netmon-gitops.timer netmon-backup.timer"
  exit 0
fi
# Timers first: if the first deploy fails (e.g. the release image is not published yet, or
# the proxy blocks ghcr.io), the timer keeps retrying every 2 minutes on its own.
systemctl start netmon-gitops.timer netmon-backup.timer
step "First deploy (this pulls the images; can take a few minutes)"
if ! systemctl start netmon-gitops.service; then
  journalctl -u netmon-gitops -n 40 --no-pager || true
  cat <<EOF

WARNING: the first deploy did not finish. The sync timer is running and retries every
2 minutes (for example once the release image exists on ghcr.io). Follow it with:
  journalctl -u netmon-gitops -f
EOF
  exit 1
fi
"$SELF_DIR/status.sh" || true

cat <<EOF

========================================================================
 NetworkMonitor is running:   https://$NETMON_FQDN/

 >>> Open it NOW and complete the setup wizard (the first user becomes admin). <<<
 Until the wizard is done, anyone who reaches the page can create the admin account.

 The certificate is temporary (self-signed) until you install the CA-signed one:
   cd $NETMON_PROD_DIR
   sudo ./ops/make-csr.sh $NETMON_FQDN [server-ip]      # prints the CSR for the CA
   sudo ./ops/install-cert.sh /path/to/signed.pem       # after the CA signs it

 Check:   systemctl list-timers 'netmon*'
          journalctl -u netmon-gitops -n 50
          sudo $NETMON_PROD_DIR/ops/status.sh
========================================================================
EOF
