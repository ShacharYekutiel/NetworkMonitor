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

(($# >= 1 && $# <= 3)) || die "usage: install-cert.sh /path/to/signed.pem [chain.pem|chain.p7b ...]"
require_root
load_site_env
[[ -n ${NETMON_FQDN:-} ]] || die "NETMON_FQDN missing in $NETMON_ETC_DIR/site.env (run bootstrap.sh first)"
TLS="$NETMON_ETC_DIR/tls"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Every input (PEM / DER certificate, PEM / DER PKCS#7 chain, or a PKCS#7 saved under a
# "BEGIN CERTIFICATE" header, as Windows CA exports sometimes are) -> one PEM file per
# certificate in $WORK/certs/. Anything that is not a certificate stops the install.
mkdir -p "$WORK/certs" "$WORK/raw"
n_raw=0
add_pem_certs() { # file with only valid PEM certificates -> one file each
  awk -v d="$WORK/raw" -v base="$n_raw" '/-----BEGIN CERTIFICATE-----/{n++; f=sprintf("%s/%04d.pem", d, base+n)} f{print > f} /-----END CERTIFICATE-----/{f=""}' "$1"
  n_raw=$((n_raw + $(grep -c -- '-----BEGIN CERTIFICATE-----' "$1")))
}
p7_to_pem() { # in(PEM or DER pkcs7) out -> PEM certs (no subject/issuer lines)
  local form=PEM
  grep -q -- '-----BEGIN PKCS7-----' "$1" || form=DER
  openssl pkcs7 -inform "$form" -in "$1" -print_certs 2>/dev/null |
    sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p' >"$2"
  [[ -s $2 ]]
}
load_input() { # file
  local f="$1" clean="$WORK/in.pem" blk i=0
  [[ -s $f ]] || die "$f is empty or missing"
  if grep -q -- '-----BEGIN' "$f"; then
    tr -d '\r' <"$f" >"$clean"
    if grep -q -- '-----BEGIN PKCS7-----' "$clean"; then
      sed -n '/-----BEGIN PKCS7-----/,/-----END PKCS7-----/p' "$clean" >"$WORK/p7.pem"
      p7_to_pem "$WORK/p7.pem" "$WORK/p7certs.pem" || die "$f: the PKCS7 block has no certificates"
      add_pem_certs "$WORK/p7certs.pem"
    fi
    rm -f "$WORK"/blk-*
    awk -v d="$WORK" '/-----BEGIN CERTIFICATE-----/{n++; f=sprintf("%s/blk-%04d", d, n)} f{print > f} /-----END CERTIFICATE-----/{f=""}' "$clean"
    for blk in "$WORK"/blk-*; do
      [[ -e $blk ]] || continue
      i=$((i + 1))
      if openssl x509 -in "$blk" -noout 2>/dev/null; then
        add_pem_certs "$blk"
      elif sed '1d;$d' "$blk" | base64 -d >"$WORK/blk.der" 2>/dev/null &&
        p7_to_pem "$WORK/blk.der" "$WORK/blkcerts.pem"; then
        echo "$f: block $i is a PKCS#7 chain (.p7b) under a CERTIFICATE header: extracted its certificates"
        add_pem_certs "$WORK/blkcerts.pem"
      else
        die "$f: block $i is not a valid certificate (nor a PKCS#7 chain)"
      fi
    done
  elif openssl x509 -inform DER -in "$f" -out "$WORK/der.pem" 2>/dev/null; then
    add_pem_certs "$WORK/der.pem"
  elif p7_to_pem "$f" "$WORK/derp7.pem"; then
    echo "$f: DER PKCS#7 chain (.p7b): extracted its certificates"
    add_pem_certs "$WORK/derp7.pem"
  else
    die "$f is neither a certificate nor a PKCS#7 chain (PEM or DER)"
  fi
}
for arg in "$@"; do load_input "$arg"; done
((n_raw > 0)) || die "no certificate found in: $*"
# de-duplicate (same certificate given twice, e.g. the leaf inside the p7b as well)
for c in "$WORK"/raw/*.pem; do
  fp="$(openssl x509 -in "$c" -noout -fingerprint -sha256 | cut -d= -f2 | tr -d :)"
  [[ -e $WORK/certs/$fp.pem ]] || cp "$c" "$WORK/certs/$fp.pem"
done

pub_of_cert() { openssl x509 -in "$1" -noout -pubkey | openssl pkey -pubin -outform DER 2>/dev/null | sha256sum; }
pub_of_key() { openssl pkey -in "$1" -pubout -outform DER 2>/dev/null | sha256sum; }
# The server certificate is the one whose public key matches our key (not "the first one").
KEYSRC=""
LEAF=""
for k in "$TLS/netmon.key" "$TLS/key.pem"; do
  [[ -s $k ]] || continue
  kp="$(pub_of_key "$k")"
  for c in "$WORK"/certs/*.pem; do
    if [[ "$(pub_of_cert "$c")" == "$kp" ]]; then
      KEYSRC="$k"
      LEAF="$c"
      break 2
    fi
  done
done
[[ -n $KEYSRC ]] || die "no certificate matches $TLS/netmon.key (nor key.pem): was it issued for the CSR from make-csr.sh?"
echo "key match: $KEYSRC"
cp "$LEAF" "$WORK/leaf.pem"

# Chain = leaf + its issuers in order, without the self-signed root (clients have it).
subj() { openssl x509 -in "$1" -noout -subject -nameopt RFC2253 | sed 's/^subject=//'; }
iss() { openssl x509 -in "$1" -noout -issuer -nameopt RFC2253 | sed 's/^issuer=//'; }
cp "$LEAF" "$WORK/chain.pem"
cur="$LEAF"
ncerts=1
for _ in 1 2 3 4 5 6; do
  want_subject="$(iss "$cur")"
  [[ "$(subj "$cur")" == "$want_subject" ]] && break # self-signed
  next=""
  for c in "$WORK"/certs/*.pem; do
    [[ $c != "$cur" && "$(subj "$c")" == "$want_subject" ]] && { next="$c"; break; }
  done
  [[ -n $next ]] || break
  [[ "$(subj "$next")" == "$(iss "$next")" ]] && { echo "root CA left out of the chain: $(subj "$next")"; break; }
  cat "$next" >>"$WORK/chain.pem"
  ncerts=$((ncerts + 1))
  cur="$next"
done


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
  # [1.4.2] Caddy's admin API is off (no `caddy reload`): a restart loads the new pair.
  compose restart proxy >/dev/null && echo "Caddy restarted"
  sleep 2
  echo | openssl s_client -connect 127.0.0.1:443 -servername "$NETMON_FQDN" 2>/dev/null |
    openssl x509 -noout -subject -issuer -enddate || warn "could not read the certificate from :443"
else
  echo "proxy container is not running; the certificate is used on its next start"
fi
