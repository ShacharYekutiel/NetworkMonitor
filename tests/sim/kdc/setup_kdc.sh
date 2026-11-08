#!/usr/bin/env bash
# MIT KDC for the phase-3 Kerberos SSO tests (docs/architecture.md §13.11). Linux only, no root.
#
#   tests/sim/kdc/setup_kdc.sh start DIR [PORT]   create realm MNIDOM.TEST in DIR, start krb5kdc on 127.0.0.1:PORT
#                                                 (default: a free port), export DIR/http.keytab, print env lines
#   tests/sim/kdc/setup_kdc.sh stop DIR
#   tests/sim/kdc/setup_kdc.sh kinit DIR USER [CCACHE]   get a TGT for USER (password from DIR/passwords)
#   tests/sim/kdc/setup_kdc.sh env DIR
#
# Principals: HTTP/localhost, HTTP/127.0.0.1, HTTP/netmon.test (service; keytab DIR/http.keytab, aes256 + aes128),
# users jdoe, asmith, nogroup, olduser (the 2b-3 fake-AD fixture names; password "Kerb-pass-1"), and a second
# keytab DIR/rc4.keytab (rc4-hmac only, for the rc4_only warning). Wrong-realm test: configure
# kerberos.realms without MNIDOM.TEST and sign in as jdoe@MNIDOM.TEST (-> sso_failed, audit sso_realm). Packages: apt krb5-kdc krb5-admin-server krb5-user.
# Exits 3 when krb5kdc / kdb5_util are missing (callers skip their tests).
set -euo pipefail
REALM=MNIDOM.TEST
PASS=Kerb-pass-1
cmd=${1:?start|stop|kinit|env}; DIR=${2:?DIR}
mkdir -p "$DIR"; DIR="$(cd "$DIR" && pwd)"
export KRB5_CONFIG="$DIR/krb5.conf" KRB5_KDC_PROFILE="$DIR/kdc.conf"

need() { command -v "$1" >/dev/null 2>&1 || [ -x "/usr/sbin/$1" ] || { echo "setup_kdc: $1 missing (apt install krb5-kdc krb5-admin-server krb5-user)" >&2; exit 3; }; }
bin() { command -v "$1" 2>/dev/null || echo "/usr/sbin/$1"; }

print_env() {
  echo "export KRB5_CONFIG=$DIR/krb5.conf"
  echo "export KDC_DIR=$DIR"
  echo "export KDC_PORT=$(cat "$DIR/port")"
  echo "export KDC_REALM=$REALM"
  echo "export KDC_KEYTAB=$DIR/http.keytab"
}

case "$cmd" in
start)
  need krb5kdc; need kdb5_util; need kadmin.local
  PORT=${3:-$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')}
  echo "$PORT" >"$DIR/port"
  cat >"$DIR/krb5.conf" <<CONF
[libdefaults]
  default_realm = $REALM
  dns_lookup_kdc = false
  dns_lookup_realm = false
  dns_canonicalize_hostname = false
  rdns = false
  # krb5 >= 1.18 qualifies short names (localhost) with the DNS search domain; a CI
  # runner's search domain then maps the host to a foreign realm
  qualify_shortname = ""
  udp_preference_limit = 1
  allow_weak_crypto = true
  default_ccache_name = FILE:$DIR/ccache_default
[realms]
  $REALM = {
    kdc = 127.0.0.1:$PORT
    admin_server = 127.0.0.1:$PORT
  }
[domain_realm]
  localhost = $REALM
  127.0.0.1 = $REALM
  netmon.test = $REALM
CONF
  cat >"$DIR/kdc.conf" <<CONF
[kdcdefaults]
  kdc_listen = 127.0.0.1:$PORT
  kdc_tcp_listen = 127.0.0.1:$PORT
[realms]
  $REALM = {
    database_name = $DIR/principal
    acl_file = $DIR/kadm5.acl
    key_stash_file = $DIR/stash
    max_life = 10h 0m 0s
    max_renewable_life = 7d 0h 0m 0s
    supported_enctypes = aes256-cts-hmac-sha1-96:normal aes128-cts-hmac-sha1-96:normal rc4-hmac:normal
  }
[logging]
  kdc = FILE:$DIR/kdc.log
CONF
  : >"$DIR/kadm5.acl"
  if [ ! -f "$DIR/principal" ]; then
    "$(bin kdb5_util)" create -r "$REALM" -s -P "master-$PASS" >/dev/null
    k() { "$(bin kadmin.local)" -r "$REALM" -q "$1" >/dev/null 2>&1; }
    for u in jdoe asmith nogroup olduser; do k "addprinc -pw $PASS $u"; done
    for s in HTTP/localhost HTTP/127.0.0.1 HTTP/netmon.test; do k "addprinc -randkey $s"; done
    k "ktadd -k $DIR/http.keytab -e aes256-cts-hmac-sha1-96:normal,aes128-cts-hmac-sha1-96:normal HTTP/localhost HTTP/127.0.0.1 HTTP/netmon.test"
    k "addprinc -randkey -e rc4-hmac:normal HTTP/rc4.test"
    k "ktadd -k $DIR/rc4.keytab -e rc4-hmac:normal HTTP/rc4.test"
    printf 'jdoe %s\nasmith %s\nnogroup %s\nolduser %s\n' "$PASS" "$PASS" "$PASS" "$PASS" >"$DIR/passwords"
  fi
  if [ -f "$DIR/kdc.pid" ] && kill -0 "$(cat "$DIR/kdc.pid")" 2>/dev/null; then :; else
    "$(bin krb5kdc)" -n -r "$REALM" -P "$DIR/kdc.pid" >>"$DIR/kdc.log" 2>&1 &
    echo $! >"$DIR/kdc.pid"
  fi
  for _ in $(seq 1 50); do
    if echo "$PASS" | kinit -c "FILE:$DIR/ccache_probe" jdoe >/dev/null 2>&1; then
      rm -f "$DIR/ccache_probe"; print_env; exit 0
    fi
    sleep 0.2
  done
  echo "setup_kdc: KDC did not answer; log:" >&2; tail -20 "$DIR/kdc.log" >&2; exit 1 ;;
stop)
  [ -f "$DIR/kdc.pid" ] && kill "$(cat "$DIR/kdc.pid")" 2>/dev/null || true
  rm -f "$DIR/kdc.pid" ;;
kinit)
  USER=${3:?user}; CC=${4:-FILE:$DIR/ccache_$USER}
  echo "$PASS" | kinit -c "$CC" "$USER" >/dev/null
  echo "export KRB5CCNAME=$CC" ;;
env) print_env ;;
*) echo "usage: $0 start|stop|kinit|env DIR" >&2; exit 2 ;;
esac
