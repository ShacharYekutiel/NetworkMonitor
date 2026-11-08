#!/usr/bin/env bash
# Validate release.env: only known keys, NETMON_VERSION is X.Y.Z[-pre], NETMON_IMAGE is
# exactly ghcr.io/shacharyekutiel/networkmonitor:<NETMON_VERSION>[@sha256:<64 hex>].
# Never ":latest". Used by sync.sh before every deploy and by .github/workflows/production.yml.
#
#   check-release.sh [path/to/release.env]
set -euo pipefail

FILE="${1:-$(dirname "$0")/../release.env}"
REPO="${NETMON_IMAGE_REPO:-ghcr.io/shacharyekutiel/networkmonitor}"
fail() {
  echo "release.env: $*" >&2
  exit 1
}

[[ -r $FILE ]] || fail "cannot read $FILE"

version="" image="" n=0
while IFS= read -r line || [[ -n $line ]]; do
  n=$((n + 1))
  line="${line%$'\r'}"
  [[ -z ${line//[[:space:]]/} || $line =~ ^[[:space:]]*# ]] && continue
  [[ $line =~ ^([A-Z_]+)=([^[:space:]\"\']*)$ ]] || fail "line $n: expected KEY=value without quotes or spaces: $line"
  key="${BASH_REMATCH[1]}" val="${BASH_REMATCH[2]}"
  case "$key" in
    NETMON_VERSION) version="$val" ;;
    NETMON_IMAGE) image="$val" ;;
    *) fail "line $n: unknown key $key (only NETMON_VERSION and NETMON_IMAGE)" ;;
  esac
done <"$FILE"

[[ -n $version ]] || fail "NETMON_VERSION is missing"
[[ -n $image ]] || fail "NETMON_IMAGE is missing"
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] ||
  fail "NETMON_VERSION=$version is not X.Y.Z (no leading v)"
expected="$REPO:$version"
[[ $image == "$expected" || $image =~ ^"$expected"@sha256:[0-9a-f]{64}$ ]] ||
  fail "NETMON_IMAGE=$image must be $expected or $expected@sha256:<64 hex> (never latest)"

echo "release.env OK: version $version, image $image"
