#!/usr/bin/env bash
# Create a private key + CSR for the organisation's internal CA.
#
#   sudo ./ops/make-csr.sh <fqdn> [ip] [--force]
#
# Writes (root-only) /etc/netmon-gitops/tls/netmon.key (only if absent; --force makes a new
# key) and /etc/netmon-gitops/tls/netmon.csr, and prints the CSR. SANs: DNS:<fqdn>,
# DNS:NetMonitor and IP:<ip> if given. The running certificate (cert.pem/key.pem) is not
# touched: after the CA signs, run `sudo ./ops/install-cert.sh /path/to/signed.pem`, which
# checks the cert against netmon.key and installs both as cert.pem/key.pem.
#
# Optional subject fields: NETMON_CSR_ORG, NETMON_CSR_OU, NETMON_CSR_COUNTRY (e.g. IL).
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$SELF_DIR/lib.sh"

FORCE=0 ARGS=()
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    -h | --help) sed -n '2,13p' "$0"; exit 0 ;;
    -*) die "unknown option: $a" ;;
    *) ARGS+=("$a") ;;
  esac
done
((${#ARGS[@]} >= 1 && ${#ARGS[@]} <= 2)) || die "usage: make-csr.sh <fqdn> [ip] [--force]"
FQDN="${ARGS[0],,}"
IP="${ARGS[1]:-}"
[[ $FQDN =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$ ]] || die "invalid DNS name: $FQDN"
if [[ -n $IP ]] && ! [[ $IP =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ || $IP =~ ^[0-9a-fA-F:]+$ ]]; then
  die "invalid IP address: $IP"
fi
require_root

TLS="$NETMON_ETC_DIR/tls"
install -d -m 0700 "$NETMON_ETC_DIR" "$TLS"
KEY="$TLS/netmon.key" CSR="$TLS/netmon.csr"

if [[ -s $KEY && $FORCE -eq 0 ]]; then
  echo "Using the existing key $KEY (pass --force for a new one)."
else
  [[ -s $KEY ]] && cp -p "$KEY" "$KEY.old-$(date '+%Y%m%d-%H%M%S')"
  (umask 077 && openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$KEY.tmp" 2>/dev/null)
  mv "$KEY.tmp" "$KEY"
  echo "Created a new RSA-3072 key: $KEY"
fi

SAN="DNS:$FQDN"
[[ $FQDN == netmonitor ]] || SAN+=",DNS:NetMonitor"
[[ -n $IP ]] && SAN+=",IP:$IP"
SUBJ="/CN=$FQDN"
[[ -n ${NETMON_CSR_OU:-} ]] && SUBJ="/OU=$NETMON_CSR_OU$SUBJ"
[[ -n ${NETMON_CSR_ORG:-} ]] && SUBJ="/O=$NETMON_CSR_ORG$SUBJ"
[[ -n ${NETMON_CSR_COUNTRY:-} ]] && SUBJ="/C=$NETMON_CSR_COUNTRY$SUBJ"

openssl req -new -sha256 -key "$KEY" -subj "$SUBJ" \
  -addext "subjectAltName=$SAN" \
  -addext "keyUsage=critical,digitalSignature,keyEncipherment" \
  -addext "extendedKeyUsage=serverAuth" \
  -out "$CSR"
chmod 0644 "$CSR"

echo
echo "CSR: $CSR"
echo "Subject: $SUBJ"
echo "SANs:    $SAN"
echo
cat "$CSR"
cat <<EOF

Next steps:
 1. Send the CSR above (the whole block, including the BEGIN/END lines) to the CA team.
    Ask for a "Web Server" certificate (serverAuth) that keeps the SANs from the CSR,
    returned as PEM (Base-64) WITH the intermediate CA(s) - the full chain.
 2. Copy the signed file to this server, e.g. /root/netmon-signed.pem, then run:
      sudo $NETMON_PROD_DIR/ops/install-cert.sh /root/netmon-signed.pem
    (it checks the certificate matches $KEY and contains $FQDN, installs it as
    $TLS/cert.pem + key.pem and reloads Caddy). If the chain comes in a separate file:
      sudo $NETMON_PROD_DIR/ops/install-cert.sh signed.pem chain.pem
EOF
