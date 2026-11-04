# NetworkMonitor — Deployment

[English](#english) · [עברית](#עברית)

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
`/var/lib/netmon/netmon.env` (mode 0600). It installs `netmon.service` with `AmbientCapabilities=CAP_NET_RAW`,
opens the port in firewalld/ufw, and prints the URL.
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
`-Port`, `-Wheel`, `-SourceDir`, `-SkipTimescale`, `-NssmPath`.

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

### Files

| Path | Purpose |
|---|---|
| `../Dockerfile` | Multi-stage build: Node builds the SPA, Python installs `netmon`, and the slim runtime runs as uid 10001 |
| `docker-compose.yml` | Production stack (`init` + `db` + `netmon`) |
| `docker-compose.dev.yml` | Development DB only (localhost:5432, `netmon`/`netmon`) |
| `.env.example` | Optional compose overrides |
| `linux/install.sh`, `linux/uninstall.sh`, `linux/netmon.service` | Native Linux |
| `windows/install.ps1`, `windows/uninstall.ps1` | Native Windows |

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
`netmon.env` עם סיסמה אקראית. בסוף הוא מתקין את השירות `netmon.service` עם ההרשאה `CAP_NET_RAW`, מפעיל אותו
ומדפיס את הכתובת.

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
