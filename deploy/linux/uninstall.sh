#!/usr/bin/env bash
# =============================================================================
# NetworkMonitor — native Linux uninstaller
#
#   sudo ./deploy/linux/uninstall.sh            # remove the service and the app, KEEP data + DB
#   sudo ./deploy/linux/uninstall.sh --purge    # also drop the database, role, data dir and user
#
# PostgreSQL, TimescaleDB and Python packages are left installed (other software may use
# them). Remove them with your package manager if you no longer need them.
# =============================================================================
set -Eeuo pipefail

readonly APP_USER=netmon
readonly INSTALL_DIR=/opt/netmon
readonly DATA_DIR=/var/lib/netmon
readonly ENV_FILE="${DATA_DIR}/netmon.env"
readonly UNIT_PATH=/etc/systemd/system/netmon.service
readonly DROPIN_DIR=/etc/systemd/system/netmon.service.d
readonly DB_NAME=netmon
readonly DB_USER=netmon
readonly HBA_MARKER="# netmon: added by NetworkMonitor installer"

PURGE=0
ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1; shift ;;
    -y | --yes) ASSUME_YES=1; shift ;;
    -h | --help)
      echo "Usage: sudo $0 [--purge] [--yes]"
      echo "  --purge  also delete the database '${DB_NAME}', role '${DB_USER}', ${DATA_DIR} and user '${APP_USER}'"
      echo "  --yes    do not ask for confirmation"
      exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

log() { printf '==> %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo $0)." >&2; exit 1; }

if [ "$PURGE" -eq 1 ] && [ "$ASSUME_YES" -ne 1 ]; then
  printf 'This permanently deletes ALL NetworkMonitor data (database %s and %s). Type "yes" to continue: ' "$DB_NAME" "$DATA_DIR"
  read -r answer
  [ "$answer" = yes ] || { echo "Aborted."; exit 1; }
fi

# Port from the installation (to close the firewall), default 8080.
PORT=8080
if [ -f "$ENV_FILE" ]; then
  p="$(sed -n 's/^NETMON_PORT=\([0-9][0-9]*\)$/\1/p' "$ENV_FILE" | head -n1)"
  PORT="${p:-8080}"
fi

log "Stopping and removing the netmon service"
if systemctl list-unit-files netmon.service >/dev/null 2>&1; then
  systemctl disable --now netmon.service >/dev/null 2>&1 || true
fi
rm -f "$UNIT_PATH"
rm -rf "$DROPIN_DIR"
systemctl daemon-reload || true
systemctl reset-failed netmon.service >/dev/null 2>&1 || true

log "Removing ${INSTALL_DIR}"
rm -rf "$INSTALL_DIR"

if have firewall-cmd && firewall-cmd --state >/dev/null 2>&1; then
  if firewall-cmd -q --permanent --query-port="${PORT}/tcp"; then
    log "Closing TCP ${PORT} in firewalld"
    firewall-cmd -q --permanent --remove-port="${PORT}/tcp" || true
    firewall-cmd -q --reload || true
  fi
elif have ufw && ufw status 2>/dev/null | grep -q '^Status: active'; then
  ufw delete allow "${PORT}/tcp" >/dev/null 2>&1 || true
fi

if [ "$PURGE" -eq 1 ]; then
  # Locate psql: Debian wrapper, PGDG RPM layout, or PATH.
  PSQL="$(command -v psql || true)"
  if [ -z "$PSQL" ]; then
    PSQL="$(find /usr/pgsql-*/bin /usr/lib/postgresql/*/bin -maxdepth 1 -name psql 2>/dev/null | sort -V | tail -n1 || true)"
  fi
  if [ -n "$PSQL" ] && id -u postgres >/dev/null 2>&1; then
    psql_super() { (cd / && runuser -u postgres -- "$PSQL" -X -q "$@"); }
    if psql_super -Atc 'SELECT 1' >/dev/null 2>&1; then
      log "Dropping database ${DB_NAME} and role ${DB_USER}"
      psql_super -d postgres -c "DROP DATABASE IF EXISTS ${DB_NAME} WITH (FORCE)" \
        || psql_super -d postgres -c "DROP DATABASE IF EXISTS ${DB_NAME}" || warn "could not drop database"
      psql_super -d postgres -c "DROP ROLE IF EXISTS ${DB_USER}" || warn "could not drop role"
      HBA_FILE="$(psql_super -Atc 'SHOW hba_file' || true)"
      if [ -n "$HBA_FILE" ] && grep -qF "$HBA_MARKER" "$HBA_FILE"; then
        log "Removing the netmon lines from ${HBA_FILE}"
        sed '/^# netmon: added by NetworkMonitor installer$/,/^# netmon: end$/d' "$HBA_FILE" >"${HBA_FILE}.tmp"
        cat "${HBA_FILE}.tmp" >"$HBA_FILE"
        rm -f "${HBA_FILE}.tmp" "${HBA_FILE}.netmon.bak"
        psql_super -c 'SELECT pg_reload_conf()' >/dev/null || true
      fi
    else
      warn "PostgreSQL is not running; database '${DB_NAME}' was not dropped."
    fi
  else
    warn "psql not found; database '${DB_NAME}' was not dropped."
  fi

  log "Removing ${DATA_DIR} and the ${APP_USER} user"
  rm -rf "$DATA_DIR"
  if id -u "$APP_USER" >/dev/null 2>&1; then
    userdel "$APP_USER" >/dev/null 2>&1 || warn "could not delete user ${APP_USER}"
  fi
  if getent group "$APP_USER" >/dev/null 2>&1; then groupdel "$APP_USER" >/dev/null 2>&1 || true; fi
  echo "NetworkMonitor was removed completely."
else
  echo "NetworkMonitor was removed. Data kept: ${DATA_DIR} and database '${DB_NAME}'."
  echo "Re-running install.sh reuses them; run '$0 --purge' to delete them."
fi
