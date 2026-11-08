#!/usr/bin/env bash
# Tests for ops/install-cert.sh with a throwaway root -> intermediate -> server CA chain.
# Covers the owner's first install: the CA returned the server cert plus a .p7b chain
# saved under a "BEGIN CERTIFICATE" header, which broke every TLS client but browsers.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPS="$HERE/../ops"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
export FAKE_STATE="$T/fake" NETMON_ALLOW_NON_ROOT=1 NETMON_ETC_DIR="$T/etc"
export NETMON_REPO_DIR="$T/clone" DOCKER="$HERE/fake-bin/docker"
mkdir -p "$FAKE_STATE" "$NETMON_ETC_DIR/tls" "$T/ca"
echo NETMON_FQDN=netmonitor.test >"$NETMON_ETC_DIR/site.env"
TLS="$NETMON_ETC_DIR/tls"
pass=0
ok() { pass=$((pass + 1)); echo "ok $pass - $1"; }
fail() { echo "not ok - $1"; exit 1; }

# --- a CA chain: root (self-signed) -> intermediate -> server cert for our key
cd "$T/ca"
openssl req -x509 -newkey rsa:2048 -nodes -keyout root.key -out root.pem -days 30 \
  -subj "/CN=PKI-ROOT-CA" -addext basicConstraints=critical,CA:TRUE 2>/dev/null
openssl req -newkey rsa:2048 -nodes -keyout int.key -out int.csr -subj "/DC=test/CN=Test-SERV-CA" 2>/dev/null
printf 'basicConstraints=critical,CA:TRUE\n' >int.ext
openssl x509 -req -in int.csr -CA root.pem -CAkey root.key -CAcreateserial -out int.pem -days 30 -extfile int.ext 2>/dev/null
openssl req -newkey rsa:2048 -nodes -keyout "$TLS/netmon.key" -out leaf.csr -subj "/CN=netmonitor.test" 2>/dev/null
printf 'subjectAltName=DNS:netmonitor.test,DNS:netmonitor\n' >leaf.ext
openssl x509 -req -in leaf.csr -CA int.pem -CAkey int.key -CAcreateserial -out leaf.pem -days 30 -extfile leaf.ext 2>/dev/null
cat leaf.pem int.pem root.pem >all.pem
openssl crl2pkcs7 -nocrl -certfile all.pem -outform DER -out chain.p7b
# the owner's file: leaf + the p7b base64-wrapped under a CERTIFICATE header
{ cat leaf.pem; echo "-----BEGIN CERTIFICATE-----"; base64 -w 64 chain.p7b; echo "-----END CERTIFICATE-----"; } >owner.pem

count() { grep -c -- '-----BEGIN CERTIFICATE-----' "$TLS/cert.pem"; }
subjects() { awk '/BEGIN CERT/{f="'"$T"'/s"++n".pem"} f{print > f} /END CERT/{f=""}' "$TLS/cert.pem"
  for f in "$T"/s*.pem; do openssl x509 -in "$f" -noout -subject -nameopt RFC2253 | sed 's/^subject=//'; done; rm -f "$T"/s*.pem; }
parses() { openssl crl2pkcs7 -nocrl -certfile "$TLS/cert.pem" >/dev/null 2>&1; }
expect_chain() { # label
  [[ "$(count)" == 2 ]] || fail "$1: expected leaf + intermediate, got $(count): $(subjects | tr '\n' ';')"
  [[ "$(subjects | tr '\n' ';')" == "CN=netmonitor.test;CN=Test-SERV-CA,DC=test;" ]] || fail "$1: order/subjects: $(subjects | tr '\n' ';')"
  parses || fail "$1: cert.pem does not parse"
  ok "$1"
}
run() { bash "$OPS/install-cert.sh" "$@" >"$T/out" 2>&1; }

run owner.pem || { cat "$T/out"; fail "owner file rejected"; }
grep -q "PKCS#7 chain (.p7b) under a CERTIFICATE header" "$T/out" || fail "no p7b notice"
grep -q "root CA left out" "$T/out" || fail "root not reported as left out"
expect_chain "leaf + p7b under a CERTIFICATE header (owner's case)"

run chain.p7b || { cat "$T/out"; fail "DER p7b rejected"; }
expect_chain "DER .p7b alone (leaf found by key, not position)"

openssl crl2pkcs7 -nocrl -certfile all.pem -out chainpem.p7b
run int.pem chainpem.p7b || { cat "$T/out"; fail "PEM PKCS7 rejected"; }
expect_chain "PEM PKCS7 as 2nd argument, intermediate first"

run leaf.pem int.pem root.pem || { cat "$T/out"; fail "plain PEM files rejected"; }
expect_chain "plain PEM leaf + intermediate + root"

cp "$TLS/cert.pem" "$T/before.pem"
{ cat leaf.pem; printf -- '-----BEGIN CERTIFICATE-----\nbm90IGEgY2VydA==\n-----END CERTIFICATE-----\n'; } >bad.pem
if run bad.pem; then fail "garbage block accepted"; fi
grep -q "block 2 is not a valid certificate" "$T/out" || { cat "$T/out"; fail "wrong error for garbage"; }
cmp -s "$TLS/cert.pem" "$T/before.pem" || fail "cert.pem changed after a rejected install"
ok "garbage block rejected, installed certificate untouched"

openssl req -x509 -newkey rsa:2048 -nodes -keyout other.key -out other.pem -days 1 -subj "/CN=netmonitor.test" 2>/dev/null
if run other.pem; then fail "certificate for another key accepted"; fi
ok "certificate for another key rejected"

echo "all $pass checks passed"
