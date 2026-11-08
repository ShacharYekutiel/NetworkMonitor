#!/bin/sh
# git credential helper for the production clone: answers with the fine-grained token
# stored root-only in /etc/netmon-gitops/repo-token (never written into .git/config).
# Installed to /usr/local/lib/netmon-gitops/ by bootstrap.sh.
cat >/dev/null
[ "${1:-}" = get ] || exit 0
f="${NETMON_ETC_DIR:-/etc/netmon-gitops}/repo-token"
[ -r "$f" ] || exit 0
printf 'username=x-access-token\npassword=%s\n' "$(tr -d '\r\n' <"$f")"
