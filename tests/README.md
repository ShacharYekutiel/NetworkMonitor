# NetworkMonitor — QA tests

End-to-end tests that run the **real** frontend against the **real** backend, plus simulators for the
monitored side (SNMP agent, HTTP target, webhook receiver).

```
tests/
├── e2e/                 Playwright suite (TypeScript)
│   ├── run.sh           one-command runner: fresh DB + build + backend + simulators + tests + teardown
│   ├── playwright.config.ts
│   ├── lib/helpers.ts   API client, login helpers, polling waits, simulator controls
│   ├── lib/lab.ts       Phase 2a: discovery lab expectations, Telegram mock, scan waits, extra hosts
│   └── specs/           01-… 18-… (run in file order, one worker)
└── sim/
    ├── lab.sh                Phase 2a lab launcher (start/stop/status/env)
    ├── snmp_agent.py         snmpsim launcher (--host/--port/--data; prints READY)
    ├── snmp/public.snmprec   the simulated device "netmon-sim" (community `public`)
    ├── discovery/            per-device snmprec data of the discovery lab (+ gen_snmprec.py)
    ├── telegram_mock.py      Telegram Bot API mock
    ├── tcp_listener.py       raw TCP accept-and-close listener (printer port 9100)
    ├── http_target.py        HTTP target whose health can be toggled (--host/--port)
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
5. starts the Phase 2a discovery lab (`tests/sim/lab.sh start` with `LAB_GENERIC=0`, Telegram mock on a free
   port) and passes `NETMON_TELEGRAM_API_BASE` to the backend (`LAB_EXTERNAL=1` reuses a lab you started);
6. runs `npx playwright test` with `BASE_URL`, `SNMP_PORT`, `HTTP_TARGET_URL`, `WEBHOOK_URL`,
   `SCREENSHOT_DIR`, `TELEGRAM_MOCK_URL`, `DISCOVERY_SNMP_PORT`, `BACKEND_LOG` exported;
7. stops everything and drops the database.

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
| `10-discovery-scan` | **[2a]** UI scan of `127.0.0.20-30` (snmp_port 1161, extra community `printers`, TCP 8080/9100, ping off): live progress + rows via WS, exactly the 5 lab hosts with the documented class/vendor/template/credential, existing device flagged, filters; ping-on scan finds all 11; target validation (`too_many_hosts`, multicast, …) |
| `11-discovery-import` | **[2a]** Import 4 hosts with their templates into a group → the documented sensors per device; own credentials (port 1161 / community `printers`); re-import → `device_exists`; values: UniFi clients 5/12, toner 40 %, drum "level not reported", page counter, disk `/` 25 %, igb0 ≈ 20 Mbit/s, HTTP 8080 |
| `12-apply-template` | **[2a]** Template list/order; apply `generic_snmp` to the switch from the device page (created/skipped), re-apply idempotent (`exists`), disabled device → paused sensors, `/templates` page |
| `13-snmp-custom-disk` | **[2a]** `snmp_custom` gauge (+ multiplier, unit PATCH) and string (contains/equals) via the form, counter rate, missing OID / non-numeric / validation; `snmp_disk` via the storage picker, exact-match rules |
| `14-telegram` | **[2a]** Telegram channel: create + test (mock receives HTML message), edit keeps the token, wrong token → 502 `telegram_error` shown, chat-not-found / 429 texts, plain mode, validation, alert rule → down + recovery messages, token never in any API response or the backend log |
| `15-discovery-profiles` | **[2a]** Scheduled profile via the UI, Run now (baseline, no notification), new host 127.0.0.41 appears → scheduled run → `discovery` event + webhook `discovery.new_hosts` + Telegram for that host only; profile API secrets/merge/validation (waits ~1–2 min for the scheduler) |
| `16-discovery-cancel` | **[2a]** Cancel a running /20 scan from its page; concurrent scan → 409 `scan_running` (API + UI), delete/import/profile-run while running → 409; delete a finished scan |
| `17-discovery-viewer` | **[2a]** Viewer: no Discovery nav, `/discovery` forbidden, 403 on all discovery endpoints + apply-template + storage, templates readable, no `discovery.*` WS frames |
| `18-discovery-rtl-mobile` | **[2a]** Discovery + results in Hebrew (RTL, translated classes/templates, LTR IPs), phone width without horizontal overflow (en/he); screenshots to `$QA2A_SHOT_DIR` (default `$SCREENSHOT_DIR/qa-2a`) |

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

## Phase 2a discovery lab (`tests/sim/lab.sh`)

```bash
tests/sim/lab.sh start     # everything below + Phase 1 sims (LAB_GENERIC=0 to skip those); prints the env
tests/sim/lab.sh status
tests/sim/lab.sh stop
eval "$(tests/sim/lab.sh env)"   # NETMON_TELEGRAM_API_BASE, TELEGRAM_MOCK_URL, ...
```

| Address | Simulates | Community | Expected classification / template |
|---|---|---|---|
| `127.0.0.21` udp/1161 | OPNsense 24.7 (net-snmp, FreeBSD OID `.8072.3.2.8`): `igb0`, `igb1`, `lo0`, `pflog0`, `enc0` (down); CPU 10/30 %; RAM 25 %; `/` (index 31) 25 %, `/boot/efi` | public | router / opnsense / `opnsense` |
| `127.0.0.22` udp/1161 | UniFi U6-Lite (`.8072.3.2.10`): `lo`, `eth0`, `br0`, `ath0`; unifiVap rows 1/2 = `HomeWiFi` 5 clients `ng`, 12 clients `na`; CPU 17 %; RAM 40 % | public | access_point / ubiquiti / `unifi_ap` |
| `127.0.0.23` udp/1161 + tcp/9100 | Brother NC-8300h (`.2435.2.3.9.1`): page counter 12345, Black Toner 40/100, Drum Unit -3/100 | **printers** | printer / brother / `printer` (`snmp_credential: "extra"`) |
| `127.0.0.24` udp/1161 | Cisco 2960-X (`.9.1.1208`): Gi1/0/1–2 up, Gi1/0/3–4 down, no HOST-RESOURCES-MIB | public | switch / cisco / `generic_snmp` |
| `127.0.0.25` tcp/8080 | HTTP-only host (`http_target.py`) | — | unknown / null / `http_server` |
| `127.0.0.1:18081` | Telegram Bot API mock | — | token `123456:TEST_TOKEN_valid_0123456789abcdef`; chat `-1001` / `@netmon_test` ok, `404` chat not found, `429` rate limit, `slow` 15 s; also rejects unparsable HTML like the real API. `GET/DELETE /_messages`. |

The snmprec files are generated by `tests/sim/discovery/gen_snmprec.py` (keeps OIDs sorted). Quick checks:

```bash
snmpget -v2c -c public 127.0.0.21:1161 1.3.6.1.2.1.1.1.0
snmpwalk -v2c -c printers 127.0.0.23:1161 1.3.6.1.2.1.43.11.1.1
curl -s -H 'Content-Type: application/json' -d '{"chat_id":"-1001","text":"hi"}' \
  http://127.0.0.1:18081/bot123456:TEST_TOKEN_valid_0123456789abcdef/sendMessage
curl -s http://127.0.0.1:18081/_messages
```

Spec 15 starts extra HTTP hosts on `127.0.0.40` / `127.0.0.41:8080` itself.

## Other suites

```bash
cd backend && TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres .venv/bin/pytest -q
cd frontend && npm test -- --run
```
