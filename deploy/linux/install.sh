#!/usr/bin/env bash
# =============================================================================
# NetworkMonitor — native Linux installer (systemd)
#
#   sudo ./deploy/linux/install.sh                 # from a repository checkout
#   sudo ./install.sh                              # from an extracted release tarball
#   sudo ./install.sh --tarball netmon-0.1.0.tar.gz  (or an https:// URL)
#   sudo ./install.sh --wheel netmon-0.1.0-py3-none-any.whl
#
# Supported: Debian 11+ / Ubuntu 22.04+ (apt) and RHEL / Rocky / Alma / CentOS Stream
# 8+ / Fedora (dnf). Safe to re-run: re-running upgrades the app in place, keeps the
# database, the data directory and the generated password.
#
# What it does (docs/architecture.md §8 "Native Linux"):
#   1. Installs Python >= 3.12 and PostgreSQL >= 14, then tries TimescaleDB from the
#      official Timescale repository. If TimescaleDB cannot be installed the app runs
#      in plain-PostgreSQL fallback mode (fully supported).
#   2. Creates the system user `netmon`, /opt/netmon/venv and /var/lib/netmon (0750).
#   3. Creates the DB role + database `netmon` with a random password and writes
#      /var/lib/netmon/netmon.env (DATABASE_URL=..., mode 0600).
#   4. Installs the app (with the web UI bundled) into the venv, with the `kerberos` extra
#      (python-gssapi, built against MIT krb5: libgssapi-krb5-2 / krb5-libs at runtime) for
#      Kerberos SSO. If gssapi cannot be built the app is installed without it (warning;
#      Kerberos SSO then reports "unavailable", everything else works).
#   5. Installs netmon.service (CAP_NET_RAW + CAP_NET_BIND_SERVICE ambient capabilities:
#      ping fallback, SNMP traps on UDP 162 and syslog on UDP/TCP 514), enables and starts it.
#      [3] The flow receivers (UDP 2055/4739/6343) are > 1024 and need no capability.
#   6. Prints the URL, the firewall lines for traps/syslog/flows and any port conflict
#      (snmptrapd / rsyslog / nfcapd already listening on 162 / 514 / 2055 / 4739 / 6343).
# Corporate TLS inspection: certificates in deploy/certs/ (see deploy/certs/README.md) are
# added to the system trust store first, so apt/dnf, pip, npm and the app trust them.
# Outbound proxy: when HTTPS_PROXY is set for the installer (sudo -E) or --https-proxy is
# given, the service gets it too (drop-in 20-proxy.conf: Telegram / Teams / Entra ID /
# webhooks; SSH, SNMP, flows and LDAP never use it). Kept on re-runs.
# =============================================================================
set -Eeuo pipefail

# ----------------------------------------------------------------------------- constants
readonly APP_USER=netmon
readonly INSTALL_DIR=/opt/netmon
readonly VENV="${INSTALL_DIR}/venv"
readonly DATA_DIR=/var/lib/netmon
readonly ENV_FILE="${DATA_DIR}/netmon.env"
readonly UNIT_PATH=/etc/systemd/system/netmon.service
readonly DROPIN_DIR=/etc/systemd/system/netmon.service.d
readonly DB_NAME=netmon
readonly DB_USER=netmon
readonly PG_WANTED_MAJOR=16          # used when PostgreSQL has to be installed
readonly PG_MIN_MAJOR=14             # date_bin() fallback needs >= 14
readonly HBA_MARKER="# netmon: added by NetworkMonitor installer"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR

# ----------------------------------------------------------------------------- options
OPT_TARBALL=""
OPT_WHEEL=""
OPT_SOURCE=""
OPT_PORT=""
OPT_TIMESCALE=1
OPT_FIREWALL=1
OPT_KERBEROS=1
OPT_HTTPS_PROXY="${HTTPS_PROXY:-${https_proxy:-}}"
OPT_NO_PROXY="${NO_PROXY:-${no_proxy:-}}"

usage() {
  cat <<EOF
Usage: sudo $0 [options]

Options:
  --tarball <file|url>  Install from a release tarball (contains a netmon-*.whl or a source tree)
  --wheel <file>        Install from a wheel file (the web UI must be bundled in it)
  --source <dir>        Install from a repository checkout (default: the checkout this
                        script lives in, or a wheel shipped next to it)
  --port <n>            HTTP port (default 8080, or the port of the existing installation)
  --no-timescale        Do not try to install TimescaleDB (plain PostgreSQL mode)
  --no-firewall         Do not open the HTTP port in firewalld / ufw (traps/syslog/flow
                        ports are never opened automatically; the installer prints the lines)
  --no-kerberos         Do not install the Kerberos SSO support (python-gssapi and the
                        compiler / krb5 headers needed to build it)
  --https-proxy <url>   Outbound HTTPS proxy for the service (Telegram, Teams, Entra ID,
                        webhooks), e.g. http://proxy.corp.local:8080. Default: HTTPS_PROXY
                        of the installer's environment, else the one set by an earlier run
  --no-proxy <list>     Extra NO_PROXY entries for the service (internal webhook targets),
                        e.g. .corp.local,10.0.0.0/8
  -h, --help            Show this help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --tarball) OPT_TARBALL="${2:?--tarball needs a value}"; shift 2 ;;
    --wheel) OPT_WHEEL="${2:?--wheel needs a value}"; shift 2 ;;
    --source) OPT_SOURCE="${2:?--source needs a value}"; shift 2 ;;
    --port) OPT_PORT="${2:?--port needs a value}"; shift 2 ;;
    --no-timescale) OPT_TIMESCALE=0; shift ;;
    --no-firewall) OPT_FIREWALL=0; shift ;;
    --no-kerberos) OPT_KERBEROS=0; shift ;;
    --https-proxy) OPT_HTTPS_PROXY="${2:?--https-proxy needs a value}"; shift 2 ;;
    --no-proxy) OPT_NO_PROXY="${2:?--no-proxy needs a value}"; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# ----------------------------------------------------------------------------- output
if [ -t 1 ]; then
  C_BLUE=$'\033[1;34m' C_YELLOW=$'\033[1;33m' C_RED=$'\033[1;31m' C_GREEN=$'\033[1;32m' C_OFF=$'\033[0m'
else
  C_BLUE="" C_YELLOW="" C_RED="" C_GREEN="" C_OFF=""
fi
log() { printf '%s==>%s %s\n' "$C_BLUE" "$C_OFF" "$*"; }
ok() { printf '%s  ok%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn() { printf '%sWARN:%s %s\n' "$C_YELLOW" "$C_OFF" "$*" >&2; }
die() { printf '%sERROR:%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }
on_error() { printf '%sERROR:%s installation failed at line %s (exit %s)\n' "$C_RED" "$C_OFF" "$1" "$2" >&2; }
trap 'on_error $LINENO $?' ERR

have() { command -v "$1" >/dev/null 2>&1; }

WORK="$(mktemp -d /tmp/netmon-install.XXXXXX)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

# ----------------------------------------------------------------------------- checks
[ "$(id -u)" -eq 0 ] || die "Run this script as root (sudo $0)."
[ -d /run/systemd/system ] || die "systemd is required (this host is not running systemd)."
[ -r /etc/os-release ] || die "/etc/os-release not found; unsupported distribution."

# shellcheck disable=SC1091
. /etc/os-release
OS_ID="${ID:-unknown}"
OS_LIKE="${ID_LIKE:-}"
OS_VERSION="${VERSION_ID:-0}"
# Ubuntu derivatives (Mint, Pop!_OS, ...) carry the upstream codename in UBUNTU_CODENAME.
OS_CODENAME="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
case " ${OS_ID} ${OS_LIKE} " in
  *" debian "* | *" ubuntu "*) FAMILY=debian ;;
  *" rhel "* | *" fedora "* | *" centos "*) FAMILY=rhel ;;
  *) die "Unsupported distribution '${OS_ID}'. Supported: Debian/Ubuntu and RHEL-family." ;;
esac
EL_MAJOR="${OS_VERSION%%.*}"
ARCH="$(uname -m)"
log "Detected ${PRETTY_NAME:-$OS_ID} (${FAMILY} family, ${ARCH})"

# ============================================================================= packages
APT_UPDATED=0
apt_install() {
  if [ "$APT_UPDATED" -eq 0 ]; then
    DEBIAN_FRONTEND=noninteractive apt-get update -q
    APT_UPDATED=1
  fi
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "$@"
}
dnf_install() { dnf install -y -q "$@"; }

# ============================================================================= corporate CA
# A TLS-inspecting firewall re-signs HTTPS with a private corporate root CA. Every
# *.crt / *.pem / *.cer in deploy/certs/ (see deploy/certs/README.md) is added to the
# system trust store, which apt/dnf/curl, pip and the app itself (truststore) use; npm and
# uv get it through NODE_EXTRA_CA_CERTS / SSL_CERT_FILE for the rest of this script.
readonly CA_SRC_DIR="${SCRIPT_DIR}/../certs"
readonly CA_IMPORT="${SCRIPT_DIR}/../import-ca-certs.sh"
CA_DONE=0
import_corporate_cas() {
  local dest update bundle before after
  if [ "$FAMILY" = debian ]; then
    dest=/usr/local/share/ca-certificates/netmon
    update="update-ca-certificates"
    bundle=/etc/ssl/certs/ca-certificates.crt
  else
    dest=/etc/pki/ca-trust/source/anchors
    update="update-ca-trust extract"
    bundle=/etc/pki/tls/certs/ca-bundle.crt
  fi
  have "${update%% *}" || return 0  # not installed yet: retried after the base tools
  CA_DONE=1
  [ -f "$CA_IMPORT" ] || return 0
  before="$(find "$dest" -maxdepth 1 -name 'netmon-*.crt' 2>/dev/null | wc -l || true)"
  sh "$CA_IMPORT" "$CA_SRC_DIR" "$dest" | sed 's/^/  /'
  after="$(find "$dest" -maxdepth 1 -name 'netmon-*.crt' 2>/dev/null | wc -l || true)"
  if [ "$before" -gt 0 ] || [ "$after" -gt 0 ]; then
    $update >/dev/null 2>&1 || warn "\"${update}\" failed; corporate CA certificates may not be trusted"
  fi
  if [ "$after" -gt 0 ] && [ -s "$bundle" ]; then
    export NODE_EXTRA_CA_CERTS="$bundle" SSL_CERT_FILE="$bundle" PIP_CERT="$bundle" UV_NATIVE_TLS=1
    ok "Trusting ${after} corporate CA certificate(s) from deploy/certs"
  fi
}
if [ -d "$CA_SRC_DIR" ]; then
  log "Checking deploy/certs for corporate CA certificates"
  import_corporate_cas
fi

log "Installing base tools"
if [ "$FAMILY" = debian ]; then
  apt_install ca-certificates curl gnupg tar gzip
else
  # Only what is missing: EL9 images ship curl-minimal, which conflicts with the curl package.
  RHEL_BASE=(ca-certificates)
  have curl || RHEL_BASE+=(curl)
  have tar || RHEL_BASE+=(tar)
  have gzip || RHEL_BASE+=(gzip)
  have find || RHEL_BASE+=(findutils)
  dnf_install "${RHEL_BASE[@]}"
fi
if [ -d "$CA_SRC_DIR" ] && [ "$CA_DONE" -eq 0 ]; then import_corporate_cas; fi

# ============================================================================= PostgreSQL
# Results of this section:
#   PG_MAJOR   PostgreSQL major version in use
#   PG_BIN     directory with psql
#   PG_UNIT    systemd unit to restart / order after
PG_MAJOR=""
PG_BIN=""
PG_UNIT=""
PG_FLAVOUR=""  # debian | pgdg-rpm | distro-rpm

psql_super() {
  # Run psql as the postgres superuser over the local socket. The `cd /` avoids
  # "could not change directory" warnings when the caller's cwd is not readable.
  (cd / && runuser -u postgres -- "${PG_BIN}/psql" -X -q -v ON_ERROR_STOP=1 "$@")
}

pg_wait_ready() {
  local _i
  for _i in $(seq 1 60); do
    if psql_super -Atc 'SELECT 1' >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}

add_pgdg_apt_repo() {
  # Official PostgreSQL apt repository (apt.postgresql.org).
  [ -n "$OS_CODENAME" ] || return 1
  install -d -m 0755 /usr/share/postgresql-common/pgdg || return 1
  curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    https://www.postgresql.org/media/keys/ACCC4CF8.asc || return 1
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${OS_CODENAME}-pgdg main" \
    >/etc/apt/sources.list.d/pgdg.list || return 1
  APT_UPDATED=0
}

install_postgres_debian() {
  PG_FLAVOUR=debian
  local existing distro_major
  existing="$(find /usr/lib/postgresql -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort -n | tail -n1 || true)"
  if [ -n "$existing" ] && [ "$existing" -ge "$PG_MIN_MAJOR" ] && [ -x "/usr/lib/postgresql/${existing}/bin/postgres" ]; then
    PG_MAJOR="$existing"
    ok "PostgreSQL ${PG_MAJOR} already installed"
  else
    apt_install postgresql-common >/dev/null 2>&1 || true
    # The distribution's default major version (the `postgresql` metapackage dependency).
    distro_major="$(apt-cache depends postgresql 2>/dev/null | sed -n 's/^ *Depends: postgresql-\([0-9][0-9]*\)$/\1/p' | head -n1 || true)"
    if [ -n "$distro_major" ] && [ "$distro_major" -ge "$PG_MIN_MAJOR" ]; then
      log "Installing PostgreSQL ${distro_major} (distribution packages)"
      apt_install postgresql postgresql-contrib
      PG_MAJOR="$distro_major"
    else
      log "Distribution PostgreSQL is older than ${PG_MIN_MAJOR}; using the official PGDG repository"
      add_pgdg_apt_repo || die "Could not add the PostgreSQL apt repository."
      apt_install "postgresql-${PG_WANTED_MAJOR}" "postgresql-contrib-${PG_WANTED_MAJOR}" 2>/dev/null \
        || apt_install "postgresql-${PG_WANTED_MAJOR}"
      PG_MAJOR="$PG_WANTED_MAJOR"
    fi
  fi
  PG_BIN="/usr/lib/postgresql/${PG_MAJOR}/bin"
  # The Debian packages create the "main" cluster automatically; create it if that was disabled.
  if ! pg_lsclusters -h 2>/dev/null | awk -v v="$PG_MAJOR" '$1 == v' | grep -q .; then
    log "Creating PostgreSQL ${PG_MAJOR} cluster 'main'"
    pg_createcluster "$PG_MAJOR" main
  fi
  local cluster
  cluster="$(pg_lsclusters -h | awk -v v="$PG_MAJOR" '$1 == v {print $2; exit}')"
  PG_UNIT="postgresql@${PG_MAJOR}-${cluster}.service"
  systemctl enable postgresql.service >/dev/null 2>&1 || true
  systemctl enable "$PG_UNIT" >/dev/null 2>&1 || true
  systemctl start "$PG_UNIT"
}

rpm_pgdg_major() {
  # Highest installed PGDG server package (postgresqlNN-server), if any.
  rpm -qa --qf '%{NAME}\n' 'postgresql*-server' 2>/dev/null \
    | sed -n 's/^postgresql\([0-9][0-9]*\)-server$/\1/p' | sort -n | tail -n1
}

add_pgdg_rpm_repo() {
  local url
  if [ "$OS_ID" = fedora ]; then
    url="https://download.postgresql.org/pub/repos/yum/reporpms/F-${EL_MAJOR}-${ARCH}/pgdg-fedora-repo-latest.noarch.rpm"
  else
    url="https://download.postgresql.org/pub/repos/yum/reporpms/EL-${EL_MAJOR}-${ARCH}/pgdg-redhat-repo-latest.noarch.rpm"
  fi
  rpm -q pgdg-redhat-repo >/dev/null 2>&1 || rpm -q pgdg-fedora-repo >/dev/null 2>&1 \
    || dnf install -y -q "$url" || return 1
  # EL8/EL9 ship a postgresql module that hides the PGDG packages.
  dnf -qy module disable postgresql >/dev/null 2>&1 || true
}

install_postgres_rhel() {
  local m
  m="$(rpm_pgdg_major || true)"
  if [ -z "$m" ] && ! rpm -q postgresql-server >/dev/null 2>&1; then
    log "Installing PostgreSQL ${PG_WANTED_MAJOR} from the official PGDG repository"
    if add_pgdg_rpm_repo && dnf_install "postgresql${PG_WANTED_MAJOR}-server" "postgresql${PG_WANTED_MAJOR}-contrib"; then
      m="$PG_WANTED_MAJOR"
    else
      warn "PGDG repository unavailable; falling back to the distribution PostgreSQL (no TimescaleDB)."
      warn "(On an outdated point release, 'dnf upgrade' and re-running install.sh usually fixes this.)"
      # A half-working PGDG repo would break every following dnf call: remove it again.
      dnf remove -y -q pgdg-redhat-repo pgdg-fedora-repo >/dev/null 2>&1 || true
      dnf -qy module reset postgresql >/dev/null 2>&1 || true
      dnf -qy module enable "postgresql:${PG_WANTED_MAJOR}" >/dev/null 2>&1 || true
      dnf_install postgresql-server postgresql-contrib
    fi
  fi

  if [ -n "$m" ]; then
    PG_FLAVOUR=pgdg-rpm
    PG_MAJOR="$m"
    PG_BIN="/usr/pgsql-${m}/bin"
    PG_UNIT="postgresql-${m}.service"
    if [ ! -f "/var/lib/pgsql/${m}/data/PG_VERSION" ]; then
      log "Initialising the PostgreSQL ${m} data directory"
      "/usr/pgsql-${m}/bin/postgresql-${m}-setup" initdb
    fi
  else
    PG_FLAVOUR=distro-rpm
    PG_BIN=/usr/bin
    PG_UNIT=postgresql.service
    PG_MAJOR="$("${PG_BIN}/postgres" --version | sed -n 's/.* \([0-9][0-9]*\)\..*/\1/p')"
    if [ -z "$PG_MAJOR" ] || [ "$PG_MAJOR" -lt "$PG_MIN_MAJOR" ]; then
      die "PostgreSQL ${PG_MAJOR:-?} is too old (need >= ${PG_MIN_MAJOR})."
    fi
    if [ ! -f /var/lib/pgsql/data/PG_VERSION ]; then
      log "Initialising the PostgreSQL data directory"
      postgresql-setup --initdb
    fi
  fi
  systemctl enable "$PG_UNIT" >/dev/null 2>&1
  systemctl start "$PG_UNIT"
}

log "Installing PostgreSQL (>= ${PG_MIN_MAJOR})"
if [ "$FAMILY" = debian ]; then install_postgres_debian; else install_postgres_rhel; fi
pg_wait_ready || die "PostgreSQL did not become ready (unit ${PG_UNIT})."
ok "PostgreSQL ${PG_MAJOR} running (${PG_UNIT})"

# ============================================================================= TimescaleDB
# Every step is allowed to fail: the app then runs in plain-PostgreSQL fallback mode.
TIMESCALE=0

install_timescale_debian() {
  # Timescale publishes per distribution (debian|ubuntu) and codename.
  local dist=debian
  case " ${OS_ID} ${OS_LIKE} " in *" ubuntu "*) dist=ubuntu ;; esac
  [ -n "$OS_CODENAME" ] || return 1
  install -d -m 0755 /etc/apt/keyrings || return 1
  curl -fsSL https://packagecloud.io/timescale/timescaledb/gpgkey \
    | gpg --dearmor --yes -o /etc/apt/keyrings/timescaledb.gpg || return 1
  echo "deb [signed-by=/etc/apt/keyrings/timescaledb.gpg] https://packagecloud.io/timescale/timescaledb/${dist}/ ${OS_CODENAME} main" \
    >/etc/apt/sources.list.d/timescaledb.list || return 1
  APT_UPDATED=0
  if ! apt_install "timescaledb-2-postgresql-${PG_MAJOR}"; then
    # Do not leave a broken repository behind (it would break later apt runs).
    rm -f /etc/apt/sources.list.d/timescaledb.list
    APT_UPDATED=0
    return 1
  fi
}

install_timescale_rhel() {
  case "$PG_FLAVOUR" in
    pgdg-rpm)
      [ "$OS_ID" != fedora ] || return 1
      cat >/etc/yum.repos.d/timescale_timescaledb.repo <<EOF || return 1
[timescale_timescaledb]
name=timescale_timescaledb
baseurl=https://packagecloud.io/timescale/timescaledb/el/${EL_MAJOR}/\$basearch
repo_gpgcheck=1
gpgcheck=0
enabled=1
gpgkey=https://packagecloud.io/timescale/timescaledb/gpgkey
sslverify=1
sslcacert=/etc/pki/tls/certs/ca-bundle.crt
metadata_expire=300
EOF
      if ! dnf_install "timescaledb-2-postgresql-${PG_MAJOR}"; then
        rm -f /etc/yum.repos.d/timescale_timescaledb.repo
        return 1
      fi
      ;;
    distro-rpm)
      # Fedora packages TimescaleDB (Apache-licensed edition) for its own PostgreSQL.
      [ "$OS_ID" = fedora ] || return 1
      dnf_install timescaledb || return 1
      ;;
    *) return 1 ;;
  esac
}

timescale_available() {
  [ "$(psql_super -Atc "SELECT count(*) FROM pg_available_extensions WHERE name = 'timescaledb'" 2>/dev/null)" = 1 ]
}

restart_postgres() {
  systemctl restart "$PG_UNIT" && pg_wait_ready
}

enable_timescale_preload() {
  # Adds timescaledb to shared_preload_libraries (keeping existing entries) and restarts
  # PostgreSQL. If the server does not come back, the change is reverted.
  local current new datadir
  current="$(psql_super -Atc 'SHOW shared_preload_libraries')" || return 1
  case ",${current// /}," in *,timescaledb,*) return 0 ;; esac
  new="${current:+${current}, }timescaledb"
  datadir="$(psql_super -Atc 'SHOW data_directory')" || return 1
  log "Adding timescaledb to shared_preload_libraries and restarting PostgreSQL"
  psql_super -c "ALTER SYSTEM SET shared_preload_libraries = '${new}'" || return 1
  if ! restart_postgres; then
    warn "PostgreSQL failed to start with TimescaleDB preloaded; reverting."
    sed -i '/^shared_preload_libraries/d' "${datadir}/postgresql.auto.conf"
    restart_postgres || die "PostgreSQL does not start any more; see: journalctl -u ${PG_UNIT}"
    return 1
  fi
}

if [ "$OPT_TIMESCALE" -eq 1 ]; then
  log "Installing TimescaleDB (optional)"
  if timescale_available; then
    ok "TimescaleDB packages already installed"
  elif [ "$FAMILY" = debian ]; then
    install_timescale_debian || warn "TimescaleDB could not be installed; continuing with plain PostgreSQL."
  else
    install_timescale_rhel || warn "TimescaleDB could not be installed; continuing with plain PostgreSQL."
  fi
  if timescale_available && enable_timescale_preload; then
    TIMESCALE=1
    ok "TimescaleDB enabled"
  else
    warn "TimescaleDB is not available: NetworkMonitor will run in plain PostgreSQL mode."
  fi
else
  log "Skipping TimescaleDB (--no-timescale)"
fi

# ============================================================================= Python
PYTHON=""

python_ok() {
  "$1" - <<'EOF' >/dev/null 2>&1
import sys, venv, ensurepip  # noqa: F401  (venv + ensurepip are needed for `-m venv`)
sys.exit(0 if sys.version_info >= (3, 12) else 1)
EOF
}

find_python() {
  local c
  # The standalone build from an earlier run (install_python_uv) is reused on upgrades.
  for c in python3.13 python3.12 python3 /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
    "${INSTALL_DIR}"/python/cpython-3.12-linux-*/bin/python3.12; do
    if have "$c" && python_ok "$(command -v "$c")"; then
      PYTHON="$(command -v "$c")"
      return 0
    fi
  done
  return 1
}

install_python_uv() {
  # Last resort for distributions without Python 3.12 packages (e.g. Debian 12,
  # Ubuntu 22.04, EL8 without AppStream): a standalone CPython managed by uv,
  # installed under /opt/netmon (nothing outside it is touched).
  log "Installing a standalone Python 3.12 into ${INSTALL_DIR}/python (via uv)"
  install -d -m 0755 "${INSTALL_DIR}/uv" "${INSTALL_DIR}/python" || return 1
  curl -fsSL https://astral.sh/uv/install.sh \
    | env UV_INSTALL_DIR="${INSTALL_DIR}/uv" UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null || return 1
  UV_PYTHON_INSTALL_DIR="${INSTALL_DIR}/python" "${INSTALL_DIR}/uv/uv" python install 3.12 >/dev/null || return 1
  PYTHON="$(UV_PYTHON_INSTALL_DIR="${INSTALL_DIR}/python" "${INSTALL_DIR}/uv/uv" python find --system 3.12 2>/dev/null \
    || UV_PYTHON_INSTALL_DIR="${INSTALL_DIR}/python" "${INSTALL_DIR}/uv/uv" python find 3.12)" || return 1
  python_ok "$PYTHON"
}

log "Installing Python (>= 3.12)"
if ! find_python; then
  if [ "$FAMILY" = debian ]; then
    apt_install python3 python3-venv python3-pip || true
    # Some releases split venv per minor version (python3.12-venv).
    for v in 3.13 3.12; do
      if have "python${v}"; then apt_install "python${v}-venv" >/dev/null 2>&1 || true; fi
    done
  else
    dnf_install python3.12 python3.12-pip >/dev/null 2>&1 || dnf_install python3 python3-pip || true
  fi
  find_python || install_python_uv || die "Could not provide Python >= 3.12."
fi
ok "Using $("$PYTHON" -V) at ${PYTHON}"

# ============================================================================= Kerberos libs
# [3] Kerberos SSO (feature `sso_kerberos`) uses python-gssapi over MIT krb5. PyPI has no
# Linux wheels for gssapi, so pip compiles it: runtime library + krb5 headers, a C compiler
# and the Python headers. Best effort: without them the app is installed without the extra.
KRB_BUILD_OK=0
if [ "$OPT_KERBEROS" -eq 1 ]; then
  log "Installing the MIT Kerberos libraries (Kerberos SSO)"
  PY_MM="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  py_headers() {
    "$PYTHON" -c 'import os, sys, sysconfig; sys.exit(0 if os.path.isfile(os.path.join(sysconfig.get_paths()["include"], "Python.h")) else 1)' 2>/dev/null
  }
  if [ "$FAMILY" = debian ]; then
    apt_install libgssapi-krb5-2 >/dev/null 2>&1 || warn "could not install libgssapi-krb5-2"
    apt_install libkrb5-dev gcc libc6-dev >/dev/null 2>&1 || true
    py_headers || apt_install "python${PY_MM}-dev" >/dev/null 2>&1 || apt_install python3-dev >/dev/null 2>&1 || true
  else
    dnf_install krb5-libs >/dev/null 2>&1 || warn "could not install krb5-libs"
    dnf_install krb5-devel gcc >/dev/null 2>&1 || true
    py_headers || dnf_install "python${PY_MM}-devel" >/dev/null 2>&1 || dnf_install python3-devel >/dev/null 2>&1 || true
  fi
  if have krb5-config && have gcc && py_headers; then
    KRB_BUILD_OK=1
    ok "krb5 headers, compiler and Python headers present"
  else
    warn "Cannot build python-gssapi (missing krb5-config / gcc / Python.h): Kerberos SSO will be unavailable."
  fi
fi

# ============================================================================= user & dirs
log "Creating the system user and directories"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  NOLOGIN="$(command -v nologin || echo /usr/sbin/nologin)"
  useradd --system --user-group --home-dir "$DATA_DIR" --no-create-home --shell "$NOLOGIN" \
    --comment "NetworkMonitor" "$APP_USER"
fi
install -d -m 0755 -o root -g root "$INSTALL_DIR"
install -d -m 0750 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR"
install -d -m 0750 -o "$APP_USER" -g "$APP_USER" "${DATA_DIR}/logs"

# ============================================================================= app package
# Resolve what to install: a wheel, or a source tree (backend/ + frontend/).
PKG=""          # path given to pip
SPA_DIR=""      # built SPA to place next to the installed package (source installs)
SRC_ROOT=""

if [ -n "$OPT_TARBALL" ]; then
  log "Unpacking ${OPT_TARBALL}"
  TARBALL_FILE="$OPT_TARBALL"
  case "$OPT_TARBALL" in
    http://* | https://*)
      TARBALL_FILE="${WORK}/release.tar.gz"
      curl -fL --retry 3 -o "$TARBALL_FILE" "$OPT_TARBALL"
      ;;
  esac
  [ -f "$TARBALL_FILE" ] || die "Tarball not found: ${TARBALL_FILE}"
  mkdir -p "${WORK}/tarball"
  tar -xzf "$TARBALL_FILE" -C "${WORK}/tarball"
  OPT_WHEEL="$(find "${WORK}/tarball" -maxdepth 3 -name 'netmon-*.whl' | sort | tail -n1)"
  if [ -z "$OPT_WHEEL" ]; then
    pp="$(find "${WORK}/tarball" -maxdepth 3 -path '*/backend/pyproject.toml' | head -n1)"
    [ -n "$pp" ] || die "The tarball contains neither a netmon-*.whl nor backend/pyproject.toml."
    SRC_ROOT="$(dirname "$(dirname "$pp")")"
  fi
elif [ -z "$OPT_WHEEL" ]; then
  if [ -n "$OPT_SOURCE" ]; then
    SRC_ROOT="$(cd "$OPT_SOURCE" && pwd)"
  else
    REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
    # Release tarball layout: <root>/netmon-*.whl + <root>/deploy/linux/install.sh
    OPT_WHEEL="$(find "$REPO_ROOT" -maxdepth 1 -name 'netmon-*.whl' | sort | tail -n1)"
    [ -n "$OPT_WHEEL" ] || SRC_ROOT="$REPO_ROOT"
  fi
fi

if [ -n "$OPT_WHEEL" ]; then
  [ -f "$OPT_WHEEL" ] || die "Wheel not found: ${OPT_WHEEL}"
  PKG="$(cd "$(dirname "$OPT_WHEEL")" && pwd)/$(basename "$OPT_WHEEL")"
  log "Installing from wheel ${PKG}"
else
  [ -f "${SRC_ROOT}/backend/pyproject.toml" ] || die "No backend/pyproject.toml under ${SRC_ROOT}; use --tarball or --wheel."
  log "Installing from source tree ${SRC_ROOT}"
  STAGE="${WORK}/stage"
  mkdir -p "$STAGE"
  # Copy the backend without local artefacts. README.md is the package readme (../README.md).
  COPY_ITEMS=(backend)
  [ -f "${SRC_ROOT}/README.md" ] && COPY_ITEMS+=(README.md)
  tar -C "$SRC_ROOT" --exclude='.venv' --exclude='__pycache__' --exclude='*.egg-info' \
    --exclude='backend/build' --exclude='backend/dist' --exclude='backend/netmon/static' \
    -cf - "${COPY_ITEMS[@]}" | tar -C "$STAGE" -xf -

  # --- web UI: build it when Node.js >= 18 is available, else use a prebuilt copy.
  node_ok() { have node && have npm && [ "$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)" -ge 18 ]; }
  prebuilt_spa() { [ -f "${SRC_ROOT}/frontend/dist/index.html" ] || [ -f "${SRC_ROOT}/backend/netmon/static/index.html" ]; }
  if [ -f "${SRC_ROOT}/frontend/package.json" ] && ! node_ok && ! prebuilt_spa; then
    # A fresh git clone has no built UI (frontend/dist is not committed): try the
    # distribution's Node.js (>= 18 on Debian 12, Ubuntu 22.04+, EL8/9 AppStream).
    log "Installing Node.js from the distribution to build the web UI"
    if [ "$FAMILY" = debian ]; then
      apt_install nodejs npm >/dev/null 2>&1 || true
    else
      dnf -qy module enable nodejs:20 >/dev/null 2>&1 || true
      dnf_install nodejs npm >/dev/null 2>&1 || true
    fi
    node_ok || warn "No Node.js >= 18 available from the distribution."
  fi
  if [ -f "${SRC_ROOT}/frontend/package.json" ] && node_ok; then
    log "Building the web UI with Node.js $(node -v)"
    mkdir -p "${STAGE}/frontend"
    tar -C "${SRC_ROOT}/frontend" --exclude='node_modules' --exclude='dist' -cf - . | tar -C "${STAGE}/frontend" -xf -
    build_spa() {
      cd "${STAGE}/frontend" || return 1
      if [ -f package-lock.json ]; then
        npm ci --no-audit --no-fund || return 1
      else
        npm install --no-audit --no-fund || return 1
      fi
      npm run build || return 1
    }
    if (build_spa) && [ -f "${STAGE}/frontend/dist/index.html" ]; then
      SPA_DIR="${STAGE}/frontend/dist"
    else
      warn "Building the web UI failed; looking for a prebuilt copy."
    fi
  fi
  if [ -z "$SPA_DIR" ]; then
    for d in "${SRC_ROOT}/frontend/dist" "${SRC_ROOT}/backend/netmon/static"; do
      if [ -f "${d}/index.html" ]; then SPA_DIR="$d"; break; fi
    done
  fi
  if [ -n "$SPA_DIR" ]; then
    ok "Web UI: ${SPA_DIR}"
    rm -rf "${STAGE}/backend/netmon/static"
    cp -a "$SPA_DIR" "${STAGE}/backend/netmon/static"
    SPA_DIR="${STAGE}/backend/netmon/static"
  else
    warn "No web UI found (install Node.js >= 18 or use a release tarball). The API works, the UI will not."
  fi
  PKG="${STAGE}/backend"
fi

log "Creating/updating the virtual environment ${VENV}"
if [ -x "${VENV}/bin/python" ] && ! python_ok "${VENV}/bin/python"; then
  warn "Existing venv uses an unsupported Python; recreating it."
  rm -rf "$VENV"
fi
[ -x "${VENV}/bin/python" ] || "$PYTHON" -m venv "$VENV"
export PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
"${VENV}/bin/python" -m pip install -q --upgrade pip setuptools wheel
KERBEROS=0
if [ "$KRB_BUILD_OK" -eq 1 ]; then
  # "<wheel or dir>[kerberos]": the app plus python-gssapi (compiled here).
  if "${VENV}/bin/python" -m pip install -q --upgrade "${PKG}[kerberos]" \
    && "${VENV}/bin/python" -c 'import gssapi.raw' >/dev/null 2>&1; then
    KERBEROS=1
  else
    warn "python-gssapi could not be installed; continuing without Kerberos SSO."
  fi
fi
"${VENV}/bin/python" -m pip install -q --upgrade "$PKG"
# Same version on re-run: force the app itself to be replaced (dependencies stay).
"${VENV}/bin/python" -m pip install -q --no-deps --force-reinstall "$PKG"
[ -x "${VENV}/bin/netmon" ] || die "The netmon console script was not installed."

# Make sure the SPA sits next to the installed package (NETMON_FRONTEND_DIST default),
# even if the package data configuration did not pick it up.
PKG_DIR="$("${VENV}/bin/python" -c 'import netmon, pathlib; print(pathlib.Path(netmon.__file__).resolve().parent)')"
if [ -n "$SPA_DIR" ] && [ ! -f "${PKG_DIR}/static/index.html" ]; then
  rm -rf "${PKG_DIR}/static"
  cp -a "$SPA_DIR" "${PKG_DIR}/static"
fi
chown -R root:root "$INSTALL_DIR"
chmod -R u+rwX,go+rX,go-w "$INSTALL_DIR"
APP_VERSION="$("${VENV}/bin/python" -c 'import netmon; print(netmon.__version__)' 2>/dev/null || echo '?')"
ok "netmon ${APP_VERSION} installed in ${VENV}"

# ============================================================================= database
log "Configuring the database"

# Keep the existing password on re-runs, so netmon.env and the DB role stay in sync.
DB_PASSWORD=""
if [ -f "$ENV_FILE" ]; then
  DB_PASSWORD="$(sed -n 's#^DATABASE_URL=postgres[^:]*://[^:/@]*:\([^@]*\)@.*#\1#p' "$ENV_FILE" | head -n1)"
fi
if [ -z "$DB_PASSWORD" ]; then
  # Hex only: safe inside a URL and inside an SQL literal without escaping.
  DB_PASSWORD="$("$PYTHON" -c 'import secrets; print(secrets.token_hex(24))')"
fi
case "$DB_PASSWORD" in *[!0-9A-Za-z_-]*) DB_PASSWORD="$("$PYTHON" -c 'import secrets; print(secrets.token_hex(24))')" ;; esac

PG_PORT="$(psql_super -Atc 'SHOW port')"
ROLE_EXISTS="$(psql_super -Atc "SELECT count(*) FROM pg_roles WHERE rolname = '${DB_USER}'")"
DB_EXISTS="$(psql_super -Atc "SELECT count(*) FROM pg_database WHERE datname = '${DB_NAME}'")"

# The SQL goes through stdin, so the password never appears in the process list.
{
  echo "SET password_encryption = 'scram-sha-256';"
  if [ "$ROLE_EXISTS" = 1 ]; then
    echo "ALTER ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASSWORD}';"
  else
    echo "CREATE ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASSWORD}';"
  fi
} | psql_super -d postgres
if [ "$DB_EXISTS" != 1 ]; then
  psql_super -d postgres -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER} ENCODING 'UTF8' TEMPLATE template0"
fi
psql_super -d "$DB_NAME" -c "GRANT ALL ON SCHEMA public TO ${DB_USER}" >/dev/null
if [ "$TIMESCALE" -eq 1 ]; then
  # Created by the superuser here, so the app does not need superuser rights for it.
  psql_super -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS timescaledb" >/dev/null 2>&1 \
    || warn "CREATE EXTENSION timescaledb failed; the app will use plain PostgreSQL mode."
fi

# [2b-3] pg_trgm (contrib) indexes the syslog/trap text search. It is a trusted extension,
# but creating it here as the superuser also covers servers where the app role may not.
pg_trgm_available() {
  [ "$(psql_super -Atc "SELECT count(*) FROM pg_available_extensions WHERE name = 'pg_trgm'" 2>/dev/null)" = 1 ]
}
if ! pg_trgm_available; then
  log "Installing the PostgreSQL contrib package (pg_trgm)"
  case "$PG_FLAVOUR" in
    debian) apt_install "postgresql-contrib-${PG_MAJOR}" >/dev/null 2>&1 || apt_install postgresql-contrib >/dev/null 2>&1 || true ;;
    pgdg-rpm) dnf_install "postgresql${PG_MAJOR}-contrib" >/dev/null 2>&1 || true ;;
    *) dnf_install postgresql-contrib >/dev/null 2>&1 || true ;;
  esac
fi
if psql_super -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS pg_trgm" >/dev/null 2>&1; then
  ok "pg_trgm extension ready"
else
  warn "pg_trgm is not available (PostgreSQL contrib package missing): log search will be slower."
fi

# Password logins over loopback for the netmon role (RHEL's initdb defaults to `ident`).
HBA_FILE="$(psql_super -Atc 'SHOW hba_file')"
if ! grep -qF "$HBA_MARKER" "$HBA_FILE"; then
  log "Allowing password logins for ${DB_USER} on 127.0.0.1/::1 in ${HBA_FILE}"
  cp -p "$HBA_FILE" "${HBA_FILE}.netmon.bak"
  {
    echo "$HBA_MARKER"
    echo "host    ${DB_NAME}    ${DB_USER}    127.0.0.1/32    scram-sha-256"
    echo "host    ${DB_NAME}    ${DB_USER}    ::1/128         scram-sha-256"
    echo "# netmon: end"
    cat "${HBA_FILE}.netmon.bak"
  } >"${WORK}/pg_hba.conf"
  cat "${WORK}/pg_hba.conf" >"$HBA_FILE"   # keeps owner, mode and SELinux label
  psql_super -c 'SELECT pg_reload_conf()' >/dev/null
fi

# Verify that the app's credentials work before going further.
if ! PGPASSWORD="$DB_PASSWORD" "${PG_BIN}/psql" -X -h 127.0.0.1 -p "$PG_PORT" -U "$DB_USER" -d "$DB_NAME" -Atc 'SELECT 1' >/dev/null 2>&1; then
  die "Cannot log in to PostgreSQL as ${DB_USER} over 127.0.0.1:${PG_PORT}. Check ${HBA_FILE}."
fi
ok "Database ${DB_NAME} ready on 127.0.0.1:${PG_PORT} (timescale: $([ "$TIMESCALE" -eq 1 ] && echo yes || echo no))"

# ============================================================================= netmon.env
PORT="$OPT_PORT"
if [ -z "$PORT" ] && [ -f "$ENV_FILE" ]; then
  PORT="$(sed -n 's/^NETMON_PORT=\([0-9][0-9]*\)$/\1/p' "$ENV_FILE" | head -n1)"
fi
PORT="${PORT:-8080}"
case "$PORT" in '' | *[!0-9]*) die "Invalid port: ${PORT}" ;; esac

log "Writing ${ENV_FILE}"
umask 077
cat >"${WORK}/netmon.env" <<EOF
# Generated by the NetworkMonitor installer on $(date -u +%Y-%m-%dT%H:%M:%SZ). Do not edit:
# re-run install.sh to change it.
DATABASE_URL=postgresql://${DB_USER}:${DB_PASSWORD}@127.0.0.1:${PG_PORT}/${DB_NAME}
NETMON_PORT=${PORT}
EOF
install -m 0600 -o "$APP_USER" -g "$APP_USER" "${WORK}/netmon.env" "$ENV_FILE"
umask 022

# ============================================================================= systemd
log "Installing the systemd unit"
UNIT_SRC="${SCRIPT_DIR}/netmon.service"
[ -f "$UNIT_SRC" ] || die "Missing ${UNIT_SRC} (it ships next to install.sh)."
install -m 0644 "$UNIT_SRC" "$UNIT_PATH"
install -d -m 0755 "$DROPIN_DIR"
cat >"${DROPIN_DIR}/10-postgresql.conf" <<EOF
# Generated by install.sh: start after the local PostgreSQL server.
[Unit]
Wants=${PG_UNIT}
After=${PG_UNIT}
EOF
# [3] Outbound proxy for the app's HTTPS clients (httpx honours HTTPS_PROXY / NO_PROXY).
# The proxy must allow long requests (>= 70 s) to api.telegram.org (Telegram long polling).
if [ -n "$OPT_HTTPS_PROXY" ]; then
  case "$OPT_HTTPS_PROXY" in
    *[[:space:]\"]*) die "--https-proxy must not contain spaces or quotes" ;;
  esac
  no_proxy_list="localhost,127.0.0.1,::1${OPT_NO_PROXY:+,${OPT_NO_PROXY}}"
  case "$no_proxy_list" in
    *[[:space:]\"]*) die "--no-proxy must not contain spaces or quotes" ;;
  esac
  log "Service outbound proxy: ${OPT_HTTPS_PROXY%%://*}://... (NO_PROXY=${no_proxy_list})"
  # '%' is a systemd specifier (URL-encoded proxy passwords): escape it. Root-only (0600):
  # the URL may carry proxy credentials.
  px="${OPT_HTTPS_PROXY//%/%%}"
  install -m 0600 /dev/null "${DROPIN_DIR}/20-proxy.conf"
  cat >"${DROPIN_DIR}/20-proxy.conf" <<EOF
# Generated by install.sh (--https-proxy / HTTPS_PROXY): outbound HTTPS of the app.
[Service]
Environment="HTTPS_PROXY=${px}" "HTTP_PROXY=${px}" "NO_PROXY=${no_proxy_list//%/%%}"
EOF
elif [ -f "${DROPIN_DIR}/20-proxy.conf" ]; then
  ok "Keeping the service proxy from ${DROPIN_DIR}/20-proxy.conf"
fi
systemctl daemon-reload
systemctl enable netmon.service >/dev/null 2>&1
systemctl restart netmon.service

# ============================================================================= firewall
if [ "$OPT_FIREWALL" -eq 1 ]; then
  if have firewall-cmd && firewall-cmd --state >/dev/null 2>&1; then
    log "Opening TCP ${PORT} in firewalld"
    if ! { firewall-cmd -q --permanent --add-port="${PORT}/tcp" && firewall-cmd -q --reload; }; then
      warn "firewalld update failed"
    fi
  elif have ufw && ufw status 2>/dev/null | grep -q '^Status: active'; then
    log "Opening TCP ${PORT} in ufw"
    ufw allow "${PORT}/tcp" >/dev/null || warn "ufw update failed"
  fi
fi

# ============================================================================= health
log "Waiting for NetworkMonitor to answer on port ${PORT}"
HEALTHY=0
for _ in $(seq 1 90); do
  if curl -fsS -m 3 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then HEALTHY=1; break; fi
  if ! systemctl is-active --quiet netmon.service && [ "$(systemctl show -p NRestarts --value netmon.service 2>/dev/null || echo 0)" -gt 3 ]; then
    break
  fi
  sleep 1
done
if [ "$HEALTHY" -ne 1 ]; then
  journalctl -u netmon.service -n 50 --no-pager >&2 || true
  die "NetworkMonitor did not become healthy. See: journalctl -u netmon"
fi

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOST_IP="${HOST_IP:-$(hostname -f 2>/dev/null || hostname)}"
echo
printf '%sNetworkMonitor %s is running.%s\n' "$C_GREEN" "$APP_VERSION" "$C_OFF"
echo "  Open:     http://${HOST_IP}:${PORT}  (complete the setup wizard on first visit)"
echo "  Health:   http://${HOST_IP}:${PORT}/api/health"
echo "  Service:  systemctl status netmon   |   logs: journalctl -u netmon -f"
echo "  Data:     ${DATA_DIR}   (config: ${ENV_FILE})"
[ "$TIMESCALE" -eq 1 ] || echo "  Storage:  plain PostgreSQL mode (TimescaleDB not installed)"
if [ "$KERBEROS" -eq 1 ]; then
  echo "  Kerberos: SSO support installed (Settings -> Single sign-on; AD steps: deploy/README.md)"
else
  echo "  Kerberos: SSO support NOT installed (re-run without --no-kerberos once gcc/krb5 headers are available)"
fi

# ============================================================================= traps / syslog
# [2b-3] The receivers listen on UDP 162 (traps) and UDP/TCP 514 (syslog) only while the
# Traps / Syslog features are ON (Settings -> Features). Nothing is opened in the firewall
# automatically: restrict senders to the device networks with the lines printed here.
lan_cidr() {
  # The connected IPv4 network of the interface that holds the default route.
  local dev
  dev="$(ip -o -4 route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}')"
  [ -n "$dev" ] || return 0
  ip -o -4 route show dev "$dev" scope link proto kernel 2>/dev/null | awk '{print $1; exit}'
}
LAN="$(lan_cidr || true)"
LAN="${LAN:-<device-network/prefix>}"
echo
echo "  SNMP traps (UDP 162) and syslog (UDP+TCP 514): turn them on in Settings -> Features,"
echo "  then point the devices at ${HOST_IP} (examples: deploy/README.md, 'Traps and syslog')."
echo "  Flows (UDP 2055 NetFlow/IPFIX, 4739 IPFIX, 6343 sFlow): turn on 'Flows' in Settings -> Features,"
echo "  then configure flow export on the devices (examples: deploy/README.md, 'Flows')."
if have ufw && ufw status 2>/dev/null | grep -q '^Status: active'; then
  echo "  ufw is active; allow the device networks (repeat per network):"
  echo "      ufw allow from ${LAN} to any port 162 proto udp"
  echo "      ufw allow from ${LAN} to any port 514 proto udp"
  echo "      ufw allow from ${LAN} to any port 514 proto tcp"
  echo "      ufw allow from ${LAN} to any port 2055 proto udp"
  echo "      ufw allow from ${LAN} to any port 4739 proto udp"
  echo "      ufw allow from ${LAN} to any port 6343 proto udp"
elif have firewall-cmd && firewall-cmd --state >/dev/null 2>&1; then
  echo "  firewalld is active; allow the device networks (repeat per network):"
  for rule in "port port=162 protocol=udp" "port port=514 protocol=udp" "port port=514 protocol=tcp" \
    "port port=2055 protocol=udp" "port port=4739 protocol=udp" "port port=6343 protocol=udp"; do
    echo "      firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source address=${LAN} ${rule} accept'"
  done
  echo "      firewall-cmd --reload"
fi

# Another program holding the ports makes the receiver report "Address already in use".
if have ss; then
  CONFLICTS="$(ss -H -lunpt '( sport = :162 or sport = :514 or sport = :2055 or sport = :4739 or sport = :6343 )' 2>/dev/null | grep -v '"netmon"' | grep -v 'pid='"$(systemctl show -p MainPID --value netmon.service 2>/dev/null || echo 0)"',' || true)"
  if [ -n "$CONFLICTS" ]; then
    warn "Another program already listens on UDP 162 / UDP-TCP 514 / UDP 2055-4739-6343; NetworkMonitor cannot receive traps/syslog/flows there:"
    printf '%s\n' "$CONFLICTS" | sed 's/^/      /' >&2
    case "$CONFLICTS" in *snmptrapd*) warn "  snmptrapd: sudo systemctl disable --now snmptrapd" ;; esac
    case "$CONFLICTS" in *rsyslogd*) warn "  rsyslog: remove the imudp/imtcp port 514 input (/etc/rsyslog.conf, /etc/rsyslog.d/) and: sudo systemctl restart rsyslog" ;; esac
    case "$CONFLICTS" in *syslog-ng*) warn "  syslog-ng: remove the network() / udp() / tcp() source on port 514 and restart syslog-ng" ;; esac
    case "$CONFLICTS" in *nfcapd* | *sfcapd* | *pmacct* | *nfacctd* | *sfacctd* | *goflow*) warn "  another flow collector (nfdump / pmacct / goflow): stop it or move it to other ports" ;; esac
  fi
fi
