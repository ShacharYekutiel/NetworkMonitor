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
#   - [2b-3] SNMP traps / syslog receivers listen on unprivileged ports inside the
#     container (NETMON_TRAP_PORT=1162, NETMON_SYSLOG_PORT=1514 UDP+TCP); compose
#     publishes host 162/udp -> 1162 and 514/udp+tcp -> 1514. Nothing listens until the
#     owner turns the Traps / Syslog features on in Settings -> Features.
#   - LDAPS (Active Directory login) trusts the same CAs as HTTPS: the OS store (with
#     deploy/certs baked in) + certifi + NETMON_EXTRA_CA_DIR (default /etc/netmon/certs,
#     mounted by compose).
#   - [3] NetFlow v5/v9 + IPFIX on UDP 2055, IPFIX on UDP 4739, sFlow on UDP 6343
#     (NETMON_NETFLOW_PORT / NETMON_IPFIX_PORT / NETMON_SFLOW_PORT, all > 1024, published
#     1:1). Nothing listens until the owner turns the Flows feature on.
#   - [3] Kerberos SSO: the `kerberos` extra (python-gssapi) is installed and linked against
#     MIT krb5 (runtime package libgssapi-krb5-2). gssapi has no Linux wheels, so the
#     backend stage compiles it (libkrb5-dev + gcc, not copied into the runtime). The
#     keytab is uploaded in the UI and stored encrypted in the DB; the app materialises it
#     under /data/run/ only while the feature is ON. asyncssh (config backup) is a core
#     dependency (pure Python on top of `cryptography`).
#   - [3b] "Ask the network" assistant (Amazon Bedrock): no new package. The app signs AWS
#     requests itself (SigV4 and the IAM Roles Anywhere X.509 variant) on top of httpx and
#     `cryptography`; botocore/boto3 are NOT installed (botocore is a CI test dependency
#     only). Outbound HTTPS follows HTTPS_PROXY / NO_PROXY (or "Proxy: direct" in the
#     assistant settings) and trusts the same CAs as above. Optional Roles Anywhere
#     certificate host files: NETMON_AWS_RA_CERT_FILE / _KEY_FILE / _CHAIN_FILE.
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
 && test -s /etc/ssl/certs/ca-certificates.crt \
 # Every corporate certificate must chain to a root in the store: Python/OpenSSL (pip and
 # the app) reject a chain that ends at an intermediate, even though npm accepts it.
 && for c in /usr/local/share/ca-certificates/netmon/netmon-*.crt; do \
      [ -f "$c" ] || continue; \
      if ! openssl verify -CAfile /etc/ssl/certs/ca-certificates.crt "$c" >/dev/null 2>&1; then \
        echo "ERROR: corporate CA $(basename "$c") is an intermediate certificate whose root is missing." >&2; \
        echo "  Subject: $(openssl x509 -in "$c" -noout -subject | sed 's/^subject= *//')" >&2; \
        echo "  Issued by (add this one too): $(openssl x509 -in "$c" -noout -issuer | sed 's/^issuer= *//')" >&2; \
        echo "  Export the TOP certificate of the chain (the root) into deploy/certs and rebuild." >&2; \
        echo "  See deploy/certs/README.md." >&2; \
        exit 1; \
      fi; \
    done
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
COPY deploy/docker/npm-tls-probe.js /usr/local/lib/netmon-npm-tls-probe.js
# The probe stops the build within seconds, with a pointer to deploy/certs, when the
# registry's certificate is not trusted (instead of minutes of npm retries).
RUN if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi; \
    NODE_USE_ENV_PROXY=1 NODE_NO_WARNINGS=1 node /usr/local/lib/netmon-npm-tls-probe.js "$(npm config get registry)" || exit 1; \
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
# [3] Build-only toolchain for python-gssapi (no Linux wheels on PyPI): krb5-config and the
# GSSAPI headers come from libkrb5-dev. Stays in this stage; the runtime gets only the
# shared libraries (libgssapi-krb5-2).
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev libkrb5-dev \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --upgrade pip \
 && pip install "/src/backend[kerberos]" \
 # Make sure the SPA ends up next to the installed package even if the package data
 # configuration does not include static/ (NETMON_FRONTEND_DIST default location).
 && PKG_DIR="$(python -c 'import netmon, pathlib; print(pathlib.Path(netmon.__file__).resolve().parent)')" \
 && rm -rf "$PKG_DIR/static" \
 && cp -a /src/backend/netmon/static "$PKG_DIR/static" \
 && test -f "$PKG_DIR/static/index.html" \
 && command -v netmon \
 # Every dependency declared in backend/pyproject.toml (e.g. ldap3 for Active Directory
 # login, phase 2b-3; asyncssh and the kerberos extra, phase 3) must be installed and
 # consistent in the venv.
 && pip check \
 # [1.3.3] re2: google-re2 bundles libre2 in its manylinux wheel (no apt package); confirm
 # the wheel imports here so a broken install fails the build rather than at runtime.
 && python -c "import asyncssh, gssapi, ldap3, re2; print('asyncssh', asyncssh.__version__, '/ gssapi ok / re2 ok')"

# ---------------------------------------------------------------------------
# Stage 3: runtime
# ---------------------------------------------------------------------------
FROM pybase AS runtime

LABEL org.opencontainers.image.title="NetworkMonitor" \
      org.opencontainers.image.description="License-free network monitor (ping, SNMP, HTTP, TCP, DNS) with a web UI"

# The OS trust store (inherited from pybase) already holds deploy/certs: apt and the app
# (netmon/tlsutil.py, via truststore) trust the corporate CAs.
# iputils-ping: last-resort ICMP fallback used by the ping check.
# libgssapi-krb5-2: [3] MIT krb5 GSSAPI library (~3 MB) for Kerberos SSO (python-gssapi).
# Without --no-install-recommends apt would pull krb5-config (debconf); the app writes its
# own krb5.conf (KRB5_CONFIG=/data/run/krb5.conf), so no /etc/krb5.conf is needed.
RUN apt-get update \
 && apt-get install -y --no-install-recommends iputils-ping tzdata libgssapi-krb5-2 \
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
# The compiled gssapi extension must load against the runtime's shared libraries (a
# missing libgssapi-krb5-2 would otherwise only show up when Kerberos SSO is turned on).
RUN /opt/netmon/venv/bin/python -c "import gssapi.raw, asyncssh, re2" \
 && /opt/netmon/venv/bin/pip check

ENV PATH=/opt/netmon/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_URL=postgresql://netmon@db:5432/netmon \
    NETMON_DATA_DIR=/data \
    NETMON_HOST=0.0.0.0 \
    NETMON_PORT=8080 \
    NETMON_TRAP_PORT=1162 \
    NETMON_SYSLOG_PORT=1514 \
    NETMON_NETFLOW_PORT=2055 \
    NETMON_IPFIX_PORT=4739 \
    NETMON_SFLOW_PORT=6343

VOLUME ["/data"]
WORKDIR /data
USER netmon:netmon
# 8080 web UI/API; 1162/udp SNMP traps; 1514/udp+tcp syslog; [3] 2055/udp NetFlow/IPFIX,
# 4739/udp IPFIX, 6343/udp sFlow (receivers listen only while their features are ON).
EXPOSE 8080 1162/udp 1514/udp 1514/tcp 2055/udp 4739/udp 6343/udp

# /api/health returns 200 while the process is alive (status "degraded" if the DB is down).
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('NETMON_PORT', '8080'), timeout=4)"]

ENTRYPOINT ["netmon"]
CMD ["serve"]
