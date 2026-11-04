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
#   4. Installs the app (with the web UI bundled) into the venv.
#   5. Installs netmon.service (CAP_NET_RAW ambient capability), enables and starts it.
#   6. Prints the URL.
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
  --no-firewall         Do not open the port in firewalld / ufw
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
