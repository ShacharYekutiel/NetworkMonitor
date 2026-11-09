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
| Database | TimescaleDB 2.31 / PostgreSQL 16 container | distro PostgreSQL ≥ 14 (+ TimescaleDB when available) | PostgreSQL 16 via winget (TimescaleDB attempted) |
| Data | volumes `netmon_netmon-data`, `netmon_netmon-db` | `/var/lib/netmon` + PostgreSQL | `%ProgramData%\NetworkMonitor` + PostgreSQL |
| Service | `restart: unless-stopped` | systemd `netmon.service` | Windows service `NetworkMonitor` (NSSM) |
| Logs | `docker compose -f deploy/docker-compose.yml logs -f netmon` | `journalctl -u netmon -f` | `%ProgramData%\NetworkMonitor\logs\` |
| Remove | `docker compose -f deploy/docker-compose.yml down` (add `-v` to delete data) | `sudo ./deploy/linux/uninstall.sh [--purge]` | `.\deploy\windows\uninstall.ps1 [-Purge]` |

If TimescaleDB is not available, the app runs in plain PostgreSQL mode automatically. That mode is fully supported
and is the normal case on Windows. `GET /api/health` reports `"timescale": true|false`.

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
without root), opens the HTTP port in firewalld/ufw, and prints the URL, the firewall lines for traps/syslog and
any port conflict (see [Traps and syslog](#traps-and-syslog)).
Options: `--port N`, `--no-timescale`, `--no-firewall`, `--wheel FILE`, `--source DIR`.

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
`-Port`, `-Wheel`, `-SourceDir`, `-SkipTimescale`, `-NssmPath`, `-IngestRemoteAddress`, `-DisableWindowsSnmpTrap`.
It also adds the firewall rules `NetworkMonitor-Traps` (UDP 162), `NetworkMonitor-Syslog-UDP` (UDP 514) and
`NetworkMonitor-Syslog-TCP` (TCP 514) (removed by `uninstall.ps1`; `-IngestRemoteAddress 10.0.0.0/8,LocalSubnet`
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

### Backup and restore

The data is in PostgreSQL. Back it up with `pg_dump` in custom format. The data directory holds `secret.key` (if it
is lost, users only have to log in again) and the DB password (Docker) or `netmon.env` (native).

**Docker**

```bash
C="docker compose -f deploy/docker-compose.yml"
# backup
$C exec -T db pg_dump -U netmon -Fc netmon > netmon-$(date +%F).dump
# restore (replaces the current database)
$C stop netmon
$C exec -T db psql -U netmon -d postgres -c 'DROP DATABASE IF EXISTS netmon WITH (FORCE)' -c 'CREATE DATABASE netmon'
$C exec -T db psql -U netmon -d netmon -c 'CREATE EXTENSION IF NOT EXISTS timescaledb' -c 'SELECT timescaledb_pre_restore()'
$C exec -T db pg_restore -U netmon -d netmon --no-owner < netmon-YYYY-MM-DD.dump
$C exec -T db psql -U netmon -d netmon -c 'SELECT timescaledb_post_restore()'
$C start netmon
# optional: the data volume (DB password, secret.key)
docker run --rm -v netmon_netmon-data:/data -v "$PWD":/backup busybox tar czf /backup/netmon-data.tgz -C /data .
```

**Linux (native)**

```bash
sudo runuser -u postgres -- pg_dump -Fc netmon > netmon-$(date +%F).dump
# restore
sudo systemctl stop netmon
sudo runuser -u postgres -- psql -c 'DROP DATABASE IF EXISTS netmon WITH (FORCE)' -c 'CREATE DATABASE netmon OWNER netmon'
# TimescaleDB only: run the next line, and the post_restore line after pg_restore
sudo runuser -u postgres -- psql -d netmon -c 'CREATE EXTENSION IF NOT EXISTS timescaledb' -c 'SELECT timescaledb_pre_restore()'
sudo runuser -u postgres -- pg_restore -d netmon --no-owner --role=netmon < netmon-YYYY-MM-DD.dump
sudo runuser -u postgres -- psql -d netmon -c 'SELECT timescaledb_post_restore()'
sudo systemctl start netmon
```

**Windows (native, elevated PowerShell)**

```powershell
$pg = 'C:\Program Files\PostgreSQL\16\bin'
# the password is in DATABASE_URL in C:\ProgramData\NetworkMonitor\netmon.env
$env:PGPASSWORD = '<password from netmon.env>'
& "$pg\pg_dump.exe" -h 127.0.0.1 -U netmon -Fc -f "netmon-$(Get-Date -Format yyyy-MM-dd).dump" netmon
# restore: Stop-Service NetworkMonitor, then drop and recreate the database and run pg_restore,
# as in the Linux steps (connect as postgres), then Start-Service NetworkMonitor
```

With TimescaleDB, `pg_dump` prints a warning about circular foreign keys in `continuous_agg`. That warning is
harmless: the custom-format dump restores cleanly with the steps above. Restore into the same major version of
TimescaleDB, or a newer one. A dump taken in plain PostgreSQL mode restores
into either mode.

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

- Check access from a container: `docker run --rm node:22-slim npm view react version`.
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
| מסד נתונים | קונטיינר TimescaleDB 2.31 / PostgreSQL 16 | PostgreSQL ≥ 14 מההפצה (+TimescaleDB אם זמין) | PostgreSQL 16 דרך winget (ניסיון להתקין TimescaleDB) |
| נתונים | הווליומים `netmon_netmon-data`, `netmon_netmon-db` | <span dir="ltr">`/var/lib/netmon`</span> + PostgreSQL | <span dir="ltr">`%ProgramData%\NetworkMonitor`</span> + PostgreSQL |
| שירות | <span dir="ltr">`restart: unless-stopped`</span> | שירות systemd בשם `netmon` | שירות Windows בשם `NetworkMonitor` (NSSM) |
| הסרה | <span dir="ltr">`docker compose -f deploy/docker-compose.yml down`</span> (עם `-v` גם הנתונים נמחקים) | <span dir="ltr">`sudo ./deploy/linux/uninstall.sh [--purge]`</span> | <span dir="ltr">`.\deploy\windows\uninstall.ps1 [-Purge]`</span> |

כש-TimescaleDB לא זמין, המערכת עוברת אוטומטית ל-PostgreSQL רגיל. מצב זה נתמך במלואו, והוא המצב הרגיל ב-Windows.
השדה `timescale` בתשובה של `/api/health` מראה באיזה מצב המערכת פועלת.

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

</div>

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

- בדיקת גישה מתוך קונטיינר: `docker run --rm node:22-slim npm view react version`.
- מאחורי proxy: מגדירים אותו ב-Docker Desktop‏ (Settings → Resources → Proxies).
- עם mirror ארגוני של npm‏ (Nexus,‏ Artifactory):
  `docker compose -f deploy/docker-compose.yml build --build-arg NPM_REGISTRY=https://<mirror>/repository/npm/`
  ואחר כך `docker compose -f deploy/docker-compose.yml up -d`.
- או בונים פעם אחת ברשת אחרת (למשל נקודה חמה מהטלפון) וחוזרים לרשת הרגילה: רק הבנייה צריכה את npm.

