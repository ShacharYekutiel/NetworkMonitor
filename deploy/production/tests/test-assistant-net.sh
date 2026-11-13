#!/usr/bin/env bash
# [3b] Tests for ops/assistant-net.sh (NO_PROXY routing of the Bedrock / Roles Anywhere
# hosts, app.env editing) and the IAM policy templates in aws/ (valid JSON, least
# privilege, render-policies.py). No network: getent and curl are faked.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROD="$HERE/.."
OPS="$PROD/ops"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
export FAKE_STATE="$T/fake" NETMON_ALLOW_NON_ROOT=1 NETMON_ETC_DIR="$T/etc"
export NETMON_REPO_DIR="$T/clone" DOCKER="$HERE/fake-bin/docker"
mkdir -p "$FAKE_STATE" "$NETMON_ETC_DIR" "$T/bin"
pass=0
ok() { pass=$((pass + 1)); echo "ok $pass - $1"; }
fail() { echo "not ok - $1"; [[ -f $T/out ]] && sed 's/^/  | /' "$T/out"; exit 1; }

# Fake resolver: vpce names and bedrock-runtime.il-central-1 (private DNS) resolve privately.
cat >"$T/bin/getent" <<'X'
#!/usr/bin/env bash
case "$2" in
  *.vpce.amazonaws.com | bedrock-runtime.il-central-1.amazonaws.com) echo "10.20.30.40     STREAM $2" ;;
  *.amazonaws.com) echo "52.94.0.10      STREAM $2" ;;
  *) exit 2 ;;
esac
X
# Fake curl: records how it was called; "reachable" unless the host is unknown.
cat >"$T/bin/curl" <<'X'
#!/usr/bin/env bash
echo "$*" >>"$FAKE_STATE/curl"
[[ "${*: -1}" == *nxdomain* ]] && { echo "curl: (6) Could not resolve host" >&2; exit 6; }
printf 403
X
chmod +x "$T/bin/getent" "$T/bin/curl"
export GETENT="$T/bin/getent" CURL="$T/bin/curl"
run() { bash "$OPS/assistant-net.sh" "$@" >"$T/out" 2>&1; }
row() { grep -E "^$1 " "$T/out"; }

# --- lib.sh no_proxy_match: httpx semantics
# shellcheck source=../ops/lib.sh
. "$OPS/lib.sh"
m() { no_proxy_match "$1" "$2"; }
m vpce-1.bedrock-runtime.us-east-1.vpce.amazonaws.com ".vpce.amazonaws.com" || fail "leading-dot suffix"
m vpce-1.bedrock-runtime.us-east-1.vpce.amazonaws.com "localhost, *.vpce.amazonaws.com" || fail "*. suffix with spaces"
m bedrock-runtime.us-east-1.amazonaws.com "amazonaws.com" || fail "bare domain matches subdomains"
m amazonaws.com "amazonaws.com" || fail "bare domain matches itself"
m anything.example "*" || fail "* matches all"
m bedrock-runtime.us-east-1.amazonaws.com "https://bedrock-runtime.us-east-1.amazonaws.com:443" || fail "scheme/port ignored"
! m evilamazonaws.com "amazonaws.com" || fail "no partial-label match"
! m bedrock-runtime.us-east-1.amazonaws.com ".vpce.amazonaws.com,db,netmon" || fail "public name must not match vpce suffix"
! m x.y "" || fail "empty list"
[[ "$(redact_url http://u:secret@proxy:8080)" == "http://***@proxy:8080" ]] || fail "redact_url"
ok "NO_PROXY matching (httpx rules) and proxy URL redaction"

# --- check without app.env: everything direct
run check --region us-east-1 || fail "check without app.env"
grep -q "proxy:     none" "$T/out" || fail "no proxy reported"
[[ "$(row bedrock-runtime.us-east-1.amazonaws.com)" == *" public "*" direct "*"HTTP 403 (reachable)"* ]] || fail "direct row"
grep -q -- "--noproxy \*" "$FAKE_STATE/curl" || fail "direct test must bypass any proxy"
ok "check without a proxy: direct, public address, reachable"

# --- check with a proxy and no vpce entry: warning for the VPC endpoint name
cat >"$NETMON_ETC_DIR/app.env" <<'X'
# Server-specific settings
HTTPS_PROXY=http://user:pw@proxy.corp:8080
HTTP_PROXY=http://user:pw@proxy.corp:8080
NO_PROXY=localhost,127.0.0.1,::1,.mnidom.net,db,netmon,proxy
TZ=Asia/Jerusalem
X
chmod 0600 "$NETMON_ETC_DIR/app.env"
: >"$FAKE_STATE/curl"
run check --region il-central-1 --host https://vpce-0abc-xyz.bedrock-runtime.us-east-1.vpce.amazonaws.com/ ||
  fail "check with proxy"
grep -q "proxy:     http://\*\*\*@proxy.corp:8080" "$T/out" || fail "proxy credentials must be hidden"
! grep -q "pw@" "$T/out" || fail "password printed"
[[ "$(row vpce-0abc-xyz.bedrock-runtime.us-east-1.vpce.amazonaws.com)" == *" private "*" proxy "* ]] || fail "vpce row"
grep -q "WARNING: VPC endpoint name goes to the proxy" "$T/out" || fail "vpce warning"
grep -q "WARNING: resolves to a private address" "$T/out" || fail "private-DNS warning for il-central-1 names"
grep -q -- "--proxy http://user:pw@proxy.corp:8080" "$FAKE_STATE/curl" || fail "proxied test uses the app.env proxy"
ok "check with a proxy: VPC endpoint / private-DNS names via the proxy are flagged"

run check --no-connect --host nxdomain.example || fail "check --no-connect"
[[ "$(row nxdomain.example)" == *" no-dns "*" proxy "*" -" ]] || fail "unresolvable host row"
ok "check --no-connect, unresolvable host"

# --- no-proxy: adds once, keeps other lines and mode 0600
run no-proxy .vpce.amazonaws.com bedrock-runtime.il-central-1.amazonaws.com || fail "no-proxy add"
grep -qx "NO_PROXY=localhost,127.0.0.1,::1,.mnidom.net,db,netmon,proxy,.vpce.amazonaws.com,bedrock-runtime.il-central-1.amazonaws.com" \
  "$NETMON_ETC_DIR/app.env" || fail "NO_PROXY line: $(grep NO_PROXY "$NETMON_ETC_DIR/app.env")"
grep -qx "TZ=Asia/Jerusalem" "$NETMON_ETC_DIR/app.env" || fail "other lines kept"
grep -qx "# Server-specific settings" "$NETMON_ETC_DIR/app.env" || fail "comments kept"
[[ "$(stat -c %a "$NETMON_ETC_DIR/app.env")" == 600 ]] || fail "mode 0600"
grep -q "docker compose --env-file release.env up -d netmon" "$T/out" || fail "apply hint"
sum1="$(md5sum <"$NETMON_ETC_DIR/app.env")"
run no-proxy .VPCE.amazonaws.com || fail "no-proxy again"
grep -q "already has" "$T/out" || fail "idempotent message"
[[ "$(md5sum <"$NETMON_ETC_DIR/app.env")" == "$sum1" ]] || fail "file changed on a no-op"
ok "no-proxy: appended once (case-insensitive), file mode and other lines kept"

run check --region il-central-1 --host vpce-0abc-xyz.bedrock-runtime.us-east-1.vpce.amazonaws.com --no-connect || fail "re-check"
[[ "$(row vpce-0abc-xyz.bedrock-runtime.us-east-1.vpce.amazonaws.com)" == *" direct "* ]] || fail "vpce now direct"
[[ "$(row bedrock-runtime.il-central-1.amazonaws.com)" == *" direct "* ]] || fail "regional name now direct"
! grep -q WARNING "$T/out" || fail "warnings left after the fix"
ok "after no-proxy the endpoint names are direct, no warnings"

printf 'HTTPS_PROXY=http://proxy:3128\n' >"$NETMON_ETC_DIR/app.env"
run no-proxy --restart .vpce.amazonaws.com || fail "no-proxy without a NO_PROXY line"
grep -qx "NO_PROXY=localhost,127.0.0.1,::1,db,netmon,proxy,.vpce.amazonaws.com" "$NETMON_ETC_DIR/app.env" || fail "NO_PROXY created"
grep -q "compose .* up -d netmon" "$FAKE_STATE/calls" || fail "--restart recreates netmon"
ok "no-proxy creates NO_PROXY with the container names; --restart runs compose up -d netmon"

printf 'TZ=UTC\n' >"$NETMON_ETC_DIR/app.env"
run no-proxy .vpce.amazonaws.com || fail "no-proxy without a proxy"
grep -q "Nothing to change" "$T/out" || fail "no-op message without a proxy"
[[ "$(cat "$NETMON_ETC_DIR/app.env")" == TZ=UTC ]] || fail "unchanged without a proxy"
! run no-proxy 'bad host;rm' || fail "invalid entry accepted"
! run check --region nowhere || fail "invalid region accepted"
ok "no-proxy is a no-op without a proxy; invalid input rejected"

# --- IAM policy templates
for f in "$PROD"/aws/*.json; do
  python3 -I - "$f" <<'X' || fail "policy $(basename "$f")"
import json, sys
doc = json.load(open(sys.argv[1]))
assert doc["Version"] == "2012-10-17", "Version"
for st in doc["Statement"]:
    assert st["Effect"] == "Allow", st
    acts = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
    for a in acts:
        assert "*" not in a, f"wildcard action {a}"
        assert a.startswith(("bedrock:", "sts:")), a
        assert a.split(":")[1].startswith(("Invoke", "Retrieve", "List", "Get", "AssumeRole", "TagSession", "SetSourceIdentity")), f"not read-only: {a}"
X
done
ok "aws/*.json: valid JSON, Version 2012-10-17, no wildcard or write actions"

python3 -I "$PROD/aws/render-policies.py" --account 111122223333 --region us-east-1 \
  --profile 'us.anthropic.claude-x-v1:0' --out "$T/pol" >"$T/out" 2>&1 || fail "render us"
python3 -I - "$T/pol/bedrock-invoke-policy.json" <<'X' || fail "rendered us policy"
import json, sys
s = json.load(open(sys.argv[1]))["Statement"]
assert s[0]["Resource"] == ["arn:aws:bedrock:us-east-1:111122223333:inference-profile/us.anthropic.claude-x-v1:0"], s[0]
assert s[1]["Resource"] == ["arn:aws:bedrock:*::foundation-model/anthropic.claude-x-v1:0"], s[1]
assert "<" not in json.dumps(s)
X
[[ ! -e $T/pol/rolesanywhere-trust-policy.json && ! -e $T/pol/bedrock-knowledge-base-policy.json ]] || fail "incomplete templates written"
python3 -I "$PROD/aws/render-policies.py" --account 111122223333 --region il-central-1 \
  --profile 'global.anthropic.claude-x-v1:0' --light-profile 'global.anthropic.claude-y-v1:0' \
  --trust-anchor-id 0f0e-1 --kb-id KB12345678 --out "$T/pol2" >"$T/out" 2>&1 || fail "render global"
[[ "$(find "$T/pol2" -name '*.json' | wc -l)" == 5 ]] || fail "all 5 rendered"
grep -q '"arn:aws:bedrock:\*::foundation-model/anthropic.claude-y-v1:0"' "$T/pol2/bedrock-invoke-policy.json" || fail "light model"
grep -q '"aws:PrincipalTag/x509Subject/CN": "netmonitor.mnidom.net"' "$T/pol2/rolesanywhere-trust-policy.json" || fail "CN"
grep -q 'trust-anchor/0f0e-1"' "$T/pol2/rolesanywhere-trust-policy.json" || fail "trust anchor"
! grep -rq '<[A-Z_]*>' "$T/pol2" || fail "placeholder left"
ok "render-policies.py: us. and global. profiles, light model, trust policy, KB"

# [1.4.1] --trust-anchor-id takes the bare id or the full ARN (used to render a doubled ARN);
# region / account of the ARN are cross-checked; --cert-cn is validated and echoed
ARN='arn:aws:rolesanywhere:eu-west-1:111122223333:trust-anchor/6f1c2d3e-0000-4a5b-8c7d-9e0f1a2b3c4d'
WANT='"aws:SourceArn": "arn:aws:rolesanywhere:eu-west-1:111122223333:trust-anchor/6f1c2d3e-0000-4a5b-8c7d-9e0f1a2b3c4d"'
render() { python3 -I "$PROD/aws/render-policies.py" --account 111122223333 --region us-east-1 \
  --profile 'us.anthropic.claude-x-v1:0' "$@" >"$T/out" 2>&1; }
render --ra-region eu-west-1 --trust-anchor-id "$ARN" --cert-cn nm.example.org --out "$T/pol3" || fail "render with ARN: $(cat "$T/out")"
grep -qF "$WANT" "$T/pol3/rolesanywhere-trust-policy.json" || fail "ARN not reduced to the id"
[[ "$(grep -o 'trust-anchor/' "$T/pol3/rolesanywhere-trust-policy.json" | wc -l)" == 1 ]] || fail "doubled ARN"
grep -q '"aws:PrincipalTag/x509Subject/CN": "nm.example.org"' "$T/pol3/rolesanywhere-trust-policy.json" || fail "cert CN"
grep -q 'expects certificate subject CN = nm.example.org' "$T/out" || fail "CN not echoed"
render --ra-region eu-west-1 --trust-anchor-id 6f1c2d3e-0000-4a5b-8c7d-9e0f1a2b3c4d --out "$T/pol4" || fail "render with id"
grep -qF "$WANT" "$T/pol4/rolesanywhere-trust-policy.json" || fail "bare id"
render --trust-anchor-id "$ARN" --out "$T/pol5" && fail "region mismatch accepted"
grep -q 'ARN is in region eu-west-1, but the Roles Anywhere region is us-east-1' "$T/out" || fail "region mismatch message: $(cat "$T/out")"
render --ra-region eu-west-1 --trust-anchor-id "${ARN/111122223333/999988887777}" --out "$T/pol5" && fail "account mismatch accepted"
grep -q 'ARN is in account 999988887777, but --account is 111122223333' "$T/out" || fail "account mismatch message"
render --trust-anchor-id 'arn:aws:iam::111122223333:role/x' --out "$T/pol5" && fail "foreign ARN accepted"
render --trust-anchor-id 'trust-anchor/abc' --out "$T/pol5" && fail "path accepted"
for cn in 'CN=nm.example.org' 'nm.example.org, O=Mnidom' '*.example.org' ' nm.example.org' ''; do
  render --ra-region eu-west-1 --trust-anchor-id "$ARN" --cert-cn "$cn" --out "$T/pol5" && fail "bad CN accepted: '$cn'"
  grep -q -- '--cert-cn' "$T/out" || fail "bad CN message: '$cn'"
done
[[ ! -e $T/pol5 ]] || [[ -z "$(ls -A "$T/pol5")" ]] || fail "files written despite an error"
ok "render-policies.py: trust anchor id or ARN (cross-checked), certificate CN validated and echoed"

echo "all $pass checks passed"
