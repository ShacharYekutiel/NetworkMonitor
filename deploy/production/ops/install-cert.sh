#!/usr/bin/env bash
# Install the CA-signed certificate for Caddy.
#
#   sudo ./ops/install-cert.sh /path/to/signed.pem [chain.pem]
#
# - accepts PEM or DER; a separate chain file (intermediates) is appended after the leaf;
# - checks the certificate's public key matches /etc/netmon-gitops/tls/netmon.key (made by
#   make-csr.sh) or, for a renewal with the same key, the current key.pem;
# - checks the SAN contains NETMON_FQDN (site.env) and that it is currently valid;
# - installs cert.pem (full chain) + key.pem, keeping the previous pair in tls/previous-<ts>/;
# - reloads Caddy and prints what the server now presents.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$SELF_DIR/lib.sh"

(($# >= 1 && $# <= 2)) || die "usage: install-cert.sh /path/to/signed.pem [chain.pem]"
require_root
load_site_env
[[ -n ${NETMON_FQDN:-} ]] || die "NETMON_FQDN missing in $NETMON_ETC_DIR/site.env (run bootstrap.sh first)"
TLS="$NETMON_ETC_DIR/tls"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

to_pem() { # in out : PEM (one or more certs) or DER -> PEM
  if grep -q -- '-----BEGIN CERTIFICATE-----' "$1"; then
    tr -d '\r' <"$1" | sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p' >"$2"
  else
    openssl x509 -inform DER -in "$1" -out "$2" 2>/dev/null || die "$1 is neither PEM nor DER certificate"
  fi
  [[ -s $2 ]] || die "no certificate found in $1"
}
to_pem "$1" "$WORK/chain.pem"
if [[ -n ${2:-} ]]; then
  to_pem "$2" "$WORK/extra.pem"
  cat "$WORK/extra.pem" >>"$WORK/chain.pem"
fi
openssl x509 -in "$WORK/chain.pem" -out "$WORK/leaf.pem" # first certificate = leaf
ncerts="$(grep -c -- '-----BEGIN CERTIFICATE-----' "$WORK/chain.pem")"

pub_of_cert() { openssl x509 -in "$1" -noout -pubkey | openssl pkey -pubin -outform DER 2>/dev/null | sha256sum; }
pub_of_key() { openssl pkey -in "$1" -pubout -outform DER 2>/dev/null | sha256sum; }
want="$(pub_of_cert "$WORK/leaf.pem")"
KEYSRC=""
for k in "$TLS/netmon.key" "$TLS/key.pem"; do
  if [[ -s $k && "$(pub_of_key "$k")" == "$want" ]]; then
    KEYSRC="$k"
    break
  fi
done
[[ -n $KEYSRC ]] || die "the certificate does not match $TLS/netmon.key (nor key.pem): was it issued for the CSR from make-csr.sh?"
echo "key match: $KEYSRC"

sans="$(openssl x509 -in "$WORK/leaf.pem" -noout -ext subjectAltName 2>/dev/null | tail -n +2 | tr -d ' ')"
echo "SANs: ${sans:-none}"
[[ ",${sans,,}," == *",dns:${NETMON_FQDN},"* ]] || die "the certificate's SAN does not contain DNS:$NETMON_FQDN"
[[ ",${sans,,}," == *",dns:netmonitor,"* ]] || warn "the SAN has no DNS:NetMonitor (short-name access will show a certificate warning)"
openssl x509 -in "$WORK/leaf.pem" -noout -checkend 0 >/dev/null || die "the certificate has expired"
openssl x509 -in "$WORK/leaf.pem" -noout -checkend 2592000 >/dev/null || warn "the certificate expires within 30 days"
subject="$(openssl x509 -in "$WORK/leaf.pem" -noout -subject -nameopt RFC2253)"
issuer="$(openssl x509 -in "$WORK/leaf.pem" -noout -issuer -nameopt RFC2253)"
if ((ncerts == 1)) && [[ ${subject#subject=} != "${issuer#issuer=}" ]]; then
  warn "only the server certificate was given (no intermediate CA). Browsers that do not already have the intermediate will fail: pass the chain file as the 2nd argument if you have it."
fi
if openssl verify -untrusted "$WORK/chain.pem" "$WORK/leaf.pem" >/dev/null 2>&1; then
  echo "chain: verifies against this server's trust store"
else
  warn "chain does not verify against this server's trust store (fine if the corporate root CA is not installed here; browsers need it)"
fi

if [[ -s $TLS/cert.pem ]]; then
  prev="$TLS/previous-$(date '+%Y%m%d-%H%M%S')"
  install -d -m 0700 "$prev"
  cp -p "$TLS/cert.pem" "$TLS/key.pem" "$prev/" 2>/dev/null || true
  echo "previous pair saved in $prev"
fi
install -m 0644 "$WORK/chain.pem" "$TLS/cert.pem.new"
install -m 0600 "$KEYSRC" "$TLS/key.pem.new"
mv -f "$TLS/key.pem.new" "$TLS/key.pem"
mv -f "$TLS/cert.pem.new" "$TLS/cert.pem"
rm -f "$TLS/TEMPORARY-SELF-SIGNED"
echo "installed $TLS/cert.pem ($ncerts certificate(s)) and $TLS/key.pem"

if service_running proxy; then
  if compose exec -T proxy caddy reload --config /etc/caddy/Caddyfile --force >/dev/null 2>&1; then
    echo "Caddy reloaded"
  else
    compose restart proxy >/dev/null && echo "Caddy restarted"
  fi
  sleep 2
  echo | openssl s_client -connect 127.0.0.1:443 -servername "$NETMON_FQDN" 2>/dev/null |
    openssl x509 -noout -subject -issuer -enddate || warn "could not read the certificate from :443"
else
  echo "proxy container is not running; the certificate is used on its next start"
fi
