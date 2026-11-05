# NetworkMonitor container image (multi-stage).
#
#   docker build -t networkmonitor .
#
# Base images are build arguments, so a registry mirror can be used when Docker Hub
# rate-limits pulls, e.g.:
#   docker build --build-arg NODE_IMAGE=mirror.gcr.io/library/node:22-slim \
#                --build-arg PYTHON_IMAGE=mirror.gcr.io/library/python:3.12-slim .
#
# Runtime contract (docs/architecture.md §8):
#   - entrypoint `netmon serve` (migrations run automatically at startup)
#   - DATABASE_URL=postgresql://netmon@db:5432/netmon, NETMON_DATA_DIR=/data
#   - the DB password comes from NETMON_DB_PASSWORD_FILE (set by docker-compose)
#   - the SPA is served from <site-packages>/netmon/static
#
# Corporate TLS inspection: drop the organisation's root CA (*.crt / *.pem / *.cer, PEM
# or DER) into deploy/certs/ and rebuild. Every stage that reaches the network (npm, pip,
# apt) and the running app then trust it. With only deploy/certs/README.md the build is
# unchanged. See deploy/certs/README.md.
#
# ICMP: the process runs as an unprivileged user and uses unprivileged ICMP sockets
# (icmplib privileged=False). That needs net.ipv4.ping_group_range to cover the user's
# GID. Docker >= 20.10 sets "0 2147483647" by default and deploy/docker-compose.yml sets
# it explicitly. As a last resort the app falls back to the `ping` binary (iputils-ping,
# which carries cap_net_raw as a file capability).

ARG NODE_IMAGE=node:22-slim
ARG PYTHON_IMAGE=python:3.12-slim

# ---------------------------------------------------------------------------
# Stage 0: Python base with the corporate CAs (deploy/certs) in the OS trust store.
# Base of the backend and runtime stages; its bundle is also handed to npm.
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS pybase
COPY deploy/import-ca-certs.sh /usr/local/bin/netmon-import-ca-certs
# The whole folder is copied (it always holds README.md), so this cannot fail when no
# certificate was added; the script ignores everything but *.crt / *.pem / *.cer.
COPY deploy/certs/ /tmp/netmon-certs/
# (sed: a Windows checkout with core.autocrlf may have given the script CRLF endings.)
RUN sed -i 's/\r$//' /usr/local/bin/netmon-import-ca-certs \
 && chmod 0755 /usr/local/bin/netmon-import-ca-certs \
 && netmon-import-ca-certs /tmp/netmon-certs /usr/local/share/ca-certificates/netmon \
 && update-ca-certificates --fresh \
 && rm -rf /tmp/netmon-certs \
 && test -s /etc/ssl/certs/ca-certificates.crt
# pip/requests/httpx/OpenSSL read these; the bundle is Mozilla's roots + deploy/certs.
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt

# ---------------------------------------------------------------------------
# Stage 1: build the React SPA -> /src/frontend/dist
# ---------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS frontend
WORKDIR /src/frontend
# Node keeps its own CA list; NODE_EXTRA_CA_CERTS adds the system bundle (with the
# corporate CAs from deploy/certs) so npm works behind a TLS-inspecting firewall.
COPY --from=pybase /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/netmon-ca-bundle.crt
ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/netmon-ca-bundle.crt
# Optional npm registry mirror (e.g. a corporate Nexus/Artifactory proxy):
#   docker compose build --build-arg NPM_REGISTRY=https://nexus.example.com/repository/npm/
ARG NPM_REGISTRY=
# Slow, filtered or proxied networks (corporate firewalls, TLS inspection) can make npm
# drop connections and die with "Exit handler never called". Fewer parallel sockets,
# longer timeouts, more retries, and a few whole-command retries make the install robust.
ENV CI=1 \
    npm_config_fund=false \
    npm_config_audit=false \
    npm_config_update_notifier=false \
    npm_config_maxsockets=4 \
    npm_config_fetch_retries=3 \
    npm_config_fetch_retry_mintimeout=10000 \
    npm_config_fetch_retry_maxtimeout=60000 \
    npm_config_fetch_timeout=120000
# Install dependencies first so this layer is cached while only sources change.
COPY frontend/package.json frontend/package-lock.json ./
RUN if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi; \
    for attempt in 1 2 3; do \
      npm ci --no-audit --no-fund && exit 0; \
      echo "npm ci failed (attempt $attempt/3), retrying in 15s..." >&2; \
      rm -rf node_modules; sleep 15; \
    done; \
    echo "npm ci failed 3 times. On a filtered or proxied network see deploy/README.md (Troubleshooting)." >&2; \
    exit 1
COPY frontend/ ./
RUN npm run build && test -f dist/index.html

# ---------------------------------------------------------------------------
# Stage 2: install the backend package (with the SPA bundled) into a venv
# ---------------------------------------------------------------------------
FROM pybase AS backend
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN python -m venv /opt/netmon/venv
ENV PATH=/opt/netmon/venv/bin:$PATH
WORKDIR /src
COPY backend/ /src/backend/
# backend/pyproject.toml references ../README.md as the package readme.
COPY README.md /src/README.md
# Packaging contract: the built SPA goes to backend/netmon/static before the install.
RUN rm -rf /src/backend/netmon/static
COPY --from=frontend /src/frontend/dist/ /src/backend/netmon/static/
RUN pip install --upgrade pip \
 && pip install /src/backend \
 # Make sure the SPA ends up next to the installed package even if the package data
 # configuration does not include static/ (NETMON_FRONTEND_DIST default location).
 && PKG_DIR="$(python -c 'import netmon, pathlib; print(pathlib.Path(netmon.__file__).resolve().parent)')" \
 && rm -rf "$PKG_DIR/static" \
 && cp -a /src/backend/netmon/static "$PKG_DIR/static" \
 && test -f "$PKG_DIR/static/index.html" \
 && command -v netmon

# ---------------------------------------------------------------------------
# Stage 3: runtime
# ---------------------------------------------------------------------------
FROM pybase AS runtime

LABEL org.opencontainers.image.title="NetworkMonitor" \
      org.opencontainers.image.description="License-free network monitor (ping, SNMP, HTTP, TCP, DNS) with a web UI"

# The OS trust store (inherited from pybase) already holds deploy/certs: apt and the app
# (netmon/tlsutil.py, via truststore) trust the corporate CAs.
# iputils-ping: last-resort ICMP fallback used by the ping check.
RUN apt-get update \
 && apt-get install -y --no-install-recommends iputils-ping tzdata \
 && rm -rf /var/lib/apt/lists/*

ARG NETMON_UID=10001
ARG NETMON_GID=10001
RUN groupadd --system --gid "${NETMON_GID}" netmon \
 && useradd --system --uid "${NETMON_UID}" --gid netmon --home-dir /data \
            --no-create-home --shell /usr/sbin/nologin netmon \
 && mkdir -p /data \
 && chown netmon:netmon /data \
 && chmod 0750 /data

COPY --from=backend /opt/netmon/venv /opt/netmon/venv

ENV PATH=/opt/netmon/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_URL=postgresql://netmon@db:5432/netmon \
    NETMON_DATA_DIR=/data \
    NETMON_HOST=0.0.0.0 \
    NETMON_PORT=8080

VOLUME ["/data"]
WORKDIR /data
USER netmon:netmon
EXPOSE 8080

# /api/health returns 200 while the process is alive (status "degraded" if the DB is down).
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('NETMON_PORT', '8080'), timeout=4)"]

ENTRYPOINT ["netmon"]
CMD ["serve"]
