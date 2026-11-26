# NetworkMonitor — Deployment

[English](#english) · [עברית](#עברית)

> **Production server (GitOps, HTTPS, backups):** see [production/README.md](production/README.md)
> and [docs/production.md](../docs/production.md). The options below are for evaluation and
> small single-host installs.

## English

There are three ways to install NetworkMonitor. In each one the web UI listens on port **8080**, the health check
is at `/api/health`, and every secret is generated automatically, so there is nothing to edit. On first visit the
setup wizard creates the admin user.

| | Docker (recommended) | Native Linux | Native Windows |
|---|---|---|---|
| Command | `docker compose -f deploy/docker-compose.yml up -d` | `sudo ./deploy/linux/install.sh` | `powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1` |
| Database | TimescaleDB 2.32 / PostgreSQL 16 container | distro PostgreSQL ≥ 14 (+ TimescaleDB when available) | PostgreSQL 16 via winget (TimescaleDB attempted) |
| Data | volumes `netmon_netmon-data`, `netmon_netmon-db` | `/var/lib/netmon` + PostgreSQL | `%ProgramData%\NetworkMonitor` + PostgreSQL |
| Service | `restart: unless-stopped` | systemd `netmon.service` | Windows service `NetworkMonitor` (NSSM) |
| Logs | `docker compose -f deploy/docker-compose.yml logs -f netmon` | `journalctl -u netmon -f` | `%ProgramData%\NetworkMonitor\logs\` |
| Remove | `docker compose -f deploy/docker-compose.yml down` (add `-v` to delete data) | `sudo ./deploy/linux/uninstall.sh [--purge]` | `.\deploy\windows\uninstall.ps1 [-Purge]` |

If TimescaleDB is not available, the app runs in plain PostgreSQL mode automatically. That mode is fully supported
and is the normal case on Windows. `GET /api/health/details` (signed in) reports `"timescale": true|false`; the
public `GET /api/health` answers only `{"status": "ok"}` (or `503 {"status": "fail"}` without a database).

### 1. Docker

Requirements: Docker Engine 24 or newer with the Compose plugin.

```bash
docker compose -f deploy/docker-compose.yml up -d
# open http://<server>:8080
```

- The first `up` builds the image and starts three services. `init` is one-shot: it writes a random DB password to
  `/data/db_password` in the `netmon-data` volume. `db` is TimescaleDB, not published to the host. `netmon` is the
  app, published on 8080.
- **Upgrade** after `git pull`: `docker compose -f deploy/docker-compose.yml up -d --build`. Migrations run
  automatically at startup.
- **Optional overrides:** copy `deploy/.env.example` to `deploy/.env`. You can change the host port
  (`NETMON_HTTP_PORT`), or use registry mirrors when Docker Hub rate-limits pulls.
- **Ping** works without root: the container runs as an unprivileged user, and compose sets
  `net.ipv4.ping_group_range`.
- **Prebuilt image** (when releases are published): set `NETMON_IMAGE=ghcr.io/<owner>/<repo>:<version>` in
  `deploy/.env`, then run `docker compose -f deploy/docker-compose.yml pull netmon` and `up -d`.

### 2. Native Linux (systemd)

Supported: Debian 11+, Ubuntu 22.04+, RHEL / Rocky / Alma / CentOS Stream 8+, Fedora. Run as root:

```bash
sudo ./deploy/linux/install.sh                     # from a git checkout (builds the UI; installs distro Node.js if needed)
sudo ./install.sh                                  # from an extracted release tarball (wheel included)
sudo ./deploy/linux/install.sh --tarball https://github.com/<owner>/<repo>/releases/download/v0.1.0/netmon-0.1.0.tar.gz
```

The script is idempotent. Run it again to upgrade: it keeps the database, `/var/lib/netmon` and the password. It
installs PostgreSQL from the distribution, or from the PGDG repository when the distribution's version is too old or
on RHEL. It installs TimescaleDB from the official Timescale repository, best effort. Python ≥ 3.12 comes from
the distribution; when the distribution has none (Debian 12, Ubuntu 22.04), a standalone build is installed under
`/opt/netmon/python`. The script then creates the `netmon` user, the venv in `/opt/netmon/venv` and
`/var/lib/netmon/netmon.env` (mode 0600). It installs `netmon.service` with
`AmbientCapabilities=CAP_NET_RAW CAP_NET_BIND_SERVICE` (ping fallback; traps on UDP 162 and syslog on UDP/TCP 514
without root), opens the HTTP port in firewalld/ufw, and prints the URL, the firewall lines for traps/syslog/flows and
any port conflict (see [Traps and syslog](#traps-and-syslog), [Flows](#flows-netflow--ipfix--sflow-phase-3)). For
Kerberos SSO it installs `libgssapi-krb5-2` (RHEL: `krb5-libs`) and builds python-gssapi (gcc + krb5 headers); if
that is not possible it warns and installs without Kerberos.
Options: `--port N`, `--no-timescale`, `--no-firewall`, `--no-kerberos`, `--https-proxy URL`, `--no-proxy LIST`,
`--wheel FILE`, `--source DIR`.

### 3. Native Windows (service)

Requirements: Windows 10/11 or Server 2019+ (64-bit), an elevated PowerShell, and winget (App Installer).

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1
```

The script installs Python 3.12 and PostgreSQL 16 with winget if they are missing, and tries TimescaleDB. From a git checkout it
builds the web UI, and installs Node.js LTS with winget if needed. It creates
the venv in `%ProgramFiles%\NetworkMonitor` and the data directory `%ProgramData%\NetworkMonitor` (restricted to
SYSTEM and Administrators), and writes `netmon.env`. It registers the `NetworkMonitor` service through NSSM. NSSM is
taken from `deploy\windows\nssm.exe` if you put one there; otherwise it is downloaded. The service runs as
LocalSystem with automatic start. The script then opens TCP 8080 in Windows Firewall and prints the URL. If
PostgreSQL was already installed, pass `-PostgresSuperPassword` or type the password when prompted. Other options:
`-Port`, `-Wheel`, `-SourceDir`, `-SkipTimescale`, `-NssmPath`, `-IngestRemoteAddress`, `-DisableWindowsSnmpTrap`,
`-HttpsProxy`, `-NoProxy`.
It also adds the firewall rules `NetworkMonitor-Traps` (UDP 162), `NetworkMonitor-Syslog-UDP` (UDP 514),
`NetworkMonitor-Syslog-TCP` (TCP 514) and, phase 3, `NetworkMonitor-NetFlow` (UDP 2055), `NetworkMonitor-IPFIX`
(UDP 4739), `NetworkMonitor-sFlow` (UDP 6343) (removed by `uninstall.ps1`; `-IngestRemoteAddress 10.0.0.0/8,LocalSubnet`
limits the senders) and warns when the Windows **SNMP Trap** service (`SNMPTRAP`) holds UDP 162:
`Stop-Service SNMPTRAP; Set-Service SNMPTRAP -StartupType Disabled`, or re-run with `-DisableWindowsSnmpTrap`.

### Traps and syslog

NetworkMonitor receives SNMP traps on **UDP 162** and syslog on **UDP and TCP 514**. Everything is **off** after
install or upgrade, and nothing listens on those ports until an administrator turns on **Traps** / **Syslog** in
*Settings → Features* (no server change, no restart). The status line there says "Listening on UDP 162" or the
error (for example "Address already in use" when another program holds the port).

| | Docker (`deploy/docker-compose.yml`) | Native Linux | Native Windows |
|---|---|---|---|
| Ports | host 162/udp → container 1162, host 514/udp+tcp → 1514 (the app is non-root). Other host ports: `NETMON_TRAP_HOST_PORT`, `NETMON_SYSLOG_HOST_PORT` in `deploy/.env` | 162 / 514 directly (`CAP_NET_BIND_SERVICE`, no root) | 162 / 514 directly |
| Sender IP kept | yes for other machines on **Linux Docker Engine**; **no on Docker Desktop** (below) | yes | yes |
| Firewall | ufw does **not** filter published Docker ports; see [production/README.md](production/README.md) (`DOCKER-USER`) | `ufw allow from <LAN> to any port 162 proto udp` (and 514 udp, 514 tcp) — the installer prints them | rules added by the installer |
| Conflicts | `ss -lunpt '( sport = :162 or sport = :514 )'` (snmptrapd, rsyslog `imudp`) | same; the installer warns | Windows `SNMPTRAP` service; the installer warns |

**Docker Desktop (Windows/macOS) does not keep the sender's IP address.** Its port forwarding (vpnkit / WSL2) makes
every message look as if it came from one internal address, so messages cannot be matched to devices by address
(only by the trap agent address or the syslog hostname), and the Logs page shows a NAT warning. Test traps and syslog
on a Linux server (Docker Engine) or with the native Windows installer. Also on Linux, a test sent **from the Docker
host itself** arrives from the bridge gateway (`172.x.0.1`): send from another machine.

**Device configuration examples** (syntax varies by model and firmware — **verify on the device**; replace
`10.0.0.50` with the NetworkMonitor server). Send syslog at level `warning`: `informational` logs every CLI login
and command and fills the disk quickly (about 75 MB/day per message per second).

| Device | Syslog | Traps |
|---|---|---|
| HPE Comware 7 (e.g. FlexFabric 5945 IRF) | `info-center enable`<br>`info-center loghost 10.0.0.50 facility local7`<br>`info-center source default loghost level warning`<br>`info-center loghost source Vlan-interface<mgmt>` | v2c: `snmp-agent target-host trap address udp-domain 10.0.0.50 params securityname <community> v2c`<br>`snmp-agent trap enable`<br>`snmp-agent trap source Vlan-interface<mgmt>`<br>v3: `snmp-agent usm-user v3 <user> <group> simple authentication-mode sha <pw> privacy-mode aes128 <pw>` + `snmp-agent target-host trap address udp-domain 10.0.0.50 params securityname <user> v3 privacy` (the same user/keys as for polling work for traps) |
| ArubaOS-Switch | `logging 10.0.0.50`<br>`logging severity warning`<br>`logging facility local7` | `snmp-server host 10.0.0.50 community <community> trap-level all`<br>`snmp-server enable traps link-change <ports>`<br>v3: `snmpv3 notify` / `snmpv3 targetaddress` (see the switch guide) |
| Check Point Gaia (clish) | `add syslog log-remote-address 10.0.0.50 level warning`<br>`save config`<br>(Gaia OS messages only — **not** Log Exporter firewall traffic logs, which would flood the receiver) | `add snmp traps receiver 10.0.0.50 version v2 community <community>`<br>`set snmp traps trap <name> enable`<br>`save config` |

The device should send from the address NetworkMonitor monitors it on. Otherwise add the other address in the device
form (*Additional source addresses*) or use "Assign to device" on the *Unknown sources* tab of the Logs page.

### Flows (NetFlow / IPFIX / sFlow) [phase 3]

NetworkMonitor collects flow export on **UDP 2055** (NetFlow v5/v9 and IPFIX), **UDP 4739** (IPFIX) and **UDP 6343**
(sFlow v5). As with traps/syslog, nothing listens until an administrator turns on **Flows** in *Settings → Features*.
An exporter whose address belongs to a monitored device (its address or an *Additional source address*) is accepted
automatically; any other exporter is listed under *Flows → Exporters* as pending and its data is dropped until it is
accepted.

| | Docker (`deploy/docker-compose.yml`) | Native Linux | Native Windows |
|---|---|---|---|
| Ports | published 1:1 (all > 1024). Other host ports: `NETMON_NETFLOW_HOST_PORT`, `NETMON_IPFIX_HOST_PORT`, `NETMON_SFLOW_HOST_PORT` in `deploy/.env` | 2055 / 4739 / 6343 directly (no capability needed) | the same |
| Firewall | `DOCKER-USER` as for traps/syslog (production: `ops/ingest-firewall.sh` covers the flow ports) | `ufw allow from <LAN> to any port 2055 proto udp` (and 4739, 6343) — the installer prints them | rules `NetworkMonitor-NetFlow`, `NetworkMonitor-IPFIX`, `NetworkMonitor-sFlow` added by the installer (`-IngestRemoteAddress` applies) |
| Conflicts | `ss -lunp '( sport = :2055 or sport = :4739 or sport = :6343 )'` (nfcapd, pmacct, …) | same; the installer warns | the installer warns |

Docker Desktop loses the sender IP here too: NetFlow/IPFIX exporters are identified by their source address (sFlow by
the agent address inside the datagram), so test flows on Linux or with the native Windows installer.

Exporter examples (verify on the device; `10.0.0.50` = NetworkMonitor). NetFlow/IPFIX exporters must use an **active
timeout ≤ 60 s** and export **ingress** only (otherwise traffic is counted twice). Recommended: export from the core
and the firewall, not from every access switch (≈ 0.4 GB of database per busy exporter).

| Device | Flow export |
|---|---|
| HPE Comware 7 (FF 5945 IRF), sFlow | `sflow agent ip <mgmt ip>`<br>`sflow source ip <mgmt ip>`<br>`sflow collector 1 ip 10.0.0.50 port 6343 description netmon`<br>per uplink interface: `sflow sampling-rate 4096` and `sflow flow collector 1` |
| ArubaOS-Switch, sFlow | `sflow 1 destination 10.0.0.50 6343`<br>`sflow 1 sampling <ports> 4096` (syntax varies by release) |
| Check Point Gaia (clish), NetFlow v9 / IPFIX | `add netflow collector ip 10.0.0.50 port 2055 export-format Netflow_V9`<br>`save config` (or `IPFIX` to port 4739); active timeout 60 s where the version allows |
| Cisco IOS, NetFlow v9 | `flow exporter NETMON` / `destination 10.0.0.50` / `transport udp 2055` / `export-protocol netflow-v9`; flow monitor with `cache timeout active 60`, applied `ip flow monitor … input` |

### Config backup, single sign-on, chat acknowledge [phase 3]

- **Config backup** connects **out** to the devices over SSH (TCP 22); no inbound port. Credentials are entered in
  the UI and stored encrypted (`secret.key`); nothing to configure on the server.
- **Kerberos SSO (Windows integrated login)** works with **Docker and native Linux** (MIT krb5: the image ships
  `libgssapi-krb5-2` and python-gssapi; `install.sh` installs `libgssapi-krb5-2`/`krb5-libs` and builds gssapi, or
  warns and continues without it, see `--no-kerberos`). It is **not available on native Windows** (the feature reports
  "unavailable"). The AD administrator creates an SPN and a keytab once (steps:
  [docs/production.md](../docs/production.md#kerberos-sso-ad-administrator-once)); the keytab is **uploaded in the UI**
  (*Settings → Single sign-on → Kerberos*) and stored encrypted in the database — no file on the host. The server
  clock must be synchronised with the domain controllers (NTP; > 5 min skew breaks Kerberos).
- **Sign in with Microsoft (Entra ID / OIDC)**, **Telegram/Teams acknowledge** and **reports to Teams/Telegram** need
  outbound HTTPS. Behind a proxy the app needs `HTTPS_PROXY` / `NO_PROXY` in its process environment (`netmon.env`
  is not enough): Docker — `environment:` of the `netmon` service (production: `/etc/netmon-gitops/app.env`); native
  Linux — `sudo -E ./install.sh` with `HTTPS_PROXY` set, or `--https-proxy <url> [--no-proxy <list>]` (written to
  `/etc/systemd/system/netmon.service.d/20-proxy.conf`, kept on re-runs); Windows — `install.ps1 -HttpsProxy <url>
  [-NoProxy <list>]` (or `HTTPS_PROXY` in the installer's session; kept on re-runs). The proxy must allow long
  requests (≥ 70 s) to `api.telegram.org` (long polling), or lower *Telegram poll timeout* in the settings. SSH,
  SNMP, flows and LDAP never use the proxy.

### Backup and restore

The data is in PostgreSQL. Back it up with `pg_dump` in custom format. The data directory holds `secret.key` (if it
is lost, users only have to log in again) and the DB password (Docker) or `netmon.env` (native).

**Restore = same TimescaleDB version first, then update [1.5.1].** A dump must be restored into the TimescaleDB
extension version that wrote it, and the extension updated afterwards (Timescale's troubleshooting guide, "Version
mismatch when dumping and restoring"). A plain `CREATE EXTENSION timescaledb` creates the server's newest version, and
`timescaledb_post_restore()` then stops with `catalog version mismatch, expected "2.32.1" seen "2.31.0"`, leaving the
database in restore mode (background jobs off). So the steps below read the version from the dump, create exactly
that version, restore, and run `ALTER EXTENSION timescaledb UPDATE`. The server must still ship the dump's version:
`timescale/timescaledb` images and the Linux `timescaledb-2-postgresql-16` packages keep the earlier releases (the
2.32.1 image can create 2.31.0); an older server cannot restore a dump from a newer extension. Use a newer server, or an
image tag of the dump's version (`timescale/timescaledb:<version>-pg16`).

**Docker**

```bash
C="docker compose -f deploy/docker-compose.yml"
# backup
$C exec -T db pg_dump -U netmon -Fc netmon > netmon-$(date +%F).dump
# restore (replaces the current database)
F=netmon-YYYY-MM-DD.dump
# 1. the TimescaleDB version that wrote the dump, and check this server can create it (must print it back)
V=$($C exec -T db pg_restore -a -n _timescaledb_catalog -t metadata -f - < "$F" | awk -F'\t' '$1 == "timescaledb_version" {print $2}')
echo "dump written by TimescaleDB $V"
$C exec -T db psql -XAt -U netmon -d postgres -c "SELECT version FROM pg_available_extension_versions WHERE name = 'timescaledb' AND version = '$V'"
# 2. replace the database (template0: template1 of the image already holds the extension)
$C stop netmon
$C exec -T db psql -X -U netmon -d postgres -c 'DROP DATABASE IF EXISTS netmon WITH (FORCE)' -c 'CREATE DATABASE netmon TEMPLATE template0'
$C exec -T db psql -X -U netmon -d netmon -c "CREATE EXTENSION timescaledb VERSION '$V'" -c 'SELECT timescaledb_pre_restore()'
$C exec -T db pg_restore -U netmon -d netmon --no-owner < "$F"
$C exec -T db psql -X -U netmon -d netmon -c 'SELECT timescaledb_post_restore()'
# 3. update to the server's version (a separate psql -X call: it must be the first statement of a session)
$C exec -T db psql -X -U netmon -d netmon -c 'ALTER EXTENSION timescaledb UPDATE'
$C start netmon
# optional: the data volume (DB password, secret.key)
docker run --rm -v netmon_netmon-data:/data -v "$PWD":/backup busybox tar czf /backup/netmon-data.tgz -C /data .
```

If step 1 prints nothing, this image cannot create that version: set `NETMON_DB_IMAGE=timescale/timescaledb:<V>-pg16`
(or a newer tag) in `deploy/.env`, `$C up -d db`, and restore again. A dump without TimescaleDB (plain PostgreSQL
mode, `V` empty) restores with `CREATE EXTENSION IF NOT EXISTS timescaledb` in step 2 and no step 3. The production
(GitOps) server does all of this in `ops/restore.sh`.

**Linux (native)**

```bash
sudo runuser -u postgres -- pg_dump -Fc netmon > netmon-$(date +%F).dump
# restore
F=netmon-YYYY-MM-DD.dump
V=$(pg_restore -a -n _timescaledb_catalog -t metadata -f - < "$F" | awk -F'\t' '$1 == "timescaledb_version" {print $2}')
sudo runuser -u postgres -- psql -XAt -c "SELECT version FROM pg_available_extension_versions WHERE name = 'timescaledb' AND version = '$V'"
sudo systemctl stop netmon
sudo runuser -u postgres -- psql -X -c 'DROP DATABASE IF EXISTS netmon WITH (FORCE)' -c 'CREATE DATABASE netmon OWNER netmon TEMPLATE template0'
# TimescaleDB only (V not empty): the next line, and the post_restore and UPDATE lines after pg_restore
sudo runuser -u postgres -- psql -X -d netmon -c "CREATE EXTENSION timescaledb VERSION '$V'" -c 'SELECT timescaledb_pre_restore()'
sudo runuser -u postgres -- pg_restore -d netmon --no-owner --role=netmon < "$F"
sudo runuser -u postgres -- psql -X -d netmon -c 'SELECT timescaledb_post_restore()'
sudo runuser -u postgres -- psql -X -d netmon -c 'ALTER EXTENSION timescaledb UPDATE'
sudo systemctl start netmon
```

**Windows (native, elevated PowerShell)**

```powershell
$pg = 'C:\Program Files\PostgreSQL\16\bin'
# the password is in DATABASE_URL in C:\ProgramData\NetworkMonitor\netmon.env
$env:PGPASSWORD = '<password from netmon.env>'
& "$pg\pg_dump.exe" -h 127.0.0.1 -U netmon -Fc -f "netmon-$(Get-Date -Format yyyy-MM-dd).dump" netmon
# restore: the TimescaleDB version that wrote the dump
& "$pg\pg_restore.exe" -a -n _timescaledb_catalog -t metadata -f - netmon-YYYY-MM-DD.dump | Select-String '^timescaledb_version\t'
# then Stop-Service NetworkMonitor and run the Linux steps with psql.exe / pg_restore.exe as postgres
# (-h 127.0.0.1 -U postgres; password from the PostgreSQL installation), then Start-Service NetworkMonitor
```

The Windows release zip of TimescaleDB holds ONE version (e.g. the 2.32.1 zip: `timescaledb-2.32.1.dll`,
`timescaledb-tsl-2.32.1.dll`, the loader `timescaledb.dll`), unlike the Docker images and Linux packages; versions
installed earlier stay because the installer only copies files. If `pg_available_extension_versions` does not list the
dump's version `V`, download `https://github.com/timescale/timescaledb/releases/download/<V>/timescaledb-postgresql-16-windows-amd64.zip`
and copy only `timescaledb-<V>.dll` and `timescaledb-tsl-<V>.dll` to `C:\Program Files\PostgreSQL\16\lib` and
`timescaledb--<V>.sql` to `...\16\share\extension` (NOT `timescaledb.dll` or `timescaledb.control`: they belong to the
newer version), then restore.

With TimescaleDB, `pg_dump` prints a warning about circular foreign keys in `continuous_agg`. That warning is
harmless: the custom-format dump restores cleanly with the steps above. A dump taken in plain PostgreSQL mode
restores into either mode.

### TimescaleDB updates [1.5.0]

A newer `timescale/timescaledb` image (same PostgreSQL major) leaves the extension in an existing database at its
old version. At startup the app runs `ALTER EXTENSION timescaledb UPDATE` itself when the server provides a newer
version (logged as `TimescaleDB extension updated from X to Y`). [1.5.1] The running app also does it after a
database restart, within ~30 s: with Docker, `docker compose up -d` after a db image change recreates only `db`, the
app keeps running, re-connects (log: `Advisory lock re-acquired`) and updates the extension; no app restart and no
extra command (the production GitOps server: `ops/sync.sh` also recreates `netmon`, see docs/production.md
"[1.5.1] Upgrade and rollback"). To check: `docker compose -f deploy/docker-compose.yml logs netmon | grep TimescaleDB`.
That needs the owner of the extension: in
Docker the app's database user is it. Native installs create the extension as `postgres`, so after an OS package update of
TimescaleDB the app logs a warning instead; then run, as `postgres`, with NetworkMonitor stopped:
`psql -X -d netmon -c "ALTER EXTENSION timescaledb UPDATE"`. A PostgreSQL MAJOR upgrade (e.g. 16 to 17) is a
separate data-directory migration and is never done automatically.

The update cannot be undone: after it, an OLDER `timescale/timescaledb` image (or package) cannot open the database
(`could not access file "$libdir/timescaledb-<version>"`). Take a `pg_dump` before moving to a newer image; going back
means restoring that dump (see "Backup and restore") under the old image, or keeping the new image.

### Prebuilt image (no build on the host)

Every push to `main` (and `phase*` branches) publishes a ready image to GitHub Container
Registry, so a host that cannot download npm/PyPI packages does not need to build:

```sh
# deploy/.env
NETMON_IMAGE=ghcr.io/shacharyekutiel/networkmonitor:latest   # or :phase2, :sha-<commit>

docker login ghcr.io          # only if the package is private (user + PAT with read:packages)
docker compose -f deploy/docker-compose.yml pull netmon
docker compose -f deploy/docker-compose.yml up -d --no-build
```

Corporate CAs in `deploy/certs` are mounted into the container (`/etc/netmon/certs`) and
trusted at runtime, so the prebuilt image works behind a TLS-inspecting firewall as well.
To update later: `pull netmon` again, then `up -d --no-build`.

### Troubleshooting: certificate errors (`UNABLE_TO_GET_ISSUER_CERT_LOCALLY`, `SELF_SIGNED_CERT_IN_CHAIN`)

The build stops with `ERROR: TLS certificate check failed for https://registry.npmjs.org/`, or `npm ci` /
`pip install` report `UNABLE_TO_GET_ISSUER_CERT_LOCALLY`, `SELF_SIGNED_CERT_IN_CHAIN` or
`CERTIFICATE_VERIFY_FAILED`; or, at runtime, Telegram / webhook / email notifications or HTTPS sensors fail with
`certificate verify failed`. Your network inspects HTTPS: a firewall or proxy re-signs every connection with the
organisation's own root CA, which your PC trusts but a container does not.

Fix: export that root CA (on Windows: browser padlock → certificate → top of the chain → export as
*Base-64 encoded X.509*; or `certlm.msc` → *Trusted Root Certification Authorities*), save it into
[`deploy/certs/`](certs/README.md) and rebuild:

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

Nothing else to configure: every `*.crt` / `*.pem` / `*.cer` in `deploy/certs/` (PEM or DER) is trusted by npm, pip
and apt during the build and by the app at runtime. The files are git-ignored. Step-by-step export instructions:
[deploy/certs/README.md](certs/README.md).

Native installs: on Windows the app trusts the Windows certificate store, so a CA deployed by IT works as is;
`install.ps1` also trusts any files in `deploy\certs` (web UI build, pip and the service). On Linux
`install.sh` adds the files in `deploy/certs` to the system trust store.

### Troubleshooting: `npm ci` fails during `docker compose up --build`

Errors such as `npm error Exit handler never called!`, `ECONNRESET` or `ETIMEDOUT` in the
`frontend` build stage mean the build host cannot reliably download packages from
registry.npmjs.org (corporate proxy, firewall, TLS inspection). With TLS inspection, `npm ci` may only print
`Exit handler never called!`: see the certificate section above first. The Dockerfile already
retries with conservative settings. If it still fails:

- Check access from a container: `docker run --rm node:26-slim npm view react version`.
- Behind an HTTP proxy, configure it in Docker Desktop (Settings → Resources → Proxies) so builds use it.
- With a corporate npm mirror (Nexus, Artifactory):
  `docker compose -f deploy/docker-compose.yml build --build-arg NPM_REGISTRY=https://<mirror>/repository/npm/`
  and then `docker compose -f deploy/docker-compose.yml up -d`.
- Or build on another network (e.g. a phone hotspot) and run there afterwards: only the build needs the npm registry.

### Files

| Path | Purpose |
|---|---|
| `../Dockerfile` | Multi-stage build: Node builds the SPA, Python installs `netmon`, and the slim runtime runs as uid 10001 |
| `docker-compose.yml` | Production stack (`init` + `db` + `netmon`) |
| `docker-compose.dev.yml` | Development DB only (localhost:5432, `netmon`/`netmon`) |
| `.env.example` | Optional compose overrides |
| `linux/install.sh`, `linux/uninstall.sh`, `linux/netmon.service` | Native Linux |
| `windows/install.ps1`, `windows/uninstall.ps1` | Native Windows |
| `certs/` | Corporate root CA certificates for TLS-inspecting networks (git-ignored; see `certs/README.md`) |
| `import-ca-certs.sh` | Normalises `certs/*` into the system trust store (used by the Dockerfile and `install.sh`) |

---

<div dir="rtl">

## עברית

יש שלוש דרכים להתקין את NetworkMonitor. בכל אחת מהן ממשק הווב מאזין בפורט **8080**, ובדיקת התקינות זמינה
בכתובת `/api/health`. כל הסודות (סיסמאות, מפתחות) נוצרים אוטומטית, כך שאין מה לערוך. בכניסה הראשונה לממשק, אשף
ההתקנה יוצר את משתמש המנהל.

| | Docker (מומלץ) | לינוקס (מקומית) | Windows (מקומית) |
|---|---|---|---|
| התקנה | <span dir="ltr">`docker compose -f deploy/docker-compose.yml up -d`</span> | <span dir="ltr">`sudo ./deploy/linux/install.sh`</span> | <span dir="ltr">`powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1`</span> |
| מסד נתונים | קונטיינר TimescaleDB 2.32 / PostgreSQL 16 | PostgreSQL ≥ 14 מההפצה (+TimescaleDB אם זמין) | PostgreSQL 16 דרך winget (ניסיון להתקין TimescaleDB) |
| נתונים | הווליומים `netmon_netmon-data`, `netmon_netmon-db` | <span dir="ltr">`/var/lib/netmon`</span> + PostgreSQL | <span dir="ltr">`%ProgramData%\NetworkMonitor`</span> + PostgreSQL |
| שירות | <span dir="ltr">`restart: unless-stopped`</span> | שירות systemd בשם `netmon` | שירות Windows בשם `NetworkMonitor` (NSSM) |
| הסרה | <span dir="ltr">`docker compose -f deploy/docker-compose.yml down`</span> (עם `-v` גם הנתונים נמחקים) | <span dir="ltr">`sudo ./deploy/linux/uninstall.sh [--purge]`</span> | <span dir="ltr">`.\deploy\windows\uninstall.ps1 [-Purge]`</span> |

כש-TimescaleDB לא זמין, המערכת עוברת אוטומטית ל-PostgreSQL רגיל. מצב זה נתמך במלואו, והוא המצב הרגיל ב-Windows.
השדה `timescale` בתשובה של `/api/health/details` (למשתמש מחובר) מראה באיזה מצב המערכת פועלת. הכתובת הציבורית
`/api/health` מחזירה רק `{"status": "ok"}` (או `503` עם `{"status": "fail"}` כשאין מסד נתונים).

### 1. Docker

דרישות: Docker Engine בגרסה 24 ומעלה, עם תוסף Compose.

<div dir="ltr">

```bash
docker compose -f deploy/docker-compose.yml up -d
```

</div>

- ההרצה הראשונה של `up` בונה את האימג' ומפעילה שלושה שירותים. `init` רץ פעם אחת: הוא יוצר סיסמת DB אקראית
  בווליום הנתונים. `db` הוא TimescaleDB, ואינו חשוף למחשב המארח. `netmon` הוא האפליקציה, בפורט 8080.
- **עדכון גרסה** אחרי `git pull`: <span dir="ltr">`docker compose -f deploy/docker-compose.yml up -d --build`</span>.
  המיגרציות של מסד הנתונים רצות אוטומטית.
- **שינויים אופציונליים:** העתיקו את `deploy/.env.example` ל-`deploy/.env`. כך אפשר לשנות את הפורט
  (`NETMON_HTTP_PORT`), או להשתמש במראות (mirrors) של Registry כש-Docker Hub מגביל הורדות.
- **Ping** עובד בלי הרשאות root: הקונטיינר רץ כמשתמש לא מורשה, וה-sysctl בשם `ping_group_range` מוגדר ב-compose.

### 2. לינוקס (systemd)

נתמכות ההפצות Debian 11 ומעלה, Ubuntu 22.04 ומעלה, RHEL / Rocky / Alma 8 ומעלה ו-Fedora. הריצו כ-root:

<div dir="ltr">

```bash
sudo ./deploy/linux/install.sh          # from a git checkout
sudo ./install.sh                       # from an extracted release tarball
```

</div>

אפשר להריץ את הסקריפט שוב בבטחה. הרצה חוזרת מעדכנת את האפליקציה, ושומרת על מסד הנתונים, על התיקייה
<span dir="ltr">`/var/lib/netmon`</span> ועל הסיסמה. הסקריפט מתקין PostgreSQL, ומנסה להתקין TimescaleDB מהמאגר
הרשמי. אם ההתקנה של TimescaleDB נכשלת, ההתקנה ממשיכה במצב PostgreSQL רגיל. הסקריפט מתקין גם Python 3.12 ומעלה.
אחר כך הוא יוצר את המשתמש `netmon`, את סביבת ה-venv בתיקייה <span dir="ltr">`/opt/netmon/venv`</span> ואת הקובץ
`netmon.env` עם סיסמה אקראית. בסוף הוא מתקין את השירות `netmon.service` עם ההרשאות `CAP_NET_RAW` ו-`CAP_NET_BIND_SERVICE`
(קבלת traps ו-syslog בפורטים 162 ו-514 בלי root), מפעיל אותו ומדפיס את הכתובת, את שורות ה-firewall ל-traps/syslog
והתנגשויות פורטים (ראו "Traps ו-syslog" למטה).

### 3. Windows (שירות)

דרישות: Windows 10/11 או Server 2019 ומעלה (64 ביט), PowerShell שרץ כמנהל, ו-winget.

<div dir="ltr">

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1
```

</div>

הסקריפט מתקין Python 3.12 ו-PostgreSQL 16 דרך winget אם הם חסרים, ומנסה להתקין TimescaleDB. הוא יוצר את
התוכנה בתיקייה <span dir="ltr">`%ProgramFiles%\NetworkMonitor`</span> ואת הנתונים בתיקייה
<span dir="ltr">`%ProgramData%\NetworkMonitor`</span>. הוא רושם שירות Windows בשם `NetworkMonitor` (באמצעות
NSSM), פותח את פורט 8080 בחומת האש ומדפיס את הכתובת. אם PostgreSQL כבר היה מותקן, הסקריפט יבקש את סיסמת המשתמש
`postgres`. אפשר גם להעביר אותה בפרמטר `-PostgresSuperPassword`.
הסקריפט מוסיף גם כללי חומת אש ל-traps ‏(UDP 162) ול-syslog ‏(UDP/TCP 514), ומזהיר אם שירות **SNMP Trap** של
Windows ‏(`SNMPTRAP`) תופס את UDP 162. הפרמטר `-DisableWindowsSnmpTrap` עוצר ומנטרל אותו.

### Traps ו-syslog

NetworkMonitor מקבל SNMP traps ב-**UDP 162** ו-syslog ב-**UDP ו-TCP 514**. אחרי התקנה או שדרוג הכול **כבוי**,
ושום דבר לא מאזין בפורטים האלה עד שמנהל מפעיל **Traps** / **Syslog** ב-*הגדרות ← תכונות* (בלי שינוי בשרת ובלי
הפעלה מחדש).

- **Docker:** הפורטים 162 ו-514 במחשב מועברים לפורטים 1162 ו-1514 בקונטיינר (האפליקציה לא רצה כ-root). אם תוכנה
  אחרת במחשב כבר משתמשת בהם, אפשר לשנות ב-<span dir="ltr">`deploy/.env`</span>
  (`NETMON_TRAP_HOST_PORT`, ‏`NETMON_SYSLOG_HOST_PORT`).
- **Docker Desktop ‏(Windows/macOS) לא שומר את כתובת ה-IP של השולח.** כל ההודעות נראות כאילו הגיעו מכתובת פנימית
  אחת, ולכן אי אפשר לשייך אותן למכשירים לפי כתובת, ובדף הלוגים מופיעה אזהרת NAT. בודקים traps ו-syslog על שרת
  לינוקס או עם ההתקנה הרגילה של Windows.
- **ufw לא מסנן פורטים ש-Docker מפרסם.** ראו [production/README.md](production/README.md) ‏(`DOCKER-USER`).
- דוגמאות הגדרה ל-HPE Comware, ‏ArubaOS-Switch ו-Check Point Gaia נמצאות בחלק האנגלי
  ([Traps and syslog](#traps-and-syslog)). התחביר משתנה לפי דגם וגרסה — **יש לבדוק על המכשיר**. מומלץ לשלוח syslog
  ברמת `warning`.

### Flows ‏(NetFlow / IPFIX / sFlow) ‏[שלב 3]

NetworkMonitor אוסף flow export ב-**UDP 2055** ‏(NetFlow v5/v9 ו-IPFIX), ‏**UDP 4739** ‏(IPFIX) ו-**UDP 6343** ‏(sFlow v5).
כמו ב-traps/syslog, שום דבר לא מאזין עד שמנהל מפעיל **Flows** ב-*הגדרות ← תכונות*. ‏exporter שהכתובת שלו שייכת למכשיר
מנוטר מתקבל אוטומטית; אחר מופיע ב-*Flows ← Exporters* כממתין, והנתונים שלו נזרקים עד שמאשרים אותו.

- **Docker:** הפורטים מפורסמים 1:1 (כולם מעל 1024). שינוי בצד המחשב ב-<span dir="ltr">`deploy/.env`</span>
  (`NETMON_NETFLOW_HOST_PORT`, ‏`NETMON_IPFIX_HOST_PORT`, ‏`NETMON_SFLOW_HOST_PORT`). גם כאן Docker Desktop לא שומר את
  כתובת השולח.
- **לינוקס:** ההתקנה מדפיסה את שורות ה-ufw/firewalld ‏(2055, ‏4739, ‏6343 ב-UDP). **Windows:** ההתקנה מוסיפה את
  הכללים `NetworkMonitor-NetFlow`, ‏`NetworkMonitor-IPFIX`, ‏`NetworkMonitor-sFlow`.
- דוגמאות הגדרה בחלק האנגלי ([Flows](#flows-netflow--ipfix--sflow-phase-3)). ב-NetFlow/IPFIX: ‏active timeout של
  60 שניות לכל היותר, ו-ingress בלבד.

### גיבוי קונפיגורציה, SSO ואישור מהצ'אט ‏[שלב 3]

- **גיבוי קונפיגורציה** מתחבר **החוצה** למכשירים ב-SSH ‏(TCP 22). אין פורט נכנס, ואין מה להגדיר בשרת.
- **Kerberos SSO** ‏(כניסה אוטומטית עם משתמש Windows) עובד ב-**Docker ובלינוקס** בלבד, **לא** בהתקנת Windows. מנהל
  ה-AD יוצר SPN ו-keytab פעם אחת (ההוראות: [production/README.md](production/README.md)); את ה-keytab **מעלים בממשק**
  והוא נשמר מוצפן במסד הנתונים — שום קובץ בשרת. השעון של השרת חייב להיות מסונכרן ל-DC‏ (NTP).
- **Sign in with Microsoft**, אישור מ-Telegram/Teams ודוחות ל-Teams/Telegram צריכים HTTPS יוצא. מאחורי proxy מגדירים
  `HTTPS_PROXY` / `NO_PROXY` בסביבת התהליך של האפליקציה: ב-Docker ב-`environment:`, בלינוקס `install.sh --https-proxy`
  (או `sudo -E` עם `HTTPS_PROXY`), ב-Windows `install.ps1 -HttpsProxy`. ה-proxy חייב לאפשר בקשות ארוכות (70 שניות
  ומעלה) ל-`api.telegram.org`.

### גיבוי ושחזור

כל הנתונים נמצאים ב-PostgreSQL. מגבים אותם עם `pg_dump`, ומשחזרים עם `pg_restore`. הפקודות המלאות מופיעות בחלק
האנגלי למעלה ([Backup and restore](#backup-and-restore)). למשל, גיבוי ב-Docker:

<div dir="ltr">

```bash
docker compose -f deploy/docker-compose.yml exec -T db pg_dump -U netmon -Fc netmon > netmon-$(date +%F).dump
```

</div>

לפני שחזור, עצרו את שירות `netmon`. כש-TimescaleDB פעיל, הריצו `timescaledb_pre_restore()` לפני השחזור
ו-`timescaledb_post_restore()` אחריו. אם הקובץ `secret.key` אובד, המשתמשים רק יצטרכו להתחבר מחדש.

**‏[1.5.1] גרסת TimescaleDB בשחזור.** משחזרים dump **לאותה גרסה** של הרחבת TimescaleDB שכתבה אותו, ורק אחר כך
מעדכנים (`ALTER EXTENSION timescaledb UPDATE`). `CREATE EXTENSION timescaledb` רגיל יוצר את הגרסה החדשה ביותר בשרת,
ואז `timescaledb_post_restore()` נכשל ב-`catalog version mismatch` והמסד נשאר במצב שחזור (משימות הרקע כבויות).
לכן: קוראים את הגרסה מה-dump, יוצרים את המסד מ-`template0`, מריצים `CREATE EXTENSION timescaledb VERSION '<גרסה>'`,
משחזרים, ואז מעדכנים. השרת חייב לכלול את הגרסה של ה-dump: ה-images של `timescale/timescaledb` וחבילות הלינוקס
כוללים גם גרסאות קודמות (‏2.32.1 יודע ליצור 2.31.0); שרת **ישן** יותר לא יכול לשחזר dump של הרחבה חדשה יותר. ב-Windows
ה-zip כולל גרסה אחת בלבד. הפקודות המדויקות: בחלק האנגלי ([Backup and restore](#backup-and-restore)). בשרת הייצור
(GitOps) ‏`ops/restore.sh` עושה את כל זה לבד.

</div>

### עדכוני TimescaleDB ‏[1.5.0]

image חדש יותר של `timescale/timescaledb` (אותה גרסה ראשית של PostgreSQL) משאיר את ההרחבה במסד קיים בגרסה הישנה.
בעלייה, האפליקציה מריצה בעצמה `ALTER EXTENSION timescaledb UPDATE` כשהשרת מספק גרסה חדשה יותר (ביומן:
`TimescaleDB extension updated from X to Y`). ‏[1.5.1] האפליקציה הרצה עושה זאת גם אחרי הפעלה מחדש של המסד, תוך
כ-30 שניות: ב-Docker, ‏`docker compose up -d` אחרי החלפת image של ה-db יוצר מחדש רק את `db`, האפליקציה ממשיכה לרוץ,
מתחברת מחדש (ביומן: `Advisory lock re-acquired`) ומעדכנת את ההרחבה; בלי הפעלה מחדש של האפליקציה ובלי פקודה נוספת
(בשרת הייצור `ops/sync.sh` גם יוצר מחדש את `netmon`). לבדיקה:
<span dir="ltr">`docker compose -f deploy/docker-compose.yml logs netmon | grep TimescaleDB`</span>.
זה דורש את בעל ההרחבה: ב-Docker זה משתמש המסד של האפליקציה. בהתקנה
מקומית ההרחבה נוצרת כ-`postgres`, ולכן אחרי עדכון חבילת TimescaleDB של מערכת ההפעלה האפליקציה רושמת אזהרה; אז הריצו
כ-`postgres`, כש-NetworkMonitor עצור: `psql -X -d netmon -c "ALTER EXTENSION timescaledb UPDATE"`. שדרוג גרסה
ראשית של PostgreSQL (למשל 16 ל-17) הוא הסבת תיקיית נתונים נפרדת ולעולם לא מתבצע אוטומטית.

את העדכון אי אפשר לבטל: אחריו image (או חבילה) **ישן** יותר של TimescaleDB לא יכול לפתוח את המסד
(`could not access file "$libdir/timescaledb-<version>"`). קחו `pg_dump` לפני מעבר ל-image חדש; חזרה אחורה = שחזור
ה-dump הזה (ראו "גיבוי ושחזור") עם ה-image הישן, או השארת ה-image החדש.

### image מוכן (בלי בנייה במחשב)

כל דחיפה ל-`main` (ול-branches בשם `phase*`) מפרסמת image מוכן ב-GitHub Container Registry,
כך שמחשב שלא יכול להוריד חבילות npm/PyPI לא צריך לבנות:

```sh
# deploy/.env
NETMON_IMAGE=ghcr.io/shacharyekutiel/networkmonitor:latest   # או :phase2

docker login ghcr.io          # רק אם ה-package פרטי (משתמש + PAT עם read:packages)
docker compose -f deploy/docker-compose.yml pull netmon
docker compose -f deploy/docker-compose.yml up -d --no-build
```

התעודות הארגוניות שב-`deploy/certs` ממופות לתוך הקונטיינר (`/etc/netmon/certs`) ונטענות בזמן ריצה,
כך שגם ה-image המוכן עובד מאחורי חומת אש שבודקת SSL. לעדכון: שוב `pull netmon` ואז `up -d --no-build`.

### פתרון תקלות: שגיאות תעודה (`UNABLE_TO_GET_ISSUER_CERT_LOCALLY`,‏ `SELF_SIGNED_CERT_IN_CHAIN`)

הבנייה נעצרת עם `ERROR: TLS certificate check failed for https://registry.npmjs.org/`, או ש-`npm ci` /
`pip install` מדווחים `UNABLE_TO_GET_ISSUER_CERT_LOCALLY`,‏ `SELF_SIGNED_CERT_IN_CHAIN` או
`CERTIFICATE_VERIFY_FAILED`; או שבזמן ריצה התראות Telegram,‏ Webhook או דוא"ל, או חיישני HTTPS, נכשלים עם
`certificate verify failed`. המשמעות: הרשת שלכם בודקת HTTPS. חומת אש או proxy חותמים מחדש כל חיבור בתעודת
CA ראשית של הארגון, שהמחשב שלכם סומך עליה אבל קונטיינר לא.

הפתרון: ייצאו את תעודת ה-CA הראשית (ב-Windows: מנעול בדפדפן ← תעודה ← התעודה העליונה בשרשרת ← ייצוא בפורמט
*Base-64 encoded X.509*; או `certlm.msc` ← *Trusted Root Certification Authorities*), שמרו אותה בתיקייה
[`deploy/certs/`](certs/README.md) ובנו מחדש:

</div>

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

<div dir="rtl">

אין שום הגדרה נוספת: כל קובץ `*.crt` / `*.pem` / `*.cer` בתיקייה `deploy/certs/` (PEM או DER) מהימן על npm,‏
pip ו-apt בזמן הבנייה ועל האפליקציה בזמן ריצה. הקבצים לא נשמרים ב-git. הוראות ייצוא מפורטות:
[deploy/certs/README.md](certs/README.md).

התקנה ללא Docker: ב-Windows האפליקציה סומכת על מאגר התעודות של Windows, כך ש-CA שמחלקת ה-IT הפיצה עובד
בלי לעשות דבר; `install.ps1` סומך גם על קבצים בתיקייה `deploy\certs` (בניית הממשק, pip והשירות). בלינוקס
`install.sh` מוסיף את הקבצים מ-`deploy/certs` למאגר התעודות של המערכת.

### פתרון תקלות: `npm ci` נכשל בזמן `docker compose up --build`

שגיאות כמו `npm error Exit handler never called!`‏, `ECONNRESET` או `ETIMEDOUT` בשלב ה-`frontend`
אומרות שהמחשב לא מצליח להוריד חבילות מ-registry.npmjs.org בצורה יציבה (proxy ארגוני, חומת אש,
בדיקת SSL). עם בדיקת SSL ייתכן ש-`npm ci` ידפיס רק `Exit handler never called!`: בדקו קודם את סעיף שגיאות
התעודה למעלה. ה-Dockerfile כבר מנסה שוב עם הגדרות זהירות. אם זה עדיין נכשל:

- בדיקת גישה מתוך קונטיינר: `docker run --rm node:26-slim npm view react version`.
- מאחורי proxy: מגדירים אותו ב-Docker Desktop‏ (Settings → Resources → Proxies).
- עם mirror ארגוני של npm‏ (Nexus,‏ Artifactory):
  `docker compose -f deploy/docker-compose.yml build --build-arg NPM_REGISTRY=https://<mirror>/repository/npm/`
  ואחר כך `docker compose -f deploy/docker-compose.yml up -d`.
- או בונים פעם אחת ברשת אחרת (למשל נקודה חמה מהטלפון) וחוזרים לרשת הרגילה: רק הבנייה צריכה את npm.

