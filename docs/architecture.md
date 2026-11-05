# NetworkMonitor — Architecture (Phase 1 / MVP + Phase 2a + Phase 2b-1)

Phase 2a (auto-discovery, device templates, `snmp_custom`/`snmp_disk`, Telegram) is described in
[§ 9](#9-phase-2a-discovery-templates-custom-snmp-telegram); items elsewhere marked **[2a]** belong to it.
Phase 2b-1 (dependencies, maintenance windows, acknowledge, escalation, fingerprinting, HPE templates,
topology) is described in [§ 10](#10-phase-2b-1-dependencies-maintenance-acknowledge-escalation-fingerprinting-hpe-topology).

NetworkMonitor is a license-free, PRTG-like network monitor. One Python process serves the REST API, the
WebSocket, the built React SPA, and runs the poller and the alert engine. PostgreSQL holds everything, and
TimescaleDB is used when it is available.

Related contracts: [data-model.md](data-model.md), [api.md](api.md), [conventions.md](conventions.md).

## 1. Components

```
                 Browser (React SPA, he/en, RTL/LTR)
                    |  HTTPS/HTTP  /api/*  (JSON, cookie auth)
                    |  WebSocket   /ws     (live updates)
                    v
+-----------------------------------------------------------------+
| netmon process (uvicorn, 1 worker, asyncio event loop)           |
|                                                                  |
|  FastAPI app ── routers /api/*  ── services (status, history)    |
|      |            /ws  ───────────── RealtimeHub (fan-out)       |
|      |            /*   ───────────── SPA static files            |
|      |                                                           |
|  Poller (Scheduler + check plugins) ──> ResultProcessor ──┐      |
|      ping | snmp_* | http | tcp_port | dns                |      |
|                                                          v      |
|  AlertEngine <── status-change events ── EventWriter ─> Hub      |
|      email (aiosmtplib) | webhook (httpx)                        |
|                                                                  |
|  Maintenance tasks: retention (fallback mode), event retention   |
+-----------------------------------------------------------------+
                    |  asyncpg (SQLAlchemy 2 async)
                    v
        PostgreSQL >= 14  (+ TimescaleDB extension when available)
```

| Component | Module | Responsibility |
|---|---|---|
| API | `netmon.api.*` | REST endpoints per [api.md](api.md); auth dependency; error envelope |
| RealtimeHub | `netmon.realtime.hub` | Keeps the set of connected WebSockets and broadcasts JSON messages. In-memory only. |
| Scheduler | `netmon.poller.scheduler` | Holds every enabled sensor in a min-heap keyed by `next_run`. Bounded concurrency through `asyncio.Semaphore(NETMON_MAX_CONCURRENT_CHECKS)`. |
| Check plugins | `netmon.poller.checks.<type>` | One module per sensor type: `async def run(ctx) -> CheckResult`. A plugin never blocks the loop. Blocking libraries go through `asyncio.to_thread`. |
| ResultProcessor | `netmon.poller.processor` | Evaluates thresholds, writes `measurements`, updates `sensors` / `sensor_channels`, and emits a status change when the status differs. |
| EventWriter | `netmon.services.events` | Inserts into `events`, publishes `event.created`, and recomputes the device/group rollup → `device.status` / `group.status` WS messages. |
| AlertEngine | `netmon.alerts.engine` | Matches status-change events to `alert_rules`, applies `delay_s`, and sends to notification channels. |
| Maintenance | `netmon.services.retention` | Runs hourly. Deletes old measurements (fallback mode, or Timescale without a working retention policy, see §6) and old events. |

### Libraries (fixed)
FastAPI, uvicorn, SQLAlchemy 2 (async) + asyncpg, Alembic, pydantic v2 + pydantic-settings, PyJWT, argon2-cffi,
pysnmp >= 7 (lextudio), icmplib, httpx, dnspython, aiosmtplib, **[2b-1]** tzdata (IANA zones on Windows/slim images).
Frontend: React 18, TypeScript, Vite, Tailwind CSS, React Router, TanStack Query, ECharts, i18next.

## 2. Process model

- **One process, one uvicorn worker.** The scheduler, the alert engine and the WS hub live in memory, so running
  more than one worker is **unsupported**. At startup the app takes a PostgreSQL advisory lock
  (`pg_try_advisory_lock(727001)`). When the lock is already held, the second instance logs an error and exits
  with code 3.
- Startup sequence (FastAPI `lifespan`):
  1. Load config (env → `${NETMON_DATA_DIR}/netmon.env` → defaults).
  2. Ensure `NETMON_DATA_DIR` exists. Load or generate `secret.key` (§5).
  3. Connect to the DB (retry for 60 s with backoff, so DB containers have time to start). Take the advisory lock.
  4. Run Alembic migrations (`upgrade head`).
  5. Timescale bootstrap (§6).
  6. Set every enabled `up` sensor's `status` to `unknown` **only if** its `last_check_at` is older than 3× its
     interval. Otherwise keep the status. `down`/`warning` sensors keep their status, so a still-failing
     sensor does not alert DOWN again and a recovery still alerts UP.
  7. Start the scheduler. Each sensor's first run gets a random jitter in `[0, min(interval_s, 30))` seconds.
  8. Start the alert engine, the maintenance loop and the WS heartbeat.
- **Lock watchdog:** every 30 s the app checks that it still holds the advisory lock (a PostgreSQL restart
  drops it). It re-acquires it. When another instance has taken it meanwhile, it logs a critical error and
  exits with code 3. `netmon serve` checks the lock before starting and exits with code 3 and a one-line
  message when another instance holds it.
- **Proxy headers:** uvicorn does not rewrite the client address. With `NETMON_TRUST_PROXY=1` the app reads
  `X-Forwarded-Proto`/`X-Forwarded-Host`, and the login throttle uses the **rightmost** `X-Forwarded-For`
  entry.
- Shutdown: stop the scheduler (in-flight checks may run for up to 5 s), cancel pending delayed alerts, close
  the WebSockets with code 1001, and release the lock.
- **Config changes reach the scheduler directly.** The API layer calls `scheduler.upsert(sensor_id)` /
  `scheduler.remove(sensor_id)` after it commits a create, update, delete, pause or resume. A device or group
  change that affects sensors (enable/disable, SNMP credentials, host, delete) calls `scheduler.reload_device(id)`.
- **Check flow.** Scheduler → acquire semaphore → plugin `run()` with a hard timeout of `min(interval_s, 120)` s →
  ResultProcessor → reschedule `next_run = previous_scheduled + interval_s`. When the check is late, the next run
  is `now + interval_s`.
- **"Run now" / "test"** (`POST /api/sensors/{id}/run`, `POST /api/sensors/test`) call the same plugin inline,
  inside the request. They use the same semaphore. `run` persists its result. `test` does not.
- Ping: icmplib `async_ping(privileged=False)`. On `SocketPermissionError` it retries with `privileged=True`.
  If both fail, it falls back to the system `ping` binary through `asyncio.create_subprocess_exec` and parses the
  output (Linux/Windows formats).
- SNMP: pysnmp asyncio API (`pysnmp.hlapi.v3arch.asyncio`), one `SnmpEngine` per process.
- The poller holds no global DB transaction. Each result is written in its own short transaction.

## 3. Repository layout

```
/
├── README.md
├── Dockerfile                    # multi-stage: node build frontend → python runtime (devops)
├── .dockerignore
├── .github/workflows/ci.yml
├── docs/                         # architecture.md, data-model.md, api.md, conventions.md
├── backend/
│   ├── pyproject.toml            # package `netmon`, console script `netmon = netmon.cli:main`
│   ├── alembic.ini
│   ├── netmon/
│   │   ├── __init__.py           # __version__
│   │   ├── __main__.py           # python -m netmon  → cli.main()
│   │   ├── cli.py                # serve | migrate | reset-password
│   │   ├── config.py             # pydantic-settings Settings
│   │   ├── secrets.py            # secret.key load/generate
│   │   ├── app.py                # create_app(), lifespan, SPA mount, error handlers
│   │   ├── db/                   # engine/session, timescale.py (bootstrap + capability flag)
│   │   ├── models/               # SQLAlchemy ORM models (one file per table group)
│   │   ├── schemas/              # pydantic request/response models (mirror api.md)
│   │   ├── api/                  # routers: setup, auth, users, groups, tree, devices, sensors,
│   │   │                         #          sensor_types, events, dashboard, notification_channels,
│   │   │                         #          alert_rules, settings, health, ws
│   │   ├── services/             # status rollup, history queries, events, retention
│   │   ├── poller/
│   │   │   ├── scheduler.py, processor.py, types.py   # CheckContext, CheckResult
│   │   │   ├── sensor_types.py   # channel definitions + config schemas (source of truth in code)
│   │   │   ├── snmp_client.py    # get/walk helpers, credential resolution
│   │   │   └── checks/           # ping.py snmp_system.py snmp_cpu.py snmp_memory.py
│   │   │                         # snmp_interface.py http.py tcp_port.py dns.py
│   │   │                         # [2a] snmp_custom.py snmp_disk.py
│   │   ├── alerts/               # engine.py, email.py, webhook.py, templates (he/en), [2a] telegram.py
│   │   ├── discovery/            # [2a] targets.py, probe.py, classify.py, runner.py, scheduler.py, importer.py
│   │   ├── templates/            # [2a] builtin.py (device templates), apply.py
│   │   ├── realtime/hub.py
│   │   ├── migrations/           # alembic env.py + versions/
│   │   └── static/               # built SPA copied here for packaging (gitignored)
│   └── tests/                    # pytest unit + API tests (backend dev)
├── frontend/
│   ├── package.json, vite.config.ts, tailwind.config.ts, tsconfig.json, index.html
│   └── src/
│       ├── main.tsx, App.tsx, router.tsx
│       ├── api/                  # client.ts (fetch wrapper), types.ts (mirror of api.md), ws.ts
│       ├── i18n/                 # index.ts, locales/en.json, locales/he.json
│       ├── components/           # StatusBadge, Tree, Charts, forms...
│       ├── pages/                # Setup, Login, Dashboard, Group, Device, Sensor, Events,
│       │                         # Alerts (rules+channels), Users, Settings
│       └── hooks/
├── deploy/
│   ├── docker-compose.yml        # timescaledb + netmon
│   ├── docker-compose.dev.yml    # DB only, for local development
│   ├── linux/install.sh, linux/netmon.service, linux/uninstall.sh
│   └── windows/install.ps1, windows/uninstall.ps1
└── tests/
    ├── e2e/                      # Playwright (QA)
    ├── integration/              # pytest against a running system (QA)
    └── sim/                      # snmpsim data, HTTP target, etc.
```

## 4. Configuration

Precedence: real environment variables → `${NETMON_DATA_DIR}/netmon.env` (KEY=VALUE lines, written by the
native installers) → defaults. **The end user never edits any of these.** Installers and compose set them.

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql://netmon:netmon@localhost:5432/netmon` | `postgresql://` or `postgresql+asyncpg://`. The app normalizes it to asyncpg. |
| `NETMON_DATA_DIR` | Docker: `/data`. Linux: `/var/lib/netmon`. Windows: `%ProgramData%\NetworkMonitor`. Dev: set explicitly, e.g. `./.devdata`. | Holds `secret.key`, `netmon.env` and logs. When unset, the app resolves the OS default above, and falls back to `./data` if the default is not writable. |
| `NETMON_DB_PASSWORD_FILE` | unset | When set, the app reads the password from this file and uses it in place of the password in `DATABASE_URL` (Docker) |
| `NETMON_HOST` | `0.0.0.0` | Bind address |
| `NETMON_PORT` | `8080` | HTTP port (API + SPA + WS) |
| `NETMON_LOG_LEVEL` | `info` | `debug`/`info`/`warning`/`error` |
| `NETMON_MAX_CONCURRENT_CHECKS` | `100` | Poller semaphore size |
| `NETMON_FRONTEND_DIST` | `<package>/netmon/static`. When that has no `index.html`: `<repo>/frontend/dist`. | SPA directory |
| `NETMON_COOKIE_SECURE` | `auto` | `auto` = Secure flag set when the request scheme is https (honours `X-Forwarded-Proto` when `NETMON_TRUST_PROXY=1`). Also accepts `true`/`false`. |
| `NETMON_TRUST_PROXY` | `0` | Trust `X-Forwarded-*` headers |
| `NETMON_DEV` | `0` | `1` enables uvicorn reload and verbose errors. It never disables auth. |
| `NETMON_DISABLE_TIMESCALE` | `0` | `1` skips the Timescale bootstrap and forces fallback mode even when the extension is available (used by tests and CI to cover fallback mode on a Timescale server). |
| **[2a]** `NETMON_TELEGRAM_API_BASE` | `https://api.telegram.org` | Telegram Bot API base URL, no trailing slash. Tests point it at the mock (`http://127.0.0.1:18081`). |
| **[2a]** `NETMON_DISCOVERY_CONCURRENCY` | `64` | Hosts probed in parallel by a discovery scan (1–256) |
| **[2a]** `NETMON_DISCOVERY_RATE` | `100` | Max new hosts started per second by a scan (1–1000) |
| **[2b-1]** `TZ` | unset | Only used as the default of the `timezone` setting when it is a valid IANA name |
| **[2b-1]** `NETMON_TEST_MODE` | `0` | `1` enables `PUT /api/test/clock` (QA only; never set by installers/compose) |

Runtime settings (SMTP, retention, default interval, default language) live in the DB `settings` table and are
edited in the UI. They are **not** env vars.

## 5. Secrets and first run

- `${NETMON_DATA_DIR}/secret.key`: 64 random bytes, base64url. On first start the app creates it with
  `secrets.token_urlsafe(64)` and file mode `0600` (on Windows, the ACL is inherited from ProgramData, and the
  installer restricts it to SYSTEM + Administrators). The app uses it as the HS256 JWT signing key. If the file
  is lost, every session is invalidated, and nothing else breaks.
- The DB password: Docker compose generates it on first `up` (§8). Native installers generate it at install time
  and write `DATABASE_URL` to `netmon.env`.
- First-run wizard: while the `users` table is empty, `GET /api/setup/status` returns `{"setup_required": true}`
  and the SPA shows the wizard. `POST /api/setup` creates the first admin, the `Default` group and default
  settings, then logs the user in. After that, the setup endpoint returns `409 setup_already_done`.
- Recovery: `netmon reset-password <username>` prints a new random password (CLI on the server).

## 6. TimescaleDB with plain PostgreSQL fallback

Minimum version is **PostgreSQL 14**, because the fallback uses `date_bin`. The Alembic migrations create the
schema in **plain SQL only**. `measurements` is created as an ordinary table. Timescale-specific work happens in
`netmon.db.timescale.bootstrap()` after the migrations:

1. `SELECT 1 FROM pg_available_extensions WHERE name='timescaledb'`. If it is missing → **fallback mode**.
2. `CREATE EXTENSION IF NOT EXISTS timescaledb` inside a savepoint. On error (permission denied, or the library
   is not preloaded) → log a warning → **fallback mode**.
3. `SELECT create_hypertable('measurements', 'time', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE, migrate_data => TRUE)`.
4. Retention: `remove_retention_policy('measurements', if_exists => TRUE)`, then
   `add_retention_policy('measurements', INTERVAL '<retention_days> days')`. The app re-applies this when
   `retention_days` changes through `PUT /api/settings`. If the policy API fails (e.g. an Apache-only
   TimescaleDB build), the app remembers that and the hourly maintenance loop prunes instead:
   `drop_chunks('measurements', older_than => ...)`, then the batched `DELETE` for the rest (or for
   everything when `drop_chunks` fails too).
5. Optional: enable compression on chunks older than 7 days (`segmentby = 'sensor_id, channel'`). If that fails,
   the app ignores it.

The result is stored in the process-wide flag `timescale_enabled: bool` and reported by `/api/health`.

| Concern | Timescale mode | Fallback mode |
|---|---|---|
| Storage | hypertable | plain table + index `(sensor_id, channel, time DESC)` + BRIN on `time` |
| Retention | Timescale retention job | Maintenance loop runs hourly: `DELETE ... WHERE time < now() - retention` in batches of 50 000 rows |
| Bucketing | `time_bucket(INTERVAL, time)` | `date_bin(INTERVAL, time, TIMESTAMPTZ '2000-01-01')` |

The history service (`netmon.services.history`) selects the bucket function by the flag. Both modes return the
same shape. Event retention (`event_retention_days`) always uses the maintenance loop.

## 7. Serving the frontend (SPA)

- `npm run build` outputs to `frontend/dist`. Packaging (the Dockerfile, or the installers) copies it to
  `backend/netmon/static/` before building or installing the wheel.
- Routing order in `create_app()`:
  1. `/api/*` routers. An unknown `/api/...` path returns the JSON error `404 not_found`. It never returns
     `index.html`.
  2. `/ws` WebSocket.
  3. `/assets/*` → `StaticFiles(dist/assets)` with `Cache-Control: public, max-age=31536000, immutable`.
  4. A catch-all `GET /{path:path}`: if `dist/<path>` is an existing file (favicon, locales, ...), serve it.
     Otherwise serve `dist/index.html` with `Cache-Control: no-cache`. This is the SPA fallback that client-side
     routing depends on.
- If the dist directory has no `index.html`, `/` returns a small HTML page: "Frontend not built — run
  `npm run build` in frontend/". The API still works.
- The SPA calls the API with relative URLs (`/api/...`, `ws(s)://<host>/ws`), so it needs no build-time API
  configuration.

## 8. Deployment modes

### Docker compose (`deploy/docker-compose.yml`)
- Services:
  - `db`: `timescale/timescaledb:2.x-pg16`, volume `netmon-db`, not published to the host.
  - `netmon`: built from the root `Dockerfile`, publishes `8080:8080`, volume `netmon-data:/data`.
  - `sysctls: net.ipv4.ping_group_range: "0 2147483647"`, so unprivileged ICMP works.
- Zero config DB password: a one-shot `init` service (busybox) writes a random password to
  `/data/db_password` on the shared volume if the file is missing. `db` reads it through
  `POSTGRES_PASSWORD_FILE`. `netmon` reads it through `NETMON_DB_PASSWORD_FILE`, which, when set, replaces the
  password inside `DATABASE_URL` (default in the image:
  `DATABASE_URL=postgresql://netmon@db:5432/netmon`, `NETMON_DATA_DIR=/data`).
- The image entrypoint is `netmon serve`. Migrations run automatically.

### Native Linux (`deploy/linux/install.sh`, run as root)
1. Install the prerequisites: Python ≥ 3.12, PostgreSQL ≥ 14 (distribution packages). The script also tries the
   TimescaleDB packages. If that fails it continues (fallback mode) and adds `timescaledb` to
   `shared_preload_libraries` only when it is installed.
2. Create the system user `netmon`, `/opt/netmon` (venv), and `/var/lib/netmon` (data, 0750).
3. Create the DB role and database `netmon` with a random password. Write
   `/var/lib/netmon/netmon.env` (`DATABASE_URL=...`, 0600).
4. `pip install` the wheel (with the frontend bundled) into `/opt/netmon/venv`.
5. Install `netmon.service` (systemd): `ExecStart=/opt/netmon/venv/bin/netmon serve`, `User=netmon`,
   `Environment=NETMON_DATA_DIR=/var/lib/netmon`, `AmbientCapabilities=CAP_NET_RAW` (privileged ping
   fallback), `Restart=on-failure`. Then `systemctl enable --now netmon`.
6. Print `http://<host>:8080`.

### Native Windows (`deploy/windows/install.ps1`, elevated PowerShell)
1. Install Python 3.12 and PostgreSQL 16 through `winget` if they are missing. TimescaleDB is normally absent on
   Windows → fallback mode, which is expected and fully supported.
2. Create `C:\Program Files\NetworkMonitor\` (venv) and `%ProgramData%\NetworkMonitor\` (data).
3. Create the DB role/database with a random password and write `%ProgramData%\NetworkMonitor\netmon.env`.
4. Install the wheel into the venv. Register the service `NetworkMonitor` with NSSM (bundled):
   `netmon.exe serve`, env `NETMON_DATA_DIR`, start Automatic, runs as LocalSystem (privileged ICMP works).
5. Add the firewall rule for TCP 8080 inbound. Start the service and print the URL.

All code must be portable: `pathlib` everywhere, no `fork`/`signal.SIGHUP` assumptions, and on Windows the
default asyncio loop (Proactor) must work. Do not depend on uvloop (it is optional on Linux only).

## 9. Phase 2a: discovery, templates, custom SNMP, Telegram

Contract details: [data-model.md § Phase 2a](data-model.md#2a-phase-2a-additions) (tables, classification,
templates, migration `0003`) and [api.md § Phase 2a](api.md#2a-phase-2a-endpoints) (endpoints, WS, errors).
Backend, frontend and QA can implement in parallel against those two files.

### 9.1 Components

| Component | Module | Responsibility |
|---|---|---|
| Target parser | `netmon.discovery.targets` | Parses/validates targets, expands to a sorted, de-duplicated host list, enforces the 4096 cap. Pure functions (unit-testable). |
| Prober | `netmon.discovery.probe` | Per host: ping, TCP connect, SNMP (credential loop + sysDescr/sysObjectID/sysName GET + one subtree GETNEXT + FreeBSD `ifDescr` walk), reverse DNS, best-effort MAC. |
| Classifier | `netmon.discovery.classify` | Pure function `classify(sys_object_id, sys_descr, sys_name, probes, pf, open_ports, snmp_ok) -> (device_class, vendor)` implementing the ordered rule table, plus `suggest_template(result) -> template_id`. |
| ScanRunner | `netmon.discovery.runner` | Owns the single running scan (an `asyncio.Task` on the runtime). Writes results, counters, known hosts, events; publishes `discovery.*` WS messages; prunes old scans; sends new-host notifications. |
| DiscoveryScheduler | `netmon.discovery.scheduler` | 30 s loop starting due profile scans. Started in the lifespan after the alert engine. |
| Importer | `netmon.discovery.importer` | Creates devices from results and applies templates. |
| Templates | `netmon.templates.builtin`, `netmon.templates.apply` | Read-only template data; `apply_template(session, rt, device, template, lang) -> ApplyTemplateResult`, shared by the apply endpoint and the importer. |
| Telegram | `netmon.alerts.telegram` | `send_telegram(config, text, parse_mode) -> None`, raises `TelegramError`. `send_to_channel` gains the `telegram` branch; `templates.py` gains `telegram_text(payload, lang, parse_mode)` and the discovery payload texts. |

### 9.2 Scan execution

- **One running scan at a time**, enforced in memory (the runner) and in the DB (partial unique index).
  `POST` while one runs → `409 scan_running`.
- The scan task builds the credential list, writes the `discovery_scans` row (`running`), then feeds hosts
  through an `asyncio.Semaphore(NETMON_DISCOVERY_CONCURRENCY)`, starting at most `NETMON_DISCOVERY_RATE` hosts
  per second. It uses **its own semaphore**, not the poller's, so monitoring keeps running; the SNMP engine
  is shared.
- Per host: ping (icmplib, `count=1`, the same privileged/unprivileged/system-binary
  fallback as the ping sensor), the TCP probes (all ports concurrently, open = connect succeeded; refused =
  closed) and the SNMP stage run **concurrently**.
  - **SNMP budget:** the SNMP stage (credential loop + identity GET + subtree GETNEXT + FreeBSD `ifDescr` walk)
    has a **25 s** budget across all credential attempts. When it runs out, the stage stops: if a credential
    already answered, the identity values gathered so far are kept (subtree probes / `pf` that did not finish
    count as not present); otherwise `snmp_ok = false`.
  - **Partial results are always kept.** The SNMP budget never discards the ping/TCP results: a host that
    answered ping or has an open port is always recorded as found. (An outer safety timeout of 30 s per host
    may exist for bugs, but it must also keep the ping/TCP results gathered so far.)
  - **TCP connect cap:** all TCP probes of a scan share one global semaphore of **256** concurrent connects
    (on top of the host concurrency), so a default scan (64 hosts × 11 ports) never opens more than 256
    sockets at once.
  - **File descriptors:** at startup, on POSIX, the process raises its soft `RLIMIT_NOFILE` to the hard limit
    (capped at 65536; best effort, logged at debug when it fails). Windows needs nothing (the Proactor loop has
    no `select()` limit; in dev reload mode uvicorn uses a selector loop, and the 256 cap keeps it under 512).
  - Reverse DNS (dnspython async resolver, PTR,
  lifetime 2 s, system resolver) runs only for found hosts. MAC: on Linux read `/proc/net/arp` after the
  probes (entry with flags `0x2`); elsewhere `null`. In Docker the container sees only its own L2 neighbours,
  so MACs are usually `null`; this is expected.
- Each found host is classified, inserted, and published as `discovery.result`. Progress is flushed/published
  at most once per second.
- **Cancel** sets a flag and cancels in-flight host tasks; found results stay; status `cancelled`. Cancelled
  scans also update known hosts. `POST .../cancel` waits for the scan task at most **5 s** and always answers
  `200` with `status: "cancelled"`: if the task has not ended by then, the endpoint writes the final state
  itself (`status = 'cancelled'`, `finished_at`) and the task finishes in the background (its later finish
  must not overwrite the status).
- **Hostname targets resolving to an address already found** (through another target): the result row is
  de-duplicated per `(scan_id, ip)` — the **first writer wins**, so `hostname` may be the PTR name of the
  IP-target row rather than the target name. The host still counts once in `scanned_hosts` per target.
- **Failure** (unexpected exception) → `failed` with the error text, published as `discovery.finished`. A
  scheduled scan that fails writes a `discovery` event.
- After finishing: upsert known hosts, compute `new_hosts`, write the `discovery` event / send notifications for
  scheduled profile scans (see api.md), prune to the newest 20 scans.
- Startup: `running` scans left from a crash become `failed` ("Interrupted by restart"). Shutdown: cancel the
  running scan (it ends `cancelled`) before closing the DB.

### 9.3 Templates and apply
- **Serialized per device:** apply-template and the import of a device hold a per-device `asyncio.Lock`
  (keyed by device id) from reading the existing sensors until the commit, so concurrent applies (double
  submit, apply during an import) never create duplicate sensors; the second one reports `exists`.
- Templates are code (`netmon/templates/builtin.py`), never stored in the DB, and exposed read-only by
  `GET /api/device-templates`. Adding a template = a code change.
- `apply_template` is idempotent: identities per [data-model.md](data-model.md#idempotency-apply-adds-only-what-is-missing).
  Probes call the same plugins as `POST /api/sensors/test` (`CheckContext` with `sensor_id=None`).
- SNMP enumeration for the `snmp_interfaces` rule reuses the code behind
  `GET /api/devices/{id}/snmp/interfaces` (move it to `netmon.services.snmp_enum` so the API and the templates
  share it). `GET /api/devices/{id}/snmp/storage` lives there too.

### 9.4 Security
- **All discovery endpoints are admin-only**; `discovery.*` WS messages go to admin sockets only.
  Templates are readable by viewers (no secrets in them). Apply-template is admin.
- The scanner touches **only** the hosts expanded from the request's targets (no ARP sweep, no neighbour or
  route discovery, no following of hostnames found in SNMP). It never sends anything but ICMP echo,
  TCP connect (closed immediately, no payload), SNMP GET/GETNEXT/GETBULK of the listed OIDs, and PTR lookups.
- Limits: ≤ 4096 hosts per scan, ≤ 64 target strings, one running scan, `NETMON_DISCOVERY_CONCURRENCY` and
  `NETMON_DISCOVERY_RATE`, ≤ 9 SNMP credential attempts per host with `retries = 0` (25 s SNMP budget), ≤ 32
  TCP ports, ≤ 256 concurrent TCP connects per process. Multicast, `0.0.0.0/8` and `240.0.0.0/4` targets are
  rejected, and so are IPv6 scope ids (`fe80::1%eth0`) and IPv4-mapped IPv6 (`::ffff:a.b.c.d`), both as
  literal targets and as the result of hostname resolution.
- Unexpected errors never echo internals: import item errors use the fixed message `Internal error`
  (SQLAlchemy errors embed statement parameters, which can include SNMP secrets); the details go only to the
  log, which must not include statement parameters either (`hide_parameters=True` on the engine).
- Secrets: discovery extra communities/v3 keys and the Telegram bot token are write-only in the API, never
  logged (Telegram error texts are scrubbed of the token; `httpx`/`httpcore` loggers at WARNING), and the
  per-scan credential snapshot (`discovery_scans.credentials`) is never serialized. It is deleted together
  with the scan by retention.
- Scheduled discovery is **off by default** (`schedule_enabled = false`).

### 9.5 Frontend (high level)
- **Nav:** "Discovery" (admin only), route `/discovery`; "Templates" (all users), route `/templates`.
- **`/discovery`**: two tabs.
  - *Scans*: "New scan" form (targets as one-per-line textarea, target group select, options: ping / SNMP /
    TCP / DNS toggles, SNMP port, extra communities (password-type inputs), extra v3 rows, TCP ports list with
    the default list prefilled, timeouts under "Advanced"); a "Save as profile" checkbox that creates a profile
    first and then calls `/run`. Below, the scans table (status badge, started, trigger, found/new counts,
    delete).
  - *Profiles*: table + create/edit drawer (same fields + schedule: Off / Daily / Weekly / Every N hours,
    "Notify on new hosts" + channel multi-select), "Run now" button.
- **`/discovery/scans/:id`**: header with live progress bar (`scanned_hosts / total_hosts`, found, new) from
  `discovery.progress`, a Cancel button while running, falling back to polling `GET /scans/{id}` every 2 s
  when the WS is not open. Results table (rows appended live from `discovery.result`): checkbox, IP, name
  (sys_name / hostname), class (icon + i18n `deviceClass.<class>`), vendor, reachability chips (ICMP, SNMP
  v2c/v3, open ports), "New" badge, "Exists" link to the device when `existing_device_id` (row not
  selectable), per-row template select (default `suggested_template`, options from `/api/device-templates`,
  localized names), editable name. Filters: only new, hide existing. Bulk bar: target group select (default
  the scan's group), "Import selected (N)" → `POST .../import` → summary dialog (created with skipped sensors,
  errors translated by `errors.<code>`).
- **`/templates`**: read-only cards/table: name, description, match summary, entries list (sensor
  type/name; rules rendered as text, e.g. "Interfaces that are up (max 16)").
- **Device page**: admin action "Apply template" → modal (plain template select, localized names and
  descriptions), submit → result dialog (created / skipped with reasons, i18n
  `templates.skip.<reason>`).
- **Add sensor**: `snmp_custom` and `snmp_disk` appear in the type list; their forms are driven by
  `config_fields` (new `float` field type → number input with `step="any"`). `snmp_custom` shows `percent_of_oid`
  only for gauge, `expected`/`match` only for string. `snmp_disk` has a "Pick from device" button using
  `GET /api/devices/{id}/snmp/storage`.
- **Notification channel form**: type `telegram` with fields bot token (password input; placeholder "stored"
  when `bot_token_set`), chat id (text, hint "-100… for groups/channels or @channelname"), parse mode
  (HTML / plain), silent toggle; Test button (`errors.telegram_error` shows the server `message`).
- **Events page**: kind filter gains `discovery`.
- New i18n keys (en + he): `nav.discovery`, `nav.templates`, `deviceClass.*`, `discovery.*`, `templates.*`,
  `sensorType.snmp_custom` ("Custom SNMP OID" / "OID SNMP מותאם"), `sensorType.snmp_disk` ("Disk usage" /
  "שימוש בדיסק"), `sensorConfig.snmp_custom.*`, `sensorConfig.snmp_disk.*`, `channel.value`,
  `channel.disk_used_pct`, `channel.disk_used_bytes`, `channel.disk_free_bytes`, `channel.disk_total_bytes`,
  `errors.scan_running`, `errors.scan_not_running`, `errors.too_many_hosts`, `errors.telegram_error`,
  `errors.device_exists`, `notificationType.telegram`.

### 9.6 QA simulators and test setup (`tests/sim/`)
- **SNMP agents:** one `snmpsim-command-responder` **process per simulated device**, each with its own data
  directory and `--agent-udpv4-endpoint=<ip>:1161` (unprivileged port). Linux routes all of `127.0.0.0/8` to
  `lo`, so distinct loopback IPs work without setup (CI is Linux; on macOS add aliases, Windows is not
  supported for these tests). Scans use `options.snmp_port = 1161`.

  | IP | File | Community | Must contain |
  |---|---|---|---|
  | 127.0.0.21 | `tests/sim/discovery/opnsense/public.snmprec` | public | sysDescr `FreeBSD OPNsense.localdomain 14.1-RELEASE-p5 FreeBSD 14.1-RELEASE-p5 stable/24.7-n267758-abc amd64`, sysObjectID `1.3.6.1.4.1.8072.3.2.8`, sysName, ifTable/ifXTable with `igb0` (up), `igb1` (up), `lo0` (up, type 24), `pflog0` (up), `enc0` (down); hrProcessorLoad rows; hrStorage rows for RAM and `/` (fixed disk) |
  | 127.0.0.22 | `tests/sim/discovery/unifi_ap/public.snmprec` | public | sysObjectID `1.3.6.1.4.1.8072.3.2.10`, sysDescr `Linux U6-Lite 4.4.153 #1 SMP ... mips`, sysName `U6-Lite-Office`, unifiVap rows for indexes 1 and 2: `.1.3.6.1.4.1.41112.1.6.1.2.1.6.N` ESSID (`HomeWiFi`), `.8.N` NumStations (Gauge/Integer, e.g. 5 and 12), `.9.N` radio (`ng`, `na`); ifTable with `eth0` (up), `br0` (up), `ath0` (up), `lo` (up, type 24); hrProcessorLoad, hrStorage RAM |
  | 127.0.0.23 | `tests/sim/discovery/printer/printers.snmprec` | **printers** (only reachable through `extra_communities`) | sysObjectID `1.3.6.1.4.1.2435.2.3.9.1`, sysDescr `Brother NC-8300h, Firmware Ver.1.10`, Printer-MIB: `1.3.6.1.2.1.43.10.2.1.4.1.1` = 12345, `43.11.1.1.6.1.1` = `Black Toner Cartridge`, `.8.1.1` = 100, `.9.1.1` = 40, `.6.1.2` = `Drum Unit`, `.8.1.2` = 100, `.9.1.2` = -3 |
  | 127.0.0.24 | `tests/sim/discovery/switch/public.snmprec` | public | sysObjectID `1.3.6.1.4.1.9.1.1208`, sysDescr `Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), Version 15.2(7)E8`, 4 interfaces, 2 up; no HOST-RESOURCES (so CPU/memory probes fail → `probe_failed`) |
  | 127.0.0.25 | — (no SNMP) | — | A TCP listener on `127.0.0.25:8080` serving HTTP 200 (`tests/sim/http_target.py`) → class `unknown`, template `http_server` |
  | 127.0.0.23 | TCP listener `127.0.0.23:9100` | — | (optional) raw TCP accept, to cover the printer port rule |

  Expected classification: .21 → router/opnsense/`opnsense`; .22 → access_point/ubiquiti/`unifi_ap`
  (via the `unifi_vap` probe); .23 → printer/brother/`printer`, `snmp_credential: "extra"`; .24 →
  switch/cisco/`generic_snmp`; .25 → unknown/null/`http_server`. A scan of `127.0.0.20-30` with
  `snmp_port: 1161`, `tcp_ports: [8080, 9100]` and `extra_communities: ["printers"]` finds exactly these 5
  (plus none else on a clean CI box); ICMP to loopback answers for every IP, so set `ping: false` in that test
  to keep "found" deterministic, and add a separate test with ping on.
  Importing .22 with `unifi_ap` must create Ping, System, CPU Load, Memory, `eth0`, and two `Clients HomeWiFi (ng|na)`
  sensors; importing .23 must create a device with **its own** credentials (community `printers`, port 1161).
- **Telegram mock:** `tests/sim/telegram_mock.py`, an aiohttp/http.server app on `127.0.0.1:18081`
  (`NETMON_TELEGRAM_API_BASE=http://127.0.0.1:18081`):
  - `POST /bot<token>/sendMessage`: token `123456:TEST_TOKEN_valid_0123456789abcdef` is valid; any other token →
    `401 {"ok":false,"error_code":401,"description":"Unauthorized"}`. `chat_id` `"-1001"` or `"@netmon_test"`
    → `200 {"ok":true,"result":{"message_id":N,...}}`; `"404"` → `400 {"ok":false,"error_code":400,"description":"Bad Request: chat not found"}`;
    `"429"` → `429 {"ok":false,"error_code":429,"description":"Too Many Requests: retry after 5","parameters":{"retry_after":5}}`;
    `"-1009"` → sleeps 15 s (timeout path; a numeric id, because a chat id like `"slow"` fails `chat_id`
    validation before any request is sent).
  - `GET /_messages` returns the received requests (token, JSON body) for assertions; `DELETE /_messages` clears.
- **What QA should assert (non-exhaustive):** target parsing (cap 4096 → `too_many_hosts`, `/20` accepted,
  multicast rejected), one-scan-at-a-time 409, cancel, WS `discovery.*` reach admins but not viewers,
  results/`existing_device_id` after import, apply-template idempotency (second apply creates 0 and reports
  `exists`), snmp_custom gauge/counter (Counter32 wrap) /string modes, snmp_disk exact and prefix match,
  telegram secret write-only + test errors, discovery webhook payload, scheduler baseline rule, restart marks
  a running scan `failed`, migration 0003 up/down on both Timescale and fallback DBs.

### 9.7 Operator how-to

#### Telegram notifications
1. In Telegram, open a chat with **@BotFather**, send `/newbot`, choose a name and a username ending in `bot`.
   BotFather replies with the **token**, e.g. `123456789:AAE...`. Keep it secret.
2. Decide where alerts go and get the **chat id**:
   - *Private chat:* open your bot, press **Start** (a bot cannot message you first). Then open
     `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and read `"chat":{"id":123456789,...}`.
   - *Group:* add the bot to the group, send any message in the group (for groups, either mention the bot or
     disable privacy mode via BotFather `/setprivacy` so it sees messages), then `getUpdates` as above. Group
     ids are negative, supergroups look like `-100…`.
   - *Channel:* add the bot as an **administrator** of the channel. For a public channel use `@channelname`;
     for a private one, post in the channel and read the `-100…` id from `getUpdates` (`channel_post.chat.id`).
3. In NetworkMonitor: Alerts → Notification channels → Add → type **Telegram**, paste the token and the chat
   id, Save, then **Test**. Errors show Telegram's own text (e.g. "Bad Request: chat not found" = wrong id or
   the bot is not in that chat; "Unauthorized" = wrong token; "Forbidden: bot was blocked by the user" = press
   Start again).
4. Add the channel to an alert rule (and/or to a discovery profile's new-host notifications).
The server needs outbound HTTPS to `api.telegram.org` (Docker Desktop has it by default).

#### Enabling SNMP on the owner's devices (for discovery)
- **OPNsense:** System → Firmware → Plugins → install `os-net-snmp`; Services → Net-SNMP: enable, set a read
  community (and listen interface = LAN). Discovery classifies it as `router` / `opnsense`.
- **UniFi APs (controller-managed):** in the UniFi Network application, Settings → System (or Services) →
  **SNMP Monitoring**: enable v1/v2c and set the community (and/or v3 user). The controller provisions all APs;
  the APs then answer SNMP on their own IPs. Discovery uses the `unifi_vap` probe to recognise them.
- **TP-Link Easy Smart switches:** no SNMP. Discovery finds them by ping and port 80 and suggests
  `http_server` (ping + web UI check); that is the expected outcome.
- Put the community in the target group's SNMP credentials (preferred, devices then inherit it) or add it as
  an extra community in the scan.

## 10. Phase 2b-1: dependencies, maintenance, acknowledge, escalation, fingerprinting, HPE, topology

Contract details: [data-model.md § Phase 2b-1](data-model.md#2b-1-phase-2b-1-additions) (statuses, tables,
algorithms, rules, templates, migration `0004`) and [api.md § Phase 2b-1](api.md#2b-1-phase-2b-1-contract)
(endpoints, types, WS, errors). Backend, frontend and QA implement in parallel against those two files.
Sections are marked **[2b-1]**.

### 10.1 Components [2b-1]
| Component | Module | Responsibility |
|---|---|---|
| Clock | `netmon.clock` | `Clock.now() -> datetime` (UTC, + test offset), `changed: asyncio.Event`. On the runtime as `rt.clock`. Used by maintenance and alerting only. |
| Dependencies | `netmon.services.dependencies` | Pure: `effective_parent(inv, device_id)`, `dependency_sensor(inv, parent_id)`, `find_cycle(inv)`, `parent_down(...)`. I/O: `on_status_change(rt, events)` cascade (down→unreachable, re-check on recovery). `Inventory` gains the dependency columns and sensor type/sort_order/enabled. |
| Processor | `netmon.poller.processor` | After `evaluate()`: `down` + parent down → `unreachable` (message prefix). Clears the acknowledgement on any status change. Sets `events.in_maintenance`. |
| Scheduler | `netmon.poller.scheduler` | New `run_soon(sensor_ids, jitter_s=5)`: an extra one-off run that does not move the regular schedule (skipped when the sensor is already running). |
| MaintenanceTracker | `netmon.services.maintenance` | Pure `occurrences()` / `covers()`; the tracker loop (§ data-model). Started after the alert engine. |
| AlertEngine | `netmon.alerts.engine` | Rewritten around `alert_state`: the event path only upserts rows and `notify()`s; one loop sends due problem / recovery / escalation / reminder notifications. No in-memory timers. `sender` test hook kept. `templates.py` gains the stage subjects and the "Problem since" row. |
| Fingerprint | `netmon.discovery.fingerprint` | `async fingerprint_host(ip, host_header, open_ports, timeout_ms) -> dict` (HTTP/SSH/telnet, limits in api.md). `classify.py` gains `classify_fingerprint()` and `classify_host() -> Classification(device_class, vendor, model, confidence, classified_by)` (pure). |
| Identify | `netmon.discovery.identify` | `identify_device(rt, device) -> DeviceIdentity`, reuses `probe.py` + fingerprint + classify; used by the endpoint. |
| Templates | `netmon.templates.builtin`, `apply` | `hpe_arubaos_switch`, `hpe_comware`; `enterprises` match field; `snmp_table` `skip_zero`/`filters`/`interval_s`; `snmp_custom` `enum` (in `poller/checks/snmp_custom.py` + `sensor_types.py`). |
| Topology | `netmon.topology.collect` (SNMP walks, pure parsers for LLDP/CDP index layouts and id normalisation), `netmon.topology.refresh` (matching, upserts, stale, promotion), `netmon.topology.scheduler` (loop), `netmon.topology.suggest` (pure BFS) | One refresh at a time, semaphore 8, shared SNMP engine. |

Startup order additions: after migrations → mark `running` topology refreshes failed → … → start alert engine
(loop processes overdue `alert_state` rows) → maintenance tracker → topology scheduler. Shutdown: topology
refresh cancelled (ends `failed`, "Cancelled by shutdown"), then the loops.

### 10.2 Key behaviours [2b-1]
- **unreachable** is produced only by the processor (failed/down check + parent down) and by the cascade
  (existing `down` children when a parent goes down). It never alerts, never escalates. The child's `alert_state`
  rows become `suspended`. Recommendation for the owner: give alert rules a `delay_s` at least as long as the
  parent ping interval (e.g. 60–120 s): a child polled before its parent's check noticed the outage would
  otherwise alert `down` before the cascade turns it `unreachable`; with a delay, the pending notification is
  dropped because the sensor is no longer down when it falls due.
- **Maintenance** never hides state; it only suppresses notifications and flags devices/sensors/events.
- **Acknowledge** never hides state; it silences one problem until any status change.
- **Escalation** is measured from `problem_since` (the moment the problem began), not from the first send.
- **Fingerprinting** connects only to the hosts being scanned/identified, follows ≤ 2 same-host redirects,
  reads ≤ 64 KiB, sends no credentials and nothing on SSH/telnet. TLS verification is off for fingerprinting
  only.
- **Topology** writes are admin-only; viewers read the map. Refresh SNMP goes only to monitored devices with
  SNMP sensors.

### 10.3 Frontend (high level) [2b-1]
- **Status `unreachable`:** colour **`violet-500`** (dot `bg-violet-500`, text `text-violet-500`, badge
  `bg-violet-500 text-white`, hex `#8b5cf6`), i18n `status.unreachable` = "Unreachable" / "לא נגיש". Added to
  `STATUSES`, `SEVERITY` (5/4/3/2/1/0 per data-model), filters, `StatusCounts` displays, charts legends.
  `isProblem()` stays down/warning. conventions.md status colours updated.
- **Dependency picker** (device and group edit forms): radio *Inherit from group* / *None* / *Depends on
  device*; device = searchable select (excludes itself), then an optional "Dependency sensor" select of that
  device's sensors (default label "Ping / availability (automatic)"). Read-only line "Effective: depends on
  *branch-gw* (from group *Branch*)" from `effective_dependency`; error `errors.dependency_cycle` shows the path
  names. Device page header shows "Parent: X" (link) and, when `parent_down`, a violet banner "Parent X is down
  — sensors that fail are shown as unreachable".
- **Maintenance:** nav item "Maintenance" (all users; edit for admins), route `/maintenance`: list grouped
  *Active now* / *Upcoming* / *Recurring* / *Past* with scope, schedule text ("Every Sun 02:00, 90 min" in the
  configured time zone, shown in the header), next occurrence, enabled toggle; create/edit drawer (name, scope
  + group/device picker, One-time [start/end datetime inputs interpreted in `settings.timezone`] or Weekly
  [weekday chips, time, duration], comment), "Next occurrences" preview from `/occurrences`. **Badge** (wrench
  icon, "In maintenance until 02:00") on devices/sensors in tree, lists, device/sensor pages; global banner in
  the layout when a `global` window is active. Device page has "Add maintenance window" (prefilled scope
  device). Settings page gets a time-zone select (`Intl.supportedValuesOf('timeZone')`), and the setup wizard
  sends the browser time zone silently.
- **Acknowledge:** on the sensor page and in problem lists (dashboard, sensors filtered by status) an admin
  "Acknowledge" button for down/warning → small dialog with optional comment; acknowledged sensors show a check
  badge with tooltip "by admin at 09:12: comment" and an "Unacknowledge" action. Dashboard: *Problems* (not
  acknowledged), *Acknowledged* (separate card), *Unreachable devices* card (with parent), maintenance count.
  `sensor.ack` WS updates in place.
- **Alert rule form:** collapsible "Escalation" section: "Escalate after [n] minutes if not acknowledged" +
  channel multi-select; "Remind every [n] minutes until acknowledged or recovered". Inputs in minutes, sent in
  seconds. Rule list shows an escalation icon.
- **Discovery results:** columns gain *model* and a confidence chip (`classified_by` icon: SNMP / web / ports);
  an expandable row shows the fingerprint (HTTP titles/servers per port, SSH/telnet banner, markers).
- **Device page:** admin action "Re-identify" → spinner (≤ 40 s) → result card (class, vendor, model,
  confidence, fingerprint, suggested template) with "Apply suggested template" (existing apply flow). The
  stored `identity` is shown in the device details.
- **Templates:** `snmp_custom` form handles `value_type: enum` with a value-map table editor
  (`config_fields` type `value_map`).
- **Topology map** (`/topology`, nav "Map", all users): **ECharts `graph` series** (already a dependency, no new
  library). Nodes: devices as circles coloured by `STATUS_HEX[status]` with a class icon symbol
  (`path://` icons), maintenance as a small wrench badge (second symbol via `label.rich`), neighbours as
  hollow dashed "ghost" nodes. Edges: colour from the interface sensor of either end (worst of the two:
  `oper_status` 1 → up colour, other → down colour; no sensor → neutral gray; stale → dashed, 40 % opacity);
  optional label toggle "Show traffic" (`in_bps`/`out_bps`, human units). Click edge → side panel with both
  port names, link source, first/last seen, a Delete (admin) button; click device node → device page; click
  neighbour → panel with chassis id / mgmt address / "Add as device" (opens the device create form with
  `host` = mgmt address, `name` = sys name). Toolbar: group filter, "Include stale", "Refresh topology now"
  (admin; shows `topology.refresh` progress), "Reset layout" (admin), "Add link" (admin, manual link dialog),
  "Suggest dependencies" (admin) → table of suggestions with checkboxes and current value → "Apply selected"
  → `POST /api/devices/dependencies`.
  - Layout: if every node has `x/y` → `layout: 'none'`. Otherwise run `layout: 'force'` once (repulsion scaled by
    node count, `layoutAnimation: false`), then read positions from
    `chart.getModel().getSeriesByIndex(0).getData().getItemLayout(i)`, switch to `layout: 'none'`, and (admin)
    save them with `PUT /api/topology/layout`; nodes that already had positions are kept fixed (`fixed: true`)
    during that run. Dragging (`draggable: true`) saves the moved node on `mouseup` (admins only; viewers can drag
    but nothing is saved). One global layout.
  - RTL: the canvas is not mirrored (coordinates are absolute); side panels/toolbars use logical utilities;
    labels with IPs/ports in `dir="ltr"` spans in panels. Dark mode: colours from the Tailwind theme tokens
    (background transparent, label/edge colours switched by the `dark` class through the ECharts option).
  - Fallback if ECharts graph turns out insufficient: report to the architect before adding any library.
- New i18n keys (en + he): `status.unreachable`, `dependency.*`, `maintenance.*`, `ack.*`, `escalation.*`,
  `identify.*`, `fingerprint.*`, `confidence.*`, `classifiedBy.*`, `topology.*`, `nav.maintenance`, `nav.map`,
  `errors.dependency_cycle`, `errors.not_acknowledgeable`, `errors.topology_refresh_running`,
  `errors.link_not_manual`, `eventKind.acknowledge`, `eventKind.maintenance`, `sensorConfig.snmp_custom.value_map`,
  `sensorConfig.snmp_custom.value_map_default`, template names of `hpe_arubaos_switch` / `hpe_comware`.

### 10.4 QA simulators and tests [2b-1] (`tests/sim/`)
- **Time control:** run the server with `NETMON_TEST_MODE=1` and use `PUT /api/test/clock {"offset_s": N}` to
  jump into/out of maintenance windows and past `delay_s` / `escalation_after_s` / `repeat_every_s` (the loops
  wake immediately). Backend unit tests inject a fake `Clock`. The poller still runs in real time, so use
  short sensor intervals (10 s) for status transitions.
- **Dependency sims:** two TCP/HTTP targets or loopback IPs whose availability the test toggles (e.g. an HTTP
  sensor against `tests/sim/http_target.py` on 127.0.0.31 as the parent's "dependency sensor", children with
  HTTP sensors on 127.0.0.32/.33; stopping the targets simulates the outage). Assert: parent down → children
  `unreachable` with the message format, no notification (webhook mock receives nothing for children), rollup
  counts; parent up → children re-checked within ~5 s and back to `up`; child target kept down → `unreachable →
  down` → webhook receives DOWN. Cycle validation (direct, via group inheritance, bulk apply), self-rule.
- **Maintenance:** weekly window across midnight and across a DST change (unit tests on `occurrences()` with
  `Europe/Berlin` / `Asia/Jerusalem` dates), group scope with subgroups, global scope; notification suppressed
  in the window, sent once after the window when still down (`after_maintenance: true`), nothing when recovered
  inside; recovery of a pre-window notified problem is sent inside the window; `maintenance` events and
  `maintenance.changed`.
- **Ack/escalation:** ack stops escalation and reminders, recovery still reaches primary + escalation channels;
  ack auto-clears on status change; viewer 403; escalation survives a server restart (stop/start the process
  between the problem and `escalation_after_s`); reminder count; migration of `alert_notified` rows.
- **Fingerprint sims** (`tests/sim/fingerprint/`, one asyncio script, loopback IPs 127.0.0.41–.49):
  | IP | Ports | Serves | Expected |
  |---|---|---|---|
  | .41 | 80 | `<title>TL-SG108E</title>`, body contains `TP-LINK` | switch / tp-link / model `TL-SG108E` / medium (F7) |
  | .42 | 443 (self-signed) | 302 → `/ui/` same host, then `<title>Login \| OPNsense</title>` | router / opnsense / high, `redirects: 1` |
  | .43 | 80 | 302 → `http://example.com/` | not followed: `redirects: 0`, status 302 |
  | .44 | 22 | banner `SSH-2.0-Comware-7.1.064\r\n` | switch / h3c / high (F9) |
  | .45 | 22 | banner `SSH-2.0-ROSSSH\r\n` | router / mikrotik / high |
  | .46 | 23 | IAC negotiation bytes then `\r\nUser Access Verification\r\n` | banner text without IAC bytes |
  | .47 | 80 | `Server: HP HTTP Server; HP LaserJet …`, no title | printer / hp |
  | .48 | 8080 | 100 KiB body without a title, `WWW-Authenticate: Basic realm="DGS-1210-28"` | body capped at 64 KiB, realm captured, model `DGS-1210-28` |
  | .49 | 80 | accepts and never answers | `error: "timeout"` within `fingerprint_timeout_ms` |
  Plus pure unit tests for every F-rule and the SNMP + fingerprint `unknown` fallback.
- **HPE snmpsim** (`tests/sim/hpe/`, one process per device, port 1161, community `public`):
  - `127.0.0.51` **ArubaOS-Switch** (`arubaos.snmprec`): sysObjectID `1.3.6.1.4.1.11.2.3.7.11.181` (2930F-style),
    sysDescr `Aruba JL256A 2930F-48G-4SFP+ Switch, revision WC.16.11.0012, ROM WC.16.01.0010 (/ws/swbuildm/...)`;
    hpSwitchCpuStat `.1.3.6.1.4.1.11.2.14.11.5.1.9.6.1.0` = 7; hpLocalMemTable row 1 (.5.1 = 330000000, .6.1 =
    200000000, .7.1 = 130000000); hpicfSensorTable rows 1–4: (Descr `Fan Sensor`, Status 4), (`Power Supply
    Sensor`, **2 = bad**), (`Over-temperature Sensor`, 4), (`Redundant Power Supply Sensor`, **5 = notPresent**);
    pethMainPseTable group 1 (.2.1 = 370, .3.1 = 1, .4.1 = 45); ifTable/ifXTable ports `1`…`4` (type 6, 1 and 2
    up), `DEFAULT_VLAN` (type 53, up); LLDP data below.
  - `127.0.0.52` **Comware** (`comware.snmprec`): sysObjectID `1.3.6.1.4.1.25506.11.1.177`, sysDescr
    `HPE Comware Platform Software, Software Version 7.1.070, Release 6328 HPE 1950-24G-2SFP+-2XGT Switch`;
    entPhysicalTable indexes 1 (chassis, class 3), 2 (board `Board`, class 9), 3/4 (fans `Fan 1`/`Fan 2`,
    class 7), 5 (PSU `PSU 1`, class 6); hh3cEntityExt `.6.2` = 12 (CPU), `.6.1/.6.3/.6.4/.6.5` = 0, `.8.2` = 41,
    `.12.2` = 43, `.12.1` = 0; ErrorStatus `.19.3` = 2, **`.19.4` = 41 (fanError)**, `.19.5` = 2, `.19.1` = 1;
    pethMainPseTable group 1 (.2.1 = 0 → PoE rows filtered/skipped); ifTable `GigabitEthernet1/0/1`…`/4`,
    `Vlan-interface1` (type 136), `NULL0` (type 1), `InLoopBack0` (type 24).
  - Expected apply results: ArubaOS → Ping, System, CPU, `Memory 1` (≈ 39.39 %), `Fan Sensor` up,
    `Power Supply Sensor` **down** (`bad (2)`), `Over-temperature Sensor` up, no notPresent row, PoE usage
    (≈ 12.16 %), power 45 W, budget 370 W, interfaces `1`, `2`. Comware → Ping, System, `CPU Board`, `Memory
    Board`, `Temperature Board`, `Fan 1` up, `Fan 2` **down**, `PSU 1` up, PoE rows skipped (`no_rows`),
    interfaces up only. Suggested template per sysObjectID. Both classify as `switch` (`hp` / `h3c`).
- **LLDP snmpsim tree** (same agents plus two more): `127.0.0.51` core (ArubaOS style: lldpLocChassisId subtype
  4 MAC `00:1a:1e:00:00:51`, lldpLocPortTable PortNum = ifIndex 1–4, port id subtype 7 = `1`…`4`); `127.0.0.52`
  access-1 (Comware style: lldpLocPortNum 101–104 ≠ ifIndex 1–4, port id subtype 5 =
  `GigabitEthernet1/0/1`…); `127.0.0.53` access-2 (ArubaOS style, minimal: system + ifTable + LLDP); `127.0.0.54`
  UniFi AP (net-snmp Linux, LLDP with `eth0`). Remote rows (index `0.<localPortNum>.<n>`): core port 1 ↔ access-1
  `GigabitEthernet1/0/1` (mgmt addr 127.0.0.52 in lldpRemManAddrTable), core port 2 ↔ access-2 port 24 (no mgmt
  addr; match by chassis id), access-1 `GigabitEthernet1/0/2` ↔ AP `eth0` (match by sysName `U6-Lite-Office`),
  access-2 port 3 ↔ **unmanaged** neighbour (chassis `00:11:22:33:44:55`, sysName `printer-3f`, mgmt
  `127.0.0.99`, no device). Expected: 3 device–device links (deduplicated from both sides), 1 neighbour node,
  ifIndex resolution correct on both families, dependency suggestions with root = core: access-1/access-2 →
  core, AP → access-1. Removing a remote row and refreshing 3× → `stale: true`; adding a device with host
  127.0.0.99 and refreshing → neighbour promoted (links re-pointed). CDP: one `cdpCacheTable` row on access-2
  (optional test).
- Migration `0004` up/down on Timescale and fallback DBs, with `alert_notified` data present.

### 10.5 Owner notes [2b-1]
- Set the server time zone once in Settings (the wizard pre-fills it on new installs; existing installs start
  as `UTC` unless `TZ` is set) — weekly maintenance windows use it.
- Add a `delay_s` (≥ the parent's ping interval) to alert rules so child alerts are reliably replaced by
  `unreachable` during a parent outage.
- Verify the HPE OIDs marked medium/low on the real switches (CPU/memory/health/PoE rows); OfficeConnect
  1820/1920S may lack the ArubaOS-Switch MIBs (their rows are then skipped, not wrong).
- Enable LLDP on the switches (on by default on ArubaOS-Switch and Comware) and SNMP read access to the LLDP-MIB
  (SNMPv3 views must include `1.0.8802`). TP-Link Easy Smart switches (no SNMP) never report links; add manual
  links for them.
