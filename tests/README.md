# NetworkMonitor — QA tests

End-to-end tests that run the **real** frontend against the **real** backend, plus simulators for the
monitored side (SNMP agent, HTTP target, webhook receiver).

```
tests/
├── e2e/                 Playwright suite (TypeScript)
│   ├── run.sh           one-command runner: fresh DB + build + backend + simulators + tests + teardown
│   ├── playwright.config.ts
│   ├── lib/helpers.ts   API client, login helpers, polling waits, simulator controls
│   └── specs/           01-… 09-… (run in file order, one worker)
└── sim/
    ├── snmp_agent.py         snmpsim launcher (prints READY <port>)
    ├── snmp/public.snmprec   the simulated device "netmon-sim" (community `public`)
    ├── http_target.py        HTTP target whose health can be toggled
    └── webhook_receiver.py   records every POST as a JSON line
```

## Running the E2E suite

Prerequisites: PostgreSQL ≥ 14 (TimescaleDB optional) reachable with a role that may `CREATE DATABASE`,
`backend/.venv` with `pip install -e ".[dev]"` (includes snmpsim), `frontend/node_modules`, Node 20+, and a
Chromium for Playwright (the config uses `/opt/pw-browsers/chromium` when it exists, otherwise the browser
that `@playwright/test` installed). Ping checks need ICMP (root, or `net.ipv4.ping_group_range`).

```bash
tests/e2e/run.sh                                  # everything
tests/e2e/run.sh specs/04-http-alert.spec.ts      # one spec (extra args go to `playwright test`)
PG_ADMIN_URL=postgresql://postgres:postgres@localhost:5432/postgres SKIP_BUILD=1 tests/e2e/run.sh
```

`run.sh`:

1. creates a fresh database `netmon_e2e_<timestamp>_<pid>` (dropped afterwards unless `KEEP_DB=1`);
2. builds the SPA (`npm run build:copy`, skipped with `SKIP_BUILD=1`);
3. starts the simulators and `netmon serve` on free ports with a temporary `NETMON_DATA_DIR`;
4. waits for `/api/health`, the simulators' health endpoints and the SNMP agent's `READY`;
5. runs `npx playwright test` with `BASE_URL`, `SNMP_PORT`, `HTTP_TARGET_URL`, `WEBHOOK_URL`,
   `SCREENSHOT_DIR` exported;
6. stops everything and drops the database.

Logs: `tests/e2e/test-results/logs/` (backend, simulators, received webhooks). Playwright traces and failure
screenshots: `tests/e2e/test-results/artifacts/`, HTML report: `tests/e2e/test-results/report/`.
Screenshots of key screens: `$SCREENSHOT_DIR` (default `tests/e2e/test-results/screenshots/`).

You can also point the suite at an already running instance (`cd tests/e2e && BASE_URL=... npx playwright test`),
but `01-setup-auth` needs an empty database, and specs 03/04 need the simulators (`SNMP_PORT`,
`HTTP_TARGET_URL`, `WEBHOOK_URL`). Every other spec creates the admin through the API when the DB is fresh, so
single specs can run on their own.

## Scenarios

| Spec | Covers |
|---|---|
| `01-setup-auth` | Setup wizard → dashboard; `/api/setup` closed afterwards (409); logout → 401 → deep-link login; wrong password error |
| `02-ping-device` | Group + device `127.0.0.1` with auto ping sensor via the UI; scheduled check → "Up" live in the tree (no reload) and dashboard; rollup; history chart from real measurements |
| `03-snmp` | SNMP v2c device on the simulator; Test + create CPU/memory sensors; interface picker (enumeration, filter, add 2, "already monitored"); real values: CPU 30 %, memory 62.5 %, eth0 ≈ 10 Mbit/s, eth1 oper-down → Down |
| `04-http-alert` | HTTP sensor on the toggle target; webhook channel (+ test send); per-sensor alert rule; target fails → Down live in header/tree/device/dashboard, event, webhook payload; recovery → Up + recovery webhook; exactly one webhook per transition |
| `05-hebrew-rtl` | Language switch → `dir=rtl`, Hebrew texts, saved to `user.language`; persists after reload and in a new browser |
| `06-viewer` | Admin creates a viewer; no admin nav / write buttons / editable thresholds; admin routes show "forbidden"; 15 write/admin endpoints answer `403 forbidden` |
| `07-thresholds` | `latency_ms.warning_max = 0.001` → Warning (live, dashboard problem list, event); restore → Up; invalid input rejected |
| `08-ui-checks` | Login language from `settings.default_language`; 401 → login; 422 details; 409 conflict text; SNMP error in picker; known UI defects (see below) |
| `09-screenshots` | Screenshots of the real system (dashboard en/he @2x, sensor charts, events, settings, dark, phone) |

Timing: sensors use `interval_s = 10` (the API minimum). Waits are `expect.poll` / auto-retrying
assertions against the real backend and UI, never fixed sleeps.

## Simulators

```bash
backend/.venv/bin/python tests/sim/snmp_agent.py --port 11161      # SNMP v2c, community "public"
python3 tests/sim/http_target.py --port 18081                      # GET / -> 200 or 503
python3 tests/sim/webhook_receiver.py --port 18082 --out hooks.jsonl
```

- **SNMP device "netmon-sim"**: system group (sysUpTime grows), ifTable/ifXTable with `lo`, `eth0` (1 Gbit/s,
  up, ~10 Mbit/s in, ~1 Mbit/s out, 1 error/s) and `eth1` (100 Mbit/s, oper down), hrStorage (RAM 62.5 % of
  8 000 000 × 1 KiB, `/` 25 %) and two hrProcessorLoad rows (20 %, 40 %). The counters use snmpsim's `numeric`
  variation module, so they grow with wall-clock time. Running as root, the agent drops to `nobody:nogroup`.
- **HTTP target**: `GET /` answers `200 "NetworkMonitor sim OK"` while healthy and `503 "sim failing"` while
  failing. Control: `POST /toggle`, `/fail`, `/ok`, `GET /state`, `GET /health`.
- **Webhook receiver**: records each POST (`time`, `path`, `headers`, `body`). `GET /records` returns them,
  `DELETE /records` clears them.

## Other suites

```bash
cd backend && TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres .venv/bin/pytest -q
cd frontend && npm test -- --run
```
