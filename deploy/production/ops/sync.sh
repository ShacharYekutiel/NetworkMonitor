#!/usr/bin/env bash
# NetworkMonitor GitOps sync — run by netmon-gitops.service (timer: every 2 minutes).
#
# 1. git fetch origin/main in /opt/netmon (the code repo clone).
# 2. Deploy ONLY when the content of deploy/production/ on origin/main differs from what is
#    deployed (compared by git tree hash = `git diff --quiet <deployed>..origin/main --
#    deploy/production`). Code-only merges never deploy.
# 3. Otherwise check for drift (a service not running, wrong netmon image) and repair it.
#
# Deploy: checkout -> validate (check-release.sh, compose config) -> pg_dump BEFORE the
# netmon image changes (/var/backups/netmon/pre-deploy-<rev>-<ts>.dump) -> pull -> up -d
# --remove-orphans -> wait for health (containers + https://<fqdn>/api/health) -> clear
# stale UDP conntrack entries for traps/syslog/flows (162/514/2055/4739/6343) when netmon
# was recreated.
# On failure: the deploy/production tree is marked bad (not retried until a NEW change
# lands under deploy/production) and the previous good revision is redeployed.
# The database is NEVER restored automatically (migrations are forward-only); see
# ops/restore.sh.
#
# Usage: sync.sh [--dry-run] [--force]
#   --dry-run  fetch and print the decision only (no docker, no state changes)
#   --force    retry origin/main even if its deploy/production tree is marked bad
#
# State: /var/lib/netmon-gitops/{deployed_rev,deployed_tree,bad_trees,history.log}
# Logs:  journalctl -u netmon-gitops
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$SELF_DIR/lib.sh"

DRY_RUN=0
FORCE=0
for a in "$@"; do
  case "$a" in
    --dry-run | --check) DRY_RUN=1 ;;
    --force) FORCE=1 ;;
    -h | --help)
      sed -n '2,25p' "$0"
      exit 0
      ;;
    *) die "unknown argument: $a" ;;
  esac
done

require_root
mkdir -p "$NETMON_STATE_DIR"
chmod 0700 "$NETMON_STATE_DIR"

# One run at a time (the timer, a manual run, backup.sh and restore.sh share the lock).
exec 9>"$NETMON_LOCK_FILE"
if ! flock -n 9; then
  log "another netmon-gitops run holds $NETMON_LOCK_FILE; skipping"
  exit 0
fi

# ------------------------------------------------------------------ state helpers
state_get() { cat "$NETMON_STATE_DIR/$1" 2>/dev/null || true; }
state_set() { printf '%s\n' "$2" >"$NETMON_STATE_DIR/$1.tmp" && mv "$NETMON_STATE_DIR/$1.tmp" "$NETMON_STATE_DIR/$1"; }
state_del() { rm -f "$NETMON_STATE_DIR/$1"; }
hist() { printf '%s %s\n' "$(date -u '+%FT%TZ')" "$*" >>"$NETMON_STATE_DIR/history.log"; }
is_bad() { [[ -n $1 ]] && grep -q "^$1 " "$NETMON_STATE_DIR/bad_trees" 2>/dev/null; }
mark_bad() { # tree rev reason
  is_bad "$1" || printf '%s %s %s %s\n' "$1" "$2" "$(date -u '+%FT%TZ')" "$3" >>"$NETMON_STATE_DIR/bad_trees"
}
unmark_bad() {
  [[ -f $NETMON_STATE_DIR/bad_trees ]] || return 0
  grep -v "^$1 " "$NETMON_STATE_DIR/bad_trees" >"$NETMON_STATE_DIR/bad_trees.tmp" || true
  mv "$NETMON_STATE_DIR/bad_trees.tmp" "$NETMON_STATE_DIR/bad_trees"
}
# Notify only once per key (avoid a message every 2 minutes for the same problem).
notify_once() { # key message
  [[ "$(state_get notified)" == "$1" ]] && return 0
  state_set notified "$1"
  notify "$2"
}

git_() { git -C "$NETMON_REPO_DIR" "$@"; }
tree_of() { git_ rev-parse -q --verify "$1:$NETMON_PROD_SUBDIR" 2>/dev/null || true; }
short() { printf '%s' "${1:0:10}"; }
checkout() {
  git_ -c advice.detachedHead=false checkout -q --force --detach "$1"
}

# ------------------------------------------------------------------ fetch + decide
load_proxy_env
[[ -d $NETMON_REPO_DIR/.git ]] || die "$NETMON_REPO_DIR is not a git clone (run ops/bootstrap.sh)"

if timeout 120 git -C "$NETMON_REPO_DIR" fetch --quiet --prune origin \
  "+refs/heads/$NETMON_BRANCH:refs/remotes/origin/$NETMON_BRANCH"; then
  state_del fetch_failures
else
  fails=$(($(state_get fetch_failures || echo 0) + 0 + 1))
  state_set fetch_failures "$fails"
  warn "git fetch failed ($fails in a row); using the last fetched origin/$NETMON_BRANCH"
  ((fails == 15)) && notify "git fetch has failed for 30 minutes (token expired? proxy?) - see journalctl -u netmon-gitops"
fi

target_rev="$(git_ rev-parse -q --verify "refs/remotes/origin/$NETMON_BRANCH^{commit}")" ||
  die "origin/$NETMON_BRANCH not found"
target_tree="$(tree_of "$target_rev")"
[[ -n $target_tree ]] || die "origin/$NETMON_BRANCH has no $NETMON_PROD_SUBDIR/"
deployed_rev="$(state_get deployed_rev)"
deployed_tree="$(state_get deployed_tree)"

if ((FORCE)) && is_bad "$target_tree"; then
  log "--force: clearing the bad mark of tree $(short "$target_tree")"
  ((DRY_RUN)) || unmark_bad "$target_tree"
  FORCED_CLEAR=1
fi

if [[ $target_tree == "$deployed_tree" ]]; then
  action=check
  reason="deploy/production unchanged since $(short "$deployed_rev")"
elif is_bad "$target_tree" && [[ -z ${FORCED_CLEAR:-} ]]; then
  action=check
  reason="origin/$NETMON_BRANCH $(short "$target_rev") has a deploy/production change that already failed (marked bad); waiting for a new change"
else
  action=deploy
  reason="deploy/production changed: $(short "${deployed_rev:-none}") -> $(short "$target_rev")"
fi

if ((DRY_RUN)); then
  printf 'target_rev=%s\ntarget_tree=%s\ndeployed_rev=%s\ndeployed_tree=%s\naction=%s\nreason=%s\n' \
    "$target_rev" "$target_tree" "${deployed_rev:-}" "${deployed_tree:-}" "$action" "$reason"
  exit 0
fi

# ------------------------------------------------------------------ preflight
preflight() {
  load_site_env
  [[ -n ${NETMON_FQDN:-} ]] || {
    err "NETMON_FQDN missing in $NETMON_ETC_DIR/site.env (run ops/bootstrap.sh)"
    return 1
  }
  [[ -s $NETMON_ETC_DIR/tls/cert.pem && -s $NETMON_ETC_DIR/tls/key.pem ]] || {
    err "TLS cert/key missing in $NETMON_ETC_DIR/tls (run ops/bootstrap.sh)"
    return 1
  }
  mkdir -p "$NETMON_ETC_DIR/ca"
}

validate() {
  "$NETMON_PROD_DIR/ops/check-release.sh" "$NETMON_PROD_DIR/release.env" >/dev/null &&
    compose config -q
}

db_volume_exists() {
  [[ -n "$("$DOCKER" volume ls -q --filter label=com.docker.compose.project=netmon \
    --filter label=com.docker.compose.volume=netmon-db 2>/dev/null)" ]]
}

ensure_db_up() {
  service_running db && [[ "$(service_state db)" == healthy ]] && return 0
  log "starting db for the pre-deploy backup"
  compose up -d db || return 1
  local deadline=$((SECONDS + 180))
  while ((SECONDS < deadline)); do
    [[ "$(service_state db)" == healthy ]] && return 0
    sleep "$NETMON_HEALTH_POLL"
  done
  return 1
}

pre_deploy_backup() { # rev -> echoes the dump path
  local f
  f="$NETMON_BACKUP_DIR/pre-deploy-$(short "$1")-$(date '+%Y%m%d-%H%M%S').dump"
  ensure_db_up >&2 || return 1
  db_dump "$f" >&2 || return 1
  printf '%s' "$f"
}

prune_pre_deploy() { # keep the 10 newest pre-deploy dumps
  local f
  find "$NETMON_BACKUP_DIR" -maxdepth 1 -name 'pre-deploy-*.dump' -printf '%T@ %p\n' 2>/dev/null |
    sort -rn | tail -n +11 | while read -r _ f; do rm -f -- "$f" && log "pruned $f"; done || true
}

diagnostics() {
  compose ps -a 2>&1 | while IFS= read -r l; do log "ps: $l"; done || true
  compose logs --no-color --tail 60 netmon proxy 2>&1 | while IFS= read -r l; do log "logs: $l"; done || true
}

# Install this revision's ops scripts and units (the timer runs the installed copy, so a
# broken script in git cannot break the updater before it has been deployed healthy).
install_ops() {
  [[ ${NETMON_INSTALL_OPS:-1} == 1 ]] || return 0
  local f changed=0 dst
  mkdir -p "$NETMON_LIB_DIR"
  for f in "$NETMON_PROD_DIR"/ops/*.sh; do
    bash -n "$f" || {
      warn "not installing $f (syntax error)"
      continue
    }
    dst="$NETMON_LIB_DIR/$(basename "$f")"
    cmp -s "$f" "$dst" && continue
    install -m 0755 "$f" "$dst.new" && mv -f "$dst.new" "$dst" # atomic: running bash keeps the old inode
    log "installed $dst"
  done
  for f in "$NETMON_PROD_DIR"/systemd/*; do
    dst="$NETMON_SYSTEMD_DIR/$(basename "$f")"
    cmp -s "$f" "$dst" && continue
    install -m 0644 "$f" "$dst"
    changed=1
    log "installed $dst"
  done
  if ((changed)); then systemctl daemon-reload || warn "systemctl daemon-reload failed"; fi
}

# ------------------------------------------------------------------ deploy
rollback() { # rev tree why dump
  local rev=$1 tree=$2 why=$3 dump=$4 msg
  err "deploy of $(short "$rev") failed: $why"
  mark_bad "$tree" "$rev" "$why"
  hist "FAILED rev=$rev tree=$tree reason=$why"
  diagnostics
  msg="Deploy of $(short "$rev") FAILED ($why). It will not be retried until a new change lands in deploy/production."
  [[ -n $dump ]] && msg+=" Pre-deploy DB dump: $dump."
  if [[ -n $deployed_rev && $deployed_rev != "$rev" ]]; then
    log "rolling back to the previous good revision $(short "$deployed_rev")"
    checkout "$deployed_rev"
    if compose up -d --remove-orphans && wait_healthy; then
      hist "ROLLBACK ok rev=$deployed_rev"
      notify "$msg Rolled back to $(short "$deployed_rev"): healthy. The database was NOT restored."
    else
      hist "ROLLBACK FAILED rev=$deployed_rev"
      diagnostics
      notify "$msg ROLLBACK to $(short "$deployed_rev") ALSO FAILED - manual action needed (the old version may not run on the migrated DB: see ops/restore.sh)."
    fi
  else
    checkout "$rev" # keep the files that match the running containers
    notify "$msg No previous good revision to roll back to."
  fi
  return 1
}

deploy() { # rev tree
  local rev=$1 tree=$2 new_image cur_image dump=""
  log "$reason"
  checkout "$rev"

  if ! preflight; then
    [[ -n $deployed_rev ]] && checkout "$deployed_rev"
    notify_once "preflight-$tree" "Deploy of $(short "$rev") blocked: server setup incomplete (see journalctl -u netmon-gitops)."
    return 1
  fi
  if ! validate; then
    mark_bad "$tree" "$rev" "invalid release.env or compose.yaml"
    hist "INVALID rev=$rev tree=$tree"
    [[ -n $deployed_rev ]] && checkout "$deployed_rev"
    notify "Deploy of $(short "$rev") rejected: invalid release.env or compose.yaml (marked bad)."
    return 1
  fi

  new_image="$(env_get "$NETMON_PROD_DIR/release.env" NETMON_IMAGE)"
  cur_image="$(running_image netmon || true)"
  if [[ $cur_image != "$new_image" ]] && db_volume_exists; then
    log "netmon image changes (${cur_image:-none} -> $new_image): taking a pre-deploy backup"
    if ! dump="$(pre_deploy_backup "$rev")"; then
      [[ -n $deployed_rev ]] && checkout "$deployed_rev"
      notify_once "backup-$tree" "Deploy of $(short "$rev") postponed: the pre-deploy backup failed. Will retry."
      return 1
    fi
  fi

  local i ok=0
  for i in 1 2 3; do
    if compose pull --quiet; then
      ok=1
      break
    fi
    warn "docker compose pull failed (attempt $i)"
    sleep $((i * 5))
  done
  if ((!ok)); then
    # Registry/proxy/token problem or a tag that does not exist yet: nothing changed on the
    # server; retried on the next run (not marked bad).
    [[ -n $deployed_rev ]] && checkout "$deployed_rev"
    notify_once "pull-$tree" "Deploy of $(short "$rev") waiting: cannot pull $new_image (ghcr token? proxy? tag missing?). Retrying every run."
    return 1
  fi

  local old_cid
  old_cid="$(service_cid netmon)"
  if ! compose up -d --remove-orphans; then
    rollback "$rev" "$tree" "docker compose up failed" "$dump"
    return 1
  fi
  if ! wait_healthy; then
    rollback "$rev" "$tree" "not healthy within ${NETMON_HEALTH_TIMEOUT}s" "$dump"
    return 1
  fi
  flush_ingest_conntrack "$old_cid"

  state_set deployed_rev "$rev"
  state_set deployed_tree "$tree"
  state_del notified
  hist "DEPLOYED rev=$rev tree=$tree image=$new_image"
  log "deployed $(short "$rev") ($new_image): healthy"
  install_ops
  prune_pre_deploy
  if [[ "$(env_get "$NETMON_ETC_DIR/notify.env" NOTIFY_ON_SUCCESS 2>/dev/null)" == 1 ]]; then
    notify "Deployed $(short "$rev"): $new_image (healthy)."
  fi
}

# ------------------------------------------------------------------ drift check
check() {
  if [[ -z $deployed_rev ]]; then
    log "nothing deployed yet; $reason"
    return 0
  fi
  [[ $action == check && $target_tree != "$deployed_tree" ]] && log "$reason"

  # Keep the checkout on the deployed revision (someone may have run `git pull` by hand).
  if [[ "$(tree_of HEAD)" != "$deployed_tree" ]] || ! git_ diff --quiet HEAD -- "$NETMON_PROD_SUBDIR"; then
    warn "checkout differs from the deployed revision; resetting to $(short "$deployed_rev")"
    checkout "$deployed_rev"
  fi

  local drift=() svc want have
  for svc in "${NETMON_SERVICES[@]}"; do
    service_running "$svc" || drift+=("$svc not running")
  done
  want="$(env_get "$NETMON_PROD_DIR/release.env" NETMON_IMAGE)"
  have="$(running_image netmon || true)"
  [[ -n $have && $have != "$want" ]] && drift+=("netmon image $have != $want")
  ((${#drift[@]} == 0)) && return 0

  warn "drift: ${drift[*]}; re-applying $(short "$deployed_rev")"
  preflight || return 1
  local old_cid
  old_cid="$(service_cid netmon)"
  if compose up -d --remove-orphans && wait_healthy; then
    flush_ingest_conntrack "$old_cid"
    hist "DRIFT repaired (${drift[*]}) rev=$deployed_rev"
    log "drift repaired"
    state_del notified
  else
    hist "DRIFT repair FAILED (${drift[*]}) rev=$deployed_rev"
    diagnostics
    notify_once "drift-$deployed_tree" "Stack drifted (${drift[*]}) and re-applying $(short "$deployed_rev") did not make it healthy - manual check needed."
    return 1
  fi
}

case "$action" in
  deploy) deploy "$target_rev" "$target_tree" ;;
  check) check ;;
esac
