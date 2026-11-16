#!/usr/bin/env bash
# Exercise ops/sync.sh without Docker: a local bare "origin", a clone, fake docker/curl
# (tests/fake-bin). Covers: first deploy, no-op runs, code-only commits never deploying,
# a failing release (pre-deploy dump, rollback, bad mark, no retry, single notification),
# a fixed release, drift repair, invalid release.env, revert, --force, pull failure and
# the flock guard.
#
#   deploy/production/tests/test-sync.sh        (needs bash, git, flock)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROD_SRC="$(cd "$HERE/.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

export FAKE_STATE="$T/fake" NETMON_ALLOW_NON_ROOT=1 NETMON_INSTALL_OPS=0
export NETMON_REPO_DIR="$T/clone" NETMON_ETC_DIR="$T/etc" NETMON_STATE_DIR="$T/state"
export NETMON_BACKUP_DIR="$T/backups" NETMON_LOCK_FILE="$T/lock"
export NETMON_HEALTH_TIMEOUT=2 NETMON_HEALTH_POLL=1
export DOCKER="$HERE/fake-bin/docker" CURL="$HERE/fake-bin/curl" CONNTRACK="$HERE/fake-bin/conntrack"
export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=t@example.com GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=t@example.com
unset JOURNAL_STREAM
mkdir -p "$FAKE_STATE" "$NETMON_ETC_DIR/tls"
echo NETMON_FQDN=netmonitor.test >"$NETMON_ETC_DIR/site.env"
echo cert >"$NETMON_ETC_DIR/tls/cert.pem"
echo key >"$NETMON_ETC_DIR/tls/key.pem"
echo 'NOTIFY_WEBHOOK_URL=https://notify.example/hook' >"$NETMON_ETC_DIR/notify.env"

# origin (bare) + a work tree to push from, + the server clone
git init -q --bare -b main "$T/origin.git"
git clone -q "$T/origin.git" "$T/work" 2>/dev/null
mkdir -p "$T/work/deploy" "$T/work/src"
cp -a "$PROD_SRC" "$T/work/deploy/production"
# a fixed starting release, independent of the real (digest-pinned) release.env
printf 'NETMON_VERSION=1.0.0\nNETMON_IMAGE=ghcr.io/shacharyekutiel/networkmonitor:1.0.0\n' \
  >"$T/work/deploy/production/release.env"
echo v1 >"$T/work/src/app.py"
git -C "$T/work" add -A
git -C "$T/work" commit -qm initial
git -C "$T/work" push -q origin main 2>/dev/null
git clone -q "$T/origin.git" "$NETMON_REPO_DIR"

SYNC="$PROD_SRC/ops/sync.sh"
pass=0
ok() { pass=$((pass + 1)); echo "ok $pass - $*"; }
fail() { echo "FAIL - $*" >&2; exit 1; }

# [1.4.2] check-release.sh requires a digest; the fake releases below use plain tags.
CHECK="$PROD_SRC/ops/check-release.sh"
if "$CHECK" "$T/work/deploy/production/release.env" >/dev/null 2>&1; then
  fail "check-release.sh accepted an image without a digest"
fi
NETMON_ALLOW_UNPINNED_IMAGE=1 "$CHECK" "$T/work/deploy/production/release.env" >/dev/null 2>&1 ||
  fail "NETMON_ALLOW_UNPINNED_IMAGE=1 did not allow a tag-only image"
"$CHECK" "$PROD_SRC/release.env" >/dev/null || fail "the real release.env is not digest-pinned"
ok "check-release.sh: digest required, documented override works"
export NETMON_ALLOW_UNPINNED_IMAGE=1
run() { # expected-exit args...
  local want=$1 rc=0
  shift
  : >"$FAKE_STATE/calls"
  "$SYNC" "$@" >"$T/out" 2>&1 || rc=$?
  [[ $rc == "$want" ]] || { cat "$T/out" >&2; fail "sync $* exited $rc, expected $want"; }
}
called() { grep -q -- "$1" "$FAKE_STATE/calls"; }
decision() { "$SYNC" --dry-run 2>/dev/null | sed -n 's/^action=//p'; }
nnotify() { wc -l <"$FAKE_STATE/notifications" 2>/dev/null || echo 0; }
set_release() { # version
  sed -i "s/^NETMON_VERSION=.*/NETMON_VERSION=$1/; s#^NETMON_IMAGE=.*#NETMON_IMAGE=ghcr.io/shacharyekutiel/networkmonitor:$1#" \
    "$T/work/deploy/production/release.env"
  git -C "$T/work" commit -qam "release $1"
  git -C "$T/work" push -q origin main 2>/dev/null
}
code_commit() {
  echo "$RANDOM" >>"$T/work/src/app.py"
  git -C "$T/work" commit -qam "code change"
  git -C "$T/work" push -q origin main 2>/dev/null
}
head_origin() { git -C "$T/work" rev-parse HEAD; }
img() { cat "$FAKE_STATE/netmon_image"; }
IMG=ghcr.io/shacharyekutiel/networkmonitor

# 1. first deploy
[[ "$(decision)" == deploy ]] || fail "dry-run should say deploy"
[[ ! -s $FAKE_STATE/calls ]] || ! grep -q 'compose' "$FAKE_STATE/calls" || fail "dry-run touched docker"
ok "dry-run decides deploy without calling docker"
run 0
called "compose.*up -d --remove-orphans" || fail "no up"
[[ "$(cat "$NETMON_STATE_DIR/deployed_rev")" == "$(head_origin)" ]] || fail "deployed_rev"
[[ "$(img)" == "$IMG:1.0.0" ]] || fail "image"
ls "$NETMON_BACKUP_DIR"/pre-deploy-* >/dev/null 2>&1 && fail "pre-deploy dump without a DB"
ok "first deploy: up, healthy, deployed_rev recorded, no dump (no DB yet)"
for p in 162 514 2055 4739 6343; do
  grep -qx -- "-D -p udp --dport $p" "$FAKE_STATE/conntrack" || fail "UDP conntrack entries for $p not cleared after netmon was (re)created"
done
: >"$FAKE_STATE/conntrack"
ok "netmon (re)created: stale UDP conntrack entries for traps/syslog/flows cleared"

# 2. nothing changed
run 0
called "up -d" && fail "redeployed without a change"
[[ -s $FAKE_STATE/conntrack ]] && fail "conntrack flushed without a recreated container"
ok "unchanged: no deploy"

# 3. code-only commit
code_commit
[[ "$(decision)" == check ]] || fail "code-only commit must not deploy"
run 0
called "pull" && fail "code-only commit pulled"
ok "code-only commit on main: no deploy"

# 4. a release that fails its health check
GOOD_REV="$(cat "$NETMON_STATE_DIR/deployed_rev")"
set_release 1.1.0
BAD_TREE="$(git -C "$T/work" rev-parse HEAD:deploy/production)"
export FAKE_BAD_IMAGE="$IMG:1.1.0"
run 1
ls "$NETMON_BACKUP_DIR"/pre-deploy-*.dump >/dev/null || fail "no pre-deploy dump"
grep -q "^$BAD_TREE " "$NETMON_STATE_DIR/bad_trees" || fail "bad tree not marked"
[[ "$(cat "$NETMON_STATE_DIR/deployed_rev")" == "$GOOD_REV" ]] || fail "deployed_rev changed"
[[ "$(img)" == "$IMG:1.0.0" ]] || fail "not rolled back: $(img)"
[[ "$(git -C "$NETMON_REPO_DIR" rev-parse HEAD)" == "$GOOD_REV" ]] || fail "checkout not rolled back"
grep -q FAILED "$FAKE_STATE/notifications" || fail "no failure notification"
grep -q "Rolled back" "$FAKE_STATE/notifications" || fail "notification lacks rollback result"
ok "failed release: pre-deploy dump, marked bad, rolled back to 1.0.0, notified"

n0="$(nnotify)"
run 0
called "pull" && fail "bad rev retried"
[[ "$(nnotify)" == "$n0" ]] || fail "notified again"
code_commit
run 0
called "pull" && fail "bad rev retried after a code-only commit"
ok "bad change not retried (also after a code-only commit), no repeated notification"

# 5. fixed release
unset FAKE_BAD_IMAGE
set_release 1.1.1
run 0
[[ "$(img)" == "$IMG:1.1.1" ]] || fail "1.1.1 not deployed"
[[ "$(cat "$NETMON_STATE_DIR/deployed_rev")" == "$(head_origin)" ]] || fail "deployed_rev"
ok "new change after a bad one deploys (1.1.1)"

# 6. drift
rm -f "$FAKE_STATE/run/netmon"
run 0
called "up -d --remove-orphans" || fail "drift not repaired"
[[ -f $FAKE_STATE/run/netmon ]] || fail "netmon not running"
ok "drift (netmon stopped) repaired"

# 7. invalid release.env (latest) -> bad, no pull; revert -> nothing to do
sed -i "s#^NETMON_IMAGE=.*#NETMON_IMAGE=$IMG:latest#" "$T/work/deploy/production/release.env"
git -C "$T/work" commit -qam "bad: latest"
git -C "$T/work" push -q origin main 2>/dev/null
run 1
called "pull" && fail "pulled an invalid release"
grep -q "invalid" "$NETMON_STATE_DIR/bad_trees" || fail "invalid release not marked bad"
git -C "$T/work" revert --no-edit HEAD >/dev/null
git -C "$T/work" push -q origin main 2>/dev/null
[[ "$(decision)" == check ]] || fail "revert to the deployed content should be a no-op"
run 0
called "pull" && fail "revert redeployed"
ok "':latest' rejected and marked bad; git revert back to the deployed content is a no-op"

# 8. --force retries a bad tree
set_release 1.2.0
export FAKE_BAD_IMAGE="$IMG:1.2.0"
run 1
run 0
called "pull" && fail "retried without --force"
unset FAKE_BAD_IMAGE
run 0 --force
[[ "$(img)" == "$IMG:1.2.0" ]] || fail "--force did not deploy"
ok "--force retries a change marked bad"

# 9. pull failure: not marked bad, notified once, retried
set_release 1.3.0
export FAKE_PULL_FAIL=1
n0="$(nnotify)"
run 1
run 1
[[ "$(nnotify)" == $((n0 + 1)) ]] || fail "pull failure notifications: $(($(nnotify) - n0))"
grep -q "$(git -C "$T/work" rev-parse HEAD:deploy/production)" "$NETMON_STATE_DIR/bad_trees" && fail "pull failure marked bad"
unset FAKE_PULL_FAIL
run 0
[[ "$(img)" == "$IMG:1.3.0" ]] || fail "not deployed after the registry came back"
ok "pull failure: retried every run, one notification, deploys once the pull works"

# 10. flock: a second run while one holds the lock exits 0 and does nothing
flock "$NETMON_LOCK_FILE" sleep 3 &
sleep 0.5
run 0
grep -q "skipping" "$T/out" || fail "lock not honoured"
called "compose" && fail "ran docker while locked"
wait
ok "concurrent run skipped (flock)"

echo "all $pass checks passed"
