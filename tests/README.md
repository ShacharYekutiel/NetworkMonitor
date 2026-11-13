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
│   ├── lib/lab2b.ts     Phase 2b-1: test clock, backend restart, lab component control, toggle targets
│   ├── lib/lab2b2.ts    Phase 2b-2: port lab setup (idempotent), refresh waits, search, user template dir, i18n
│   └── specs/           01-… 37-… (run in file order, one worker)
└── sim/
    ├── lab.sh                Phase 2a lab launcher (start/stop/status/env)
    ├── snmp_agent.py         snmpsim launcher (--host/--port/--data; prints READY)
    ├── snmp/public.snmprec   the simulated device "netmon-sim" (community `public`)
    ├── discovery/            per-device snmprec data of the discovery lab (+ gen_snmprec.py)
    ├── fingerprint/fp_hosts.py  [2b-1] HTTP(S)/SSH/telnet fingerprint hosts 127.0.0.41-49, 61-67
    ├── hpe/                  [2b-1] HPE ArubaOS/Comware + LLDP tree snmprec data (+ gen_snmprec.py)
    ├── ports/                [2b-2] port-inventory switches + ARP sources (+ gen_snmprec.py, ptr.json)
    ├── vendors/              [2b-2] Check Point / ClusterXL / Aruba Instant + synthetic per-template sims
    ├── templates/user/       [2b-2] user template directory fixtures (10 files, 7 expected errors)
    ├── verify_2b2.py         [2b-2] standalone pysnmp check of every 2b-2 sim against the contract tables
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
| `19-dependencies` | **[2b-1]** Parent .31 / children .32 (explicit, dependency sensor) and .33 (group-inherited, whole-device mode): API shapes + validation; outage -> children `unreachable` (violet, live, banner, rollups, dashboard card), only the parent alerts; parent up -> children re-checked -> up; child still down -> `unreachable -> down` -> alert; cycles (direct, self, via group, bulk) -> `400 dependency_cycle` (UI shows the names); self rule; parent delete resets dependents |
| `20-maintenance` | **[2b-1]** Once window from the device page covering now -> suppressed DOWN, `in_maintenance` flags/events; test clock past the end -> one DOWN `after_maintenance`, recovery after; problem inside the window only -> nothing; weekly window + occurrences preview + schedule text; validation; `settings.timezone` (Asia/Jerusalem shifts occurrences, invalid -> 422); settings time-zone select |
| `21-acknowledge` | **[2b-1]** 409 on up; ack with comment in the UI -> `sensor.ack`, Acknowledged card, not in Problems; escalation + reminders silent (test clock); unack -> reminder resumes; re-ack; recovery auto-clears and notifies the notified channel |
| `22-escalation` | **[2b-1]** Escalation section of the rule form (minutes -> seconds), validation; problem -> primary; **backend restart**; escalation once after the delay (test clock), still once after another restart; reminders one per interval, no burst; recovery to primary + escalation channel |
| `23-fingerprint` | **[2b-1]** Scan 127.0.0.41-49 + 61-67 with fingerprinting: class/vendor/model/confidence/classified_by per F-rules, fingerprint JSON (titles, redirects, TLS self-signed, banners, IAC stripping, 64 KiB cap, realm, timeout), no credentials sent (sim log); results page details; Re-identify (UI, fingerprint-only) + SNMP identify of .51 |
| `24-hpe-templates` | **[2b-1]** Scan + import .51/.52 with the auto-selected `hpe_arubaos_switch` / `hpe_comware`: exact sensor lists, PSU bad / Fan 2 fanError down, notPresent skipped, PoE only on ArubaOS (Comware `no_rows`), values (CPU, memory, PoE %, W, temperature), enum configs |
| `25-topology` | **[2b-1]** LLDP refresh: 3 deduplicated links with the right ifIndexes (Comware `lldpLocPortNum != ifIndex`), neighbour printer-3f; map renders (+ dark); refresh 409; manual links (UI/API, PATCH, `link_not_manual`, survive refresh); promotion of the neighbour; stale after 3 refreshes (agent data swap) + bulk delete; suggestions (root core) -> Apply selected -> dependencies; layout + group filter + `topology.updated` |
| `26-viewer-2b1` | **[2b-1]** Viewer reads map/maintenance; 15 write endpoints -> 403; no admin buttons on map/maintenance/sensor/device pages |
| `27-rtl-mobile-2b1` | **[2b-1]** Hebrew (`dir=rtl`, no English leftovers, LTR IPs) on maintenance/topology/dashboard/device/sensor; 390 px without horizontal overflow (en/he); screenshots to `$QA2B1_SHOT_DIR` |
| `28-ports-faceplate` | **[2b-2]** Class detection + effective flags; the §11.6 expected port tables (AOS/CW/CX: state, speed, duplex, VLAN/mode/PVID, PoE, MACs + OUI + ARP IP + PTR, uplink by MAC count / neighbour, alert, last change, rates); health strip of the 2b-1 HPE sims; faceplate layout per parser (odd top / even bottom, uplink block, member 2, module block), tile states/colours/icons/badges, aria-labels, legend, LTR inside Hebrew |
| `29-port-modal` | **[2b-2]** Port modal fields (state, link, PoE, traffic, MAC table with vendor/IP/hostname, random MAC), uplink "Show all MACs" (`include_uplink_macs`), neighbour + AP links, Start monitoring -> `snmp_interface` sensor, close by X / Esc / backdrop, `?port=` deep link, focus trap |
| `30-ports-list` | **[2b-2]** Default columns, physical-only toggle, connected texts, sticky Port column, one filter (MAC in 3 notations, IP, hostname, vendor, neighbour, alias), column chooser persisted in localStorage (+ corrupt value), row click -> modal |
| `31-ports-refresh` | **[2b-2]** Job status (timestamps, counts, next run, `arp_sources`), never-run shape, 409 `ports_disabled` / `ports_refresh_running`, flags auto/on/off + 422, settings validation (interval 0 -> no next run), uplink threshold boundary, Refresh button -> WS `ports.updated` -> refetch (also for another device), failed run (agent killed) keeps the snapshot |
| `32-mac-presence` | **[2b-2]** MAC removed from the switch (lab swap `arubaos_nomac7`) -> `present:false`, `mac_count` 0, `last_seen` kept; back -> present, `first_seen` kept; retention setting; device delete cascades |
| `33-mac-search` | **[2b-2]** `/api/search/mac` exact / notations / prefix / IP, best access port vs uplink rows, ARP hits, invalid/local ARP dropped, 422 validation; Find page (best row, port link -> modal, `?ip=`, invalid input) |
| `34-template-library` | **[2b-2]** 19 built-ins in order, confidence on vendor OIDs, the nine converted templates equal `backend/tests/fixtures/templates_2b1.json`; user dir fixtures -> 3 user templates, override, 7 errors (exact messages/paths), `templates.reloaded`, errors panel / badges in the UI, Reload button, delete override, Comware suggestion, user template apply, viewer view |
| `35-vendor-templates` | **[2b-2]** Scan of .71-.73 + synthetic .101-.110: classification (rule 9a incl. net-snmp + `checkpoint` probe), suggestions; apply `checkpoint_gaia` (standalone + ClusterXL), `aruba_wifi` -> created / skipped (`not_applicable`, `probe_failed`, `no_rows`) + live values + health strip; apply dialog shows `not_applicable`; synthetic mechanics of all 10 new templates |
| `36-viewer-2b2` | **[2b-2]** Viewer reads ports / port / search; 8 write endpoints -> 403; no Refresh / Start monitoring / Enable / Reload buttons; admin "Enable port inventory" PATCHes `on` |
| `37-rtl-mobile-2b2` | **[2b-2]** 52-port faceplate at 390 px (en/he): scrolls inside its card, no page overflow, 22 px tiles, LTR; list / modal / Find / Templates at 390 px; no raw i18n keys; device form selects in Hebrew; screenshots to `$QA2B2_SHOT_DIR` |
| `51-features-3` | **[3]** Fresh install: 8 phase-3 toggles OFF, `sso` null in settings/public, every gated endpoint 409 `feature_disabled` (kiosk 404, OIDC callback -> `sso_error=feature_disabled`), nav hidden, `/noc` and `/ack` "not available"; each toggle ON/OFF at runtime (SSO toggles 422 until configured), nav follows live without reload; Features tab headings |
| `52-config-backup` | **[3]** SSH credentials on a group (write-only), driver override, `ssh/test` steps + masked preview + `502 ssh_error`, UI "Back up all" for Comware / ArubaOS / Check Point clish + bash (fallback), stored text == `tests/sim/ssh/expected/*`, unchanged run, change -> v2 `+2 -1` + diff (API + UI), download, ZIP export, search (text, regex, 422), read-only command log |
| `53-config-backup-events` | **[3]** Host key rotated -> `host_key_changed` before auth, sensor down + condition `host_key`, system event, 400 while changed, `409 host_key_mismatch`, UI accept, reset pin; CFGMAN syslog x3 + built-in rule -> one backup after the 120 s debounce (test clock), `config_change` in the timeline, `config_change_alert` -> warning -> webhook |
| `54-flows` | **[3]** Receivers on the 3 UDP ports, sFlow (127.0.0.91, 1:4096) + NetFlow v9 (.92) auto-accepted, unknown (.93) accept, (.94) ignore; talkers / applications / conversations / interfaces / series == `send_flows.py --expect`; sampling_seen; data before template; IPFIX options sampling; Flows page |
| `55-new-devices` | **[3]** Lab 127.0.0.90 (`tests/sim/newdev`): learning 0, run 1 baseline, run 2 Intel MAC new with IP/hostname, HP OUI allowed, random/uplink/trunk ignored, flood guard 20 + "+6 more", one webhook WARNING, UI acknowledge, OFF removes conditions |
| `56-sso-kerberos` | **[3]** (needs `krb5kdc`) keytab upload/parse/warnings, challenge, jdoe viewer / asmith admin / nogroup refused, NTLM (3 fixtures) `sso_ntlm` without challenge, garbage, 48 KB header, wrong realm, replay; Chromium with `--auth-server-allowlist` + KRB5CCNAME: auto sign-in, logout no re-login, `?sso=0`, no ticket -> form |
| `57-sso-oidc` | **[3]** Mock Entra IdP: start (PKCE S256, state, nonce, Lax path-scoped cookie), admin via app role, viewer, no role -> `not_in_group`, groups + overage, 8 failing id_token modes, leeway and key rotation ok, Entra error not echoed, forged state, open-redirect `next`, OIDC session refused while OFF |
| `58-reports` | **[3]** Weekly definition (validation, next_run_at, unique name), Run now over a real outage -> availability == contract formula on the sensor's events, email in the SMTP sink (subject, tables, no remote resources, text part, < 1 MB), HTML download, report page print media, Hebrew RTL preview |
| `59-noc` | **[3]** Screen token once, hashed; kiosk view from 127.0.0.1 vs another source (404), wrong token, header useless on `/api/devices`; WS 4401 without/late/bad auth, `kiosk.refresh` on a status change; `/noc#token` without session or cookies, down device appears live; 10.9.9.9-only screen -> not available; regenerate; feature OFF -> 404/409; 429 after 30 failures |
| `60-chat-ack` | **[3]** Telegram poller (getUpdates), link code -> `/link` via the mock -> linked + reply, single use; problem -> inline button, stranger press refused (show_alert), linked admin press -> acknowledged + `✓ admin hh:mm` markup, already acknowledged; Teams card `Action.OpenUrl` -> `/ack#` -> acknowledge with comment, `ack_stale` after recovery; forged link; 409 webhook conflict -> take over; chat_ack OFF -> no buttons |
| `61-viewer-3` | **[3]** Viewer: backup metadata only, 24 admin endpoints 403, read endpoints 200, `storage_bytes` null; UI without content/diff/download/backup/search/run/acknowledge; ack page "admins only" via the webhook `ack_url` |
| `62-rtl-mobile-3` | **[3]** Hebrew at 1440 and 390 px: config text `ltr` in an `rtl` page, `/config-backups` `/flows` `/new-devices` `/reports` without horizontal overflow, IPs/MACs `ltr` |
| `63-assistant-settings` | **[3b]** Fresh install: assistant + 10 AI features OFF, USD prices, 409 except prepare endpoints; configure-first bottom switch locked until a Test in the session; `assistant_test_required`; bad secret -> `aws_auth_failed` (mock saw `InvalidSignatureException`); certificate upload (7 invalid cases, PEM, `.pfx`, metadata only, audited); CSR flow (server key, PKCS#10 download, signed by the test PKI, certificate-only upload); Roles Anywhere Test through the UI (X.509 signature verified by the mock), switch -> ON, region edit locks; il-central-1 needs `global.` (warning `global_inference_profile`); `test_id` bound to admin + values; audit dependency; rate limit 10/10 min; viewer + API tokens 403 |
| `64-assistant-ask` | **[3b]** Example chip -> streamed answer, tool step, links, sources, AI notice; Converse request shape (system prompt sentences, cachePoints, closed tool schemas, json toolResult); SSE contract (headers, ids, events, usage = mock usage); Copy, feedback; follow-up sends history; rename/delete; re-attach with `Last-Event-ID`; validation; Enter / Shift+Enter |
| `65-assistant-tools-features` | **[3b]** Tools follow features: syslog OFF/ON, config tools only with config_backup + send_config_excerpts + masking (no unmasked secret reaches the model), flows/traps/new devices/timeline; tool error -> `status: error` + retry; 3 rounds; 3 parallel tool calls |
| `66-assistant-errors-caps` | **[3b]** Throttling retried (1 s / 4 s) then error; 10 AWS error modes -> mapped codes + health state; UI banner + Retry (retryable only); Stop within 1 s (Bedrock stream closed); busy 409; stalled stream -> `assistant_timeout`; model-call / tool-call / max_tokens limits -> final no-tool call + `limit_reached`; daily cap; USD cost estimate + monthly cost cap; usage page = mock usage |
| `67-assistant-privacy` | **[3b]** Internal-IP redaction (placeholders to the model, real addresses in the UI, args translated back); seeded secret markers never sent; injection corpus via syslog with the "obeying" mock model: `update_device` -> `unknown_tool`, no write, no `<img>` / external href / forged ref links; audit rows without the question text |
| `68-explain-alert` | **[3b]** Explain on the dashboard -> drawer with the 5 sections, gathered input has the preceding syslog; cache (no model call), Refresh; follow-up in the explain conversation; feature OFF 409, viewer 403 |
| `69-correlation` | **[3b]** Parent down + 4 dependents unreachable -> one incident, deterministic title, root, AI summary (non-streamed); resolved on recovery; mock unavailable -> title only; graph refresh for newly added dependencies; feature OFF 409 |
| `70-alert-notes` | **[3b]** AI note after the alert: Telegram `editMessageText` (+ refused edit -> reply), Teams follow-up card, threaded email reply; once per problem, never on recovery; slower than the timeout -> skipped, alert on time; rule form checkbox |
| `71-config-diff-explain` | **[3b]** Comware `risk-a` -> `risk-b` (tests/sim/ssh/configs/comware): high flags `snmp_community_added`, `mgmt_acl_removed`, info `ntp_changed`; forced `report_diff_explanation` over the masked diff, cache; UI panel; mock error keeps flags; `feature_idle` |
| `72-syslog-digest` | **[3b]** 8 days of syslog through the receiver with the test clock; digest of D-1: spike unusual (x6.5), new message type with `nm:syslog` example, AI bullets, email; mock error -> statistics only; 7-day / 5-runs limits |
| `73-rule-drafts` | **[3b]** Alert-rule draft (delay 120, Telegram channel, nothing saved), repair round, UI Describe -> prefilled form -> normal save (audited); event-rule draft + Test; gates |
| `74-template-drafts` | **[3b]** snmpsim X100 (`tests/sim/bedrock/snmp_x100`, PEN 99999): forbidden subtrees refused / never walked, secrets redacted before the model, draft valid, UI wizard -> Save as user template -> applied to the device |
| `75-knowledge` | **[3b]** KB configure-first form + Test (mock Retrieve) + bottom switch; answer cites `kb/<n>` (https clickable, s3 text); KB error -> `kb_unavailable`; injected KB passage cannot exfiltrate; OFF |
| `76-chat-ask` | **[3b]** Telegram `/ask` from a linked admin: "Working on it…" edited to the escaped HTML answer + Full answer link, channel telegram; `/ask@bot`, IP placeholders stay in Telegram; viewer / unlinked / group refused; injected HTML escaped; busy; OFF |
| `77-report-summary` | **[3b]** AI paragraph (en/he) at the top of report data, email and page; numbers checked (unverified -> dropped); mock error -> report ok without it; OFF -> `disabled`; per-report opt-in |
| `78-assistant-rtl-mobile` | **[3b]** Hebrew question -> "Answer in Hebrew", rtl answer with ltr IPs; ask/settings/usage/incident pages at 1440 and 390 px without horizontal overflow |
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

## Phase 2b-1 lab (`tests/sim/lab.sh`, `LAB_2B1=1` default; architecture.md §10.4)

`run.sh` starts the backend with `NETMON_TEST_MODE=1` (`PUT /api/test/clock`) through a restartable script
(`$BACKEND_RESTART`), and exports `LAB_SH`/`LAB_DIR` so specs can `kill`/`revive`/`swap` single components:

```bash
tests/sim/lab.sh kill http-31          # parent outage (connection refused)
tests/sim/lab.sh revive http-31
tests/sim/lab.sh swap snmp-access-2 tests/sim/hpe/access2_noprinter   # LLDP row removed (stale test)
backend/.venv/bin/python tests/sim/fingerprint/fp_hosts.py --list    # fingerprint inventory
```

| Address | Simulates | Expected |
|---|---|---|
| `127.0.0.31–.36` tcp/8080 | toggle HTTP targets (`POST /fail`, `/ok`): .31 parent, .32/.33 children, .34 maintenance, .35 ack (+ specs 26/27), .36 escalation | — |
| `127.0.0.41` tcp/80 | `<title>TL-SG108E</title>`, body `TP-LINK Easy Smart` | switch / tp-link / `TL-SG108E` / medium (F7) |
| `127.0.0.42` tcp/443 | self-signed TLS, `302 /ui/` → `Login \| OPNsense` | router / opnsense / high, redirects 1 |
| `127.0.0.43` tcp/80 | `302 http://example.com/` | not followed, status 302 |
| `127.0.0.44` / `.45` tcp/22 | `SSH-2.0-Comware-7.1.064` / `SSH-2.0-ROSSSH` | switch/h3c/high (F9) / router/mikrotik/high (F3) |
| `127.0.0.46` tcp/23 | IAC negotiation + `User Access Verification` | banner without IAC bytes |
| `127.0.0.47` tcp/80 | `Server: HP HTTP Server; HP LaserJet …`, no title | printer / hp / medium (F17) |
| `127.0.0.48` tcp/8080 | 401 `realm="DGS-1210-28"`, 100 KiB body, marker `zyxel` after 64 KiB | realm captured, no `zyxel` marker |
| `127.0.0.49` tcp/80 | accepts, never answers | `error: "timeout"` |
| `127.0.0.61–.67` | QA extras: Aruba web (F10, `JL259A`), Cisco SSH (F24), FortiGate (F23), Synology (F15), iLO (F12), iDRAC (F13), nginx (no rule) | see `23-fingerprint` |
| `127.0.0.51` udp/1161 | ArubaOS-Switch 2930F `core-sw`: CPU 7, mem 39.39 %, PSU bad, RPS notPresent, PoE 45/370 W, LLDP | `hpe_arubaos_switch` |
| `127.0.0.52` udp/1161 | Comware 1950 `access-1`: CPU/mem/temp Board, Fan 2 fanError, PoE 0, LLDP port nums 101–104 | `hpe_comware` |
| `127.0.0.53` udp/1161 | ArubaOS 2530 `access-2` (minimal) + unmanaged neighbour `printer-3f` (00:11:22:33:44:55, 127.0.0.99) | variant `access2_noprinter` |
| `127.0.0.54` udp/1161 | UniFi U6-Lite `U6-Lite-Office` with LLDP on eth0 | AP → access-1 by sysName |

All SNMP agents are v2c, community `public` (`<dir>/public.snmprec`; an SNMPv3 agent would need
`self.snmprec`, since snmpsim selects data by context name and managers send an empty context). Quick checks:

```bash
snmpwalk -v2c -c public 127.0.0.51:1161 1.3.6.1.4.1.11.2.14.11.1.2.6.1.4     # hpicfSensorStatus 4,2,4,5
snmpwalk -v2c -c public 127.0.0.52:1161 1.3.6.1.4.1.25506.2.6.1.1.1.1.19     # ErrorStatus 1,2,41,2
snmpwalk -v2c -c public 127.0.0.53:1161 1.0.8802.1.1.2.1.4                  # lldpRemTable + ManAddr
curl -sk https://127.0.0.42/ -L ; curl -s -m2 http://127.0.0.49/ ; nc 127.0.0.44 22
```

## Phase 2b-2 lab (`tests/sim/lab.sh`, `LAB_2B2=1` default; architecture.md §11.6)

`run.sh` also passes `NETMON_TEST_PTR_FILE=tests/sim/ports/ptr.json` to the backend (reverse DNS test hook:
`10.0.0.11` -> `pc-reception.lan`, `10.0.0.12` -> `phone-12.lan`; no DNS stub) and exports `E2E_DATA_DIR`
(the backend's `NETMON_DATA_DIR`, for the user template directory) and `QA2B2_SHOT_DIR`.

**Addresses differ from the contract:** §11.6 puts the port sims on 127.0.0.61-.65, which are the 2b-1
fingerprint hosts that spec 23 scans with SNMP on; the lab uses .81-.85 with the contract data unchanged.

| Address (contract) | Simulates | Notes |
|---|---|---|
| `127.0.0.81` (.61) | ArubaOS-Switch 2930F `port-aos`: ports 1-10, `Trk1`, `DEFAULT_VLAN`; Q-BRIDGE static, no dot1dBasePortIfIndex, dot1q FDB (self entry, 7 MACs on trunk 9), PoE group 1, EtherLike duplex | variant `arubaos_nomac7` (presence test) |
| `127.0.0.82` (.62) | Comware `port-cw`: 1/0/1-4, Ten-Gig 49/50, member 2 `2/0/1-2`, `Vlan-interface1`, `NULL0`, `InLoopBack0`, `M-GigabitEthernet0/0/0`; dot1dBasePortIfIndex, fdbId 1010 = VLAN 10 | QA extra: LLDP on port 2 -> the 2b-1 UniFi AP `U6-Lite-Office` (.54) = AP tile |
| `127.0.0.83` (.63) | Aruba CX 6300M `port-cx`: no static VLAN table (current table), no dot1qPvid, no dot1q FDB (dot1d fallback), `mgmt` | |
| `127.0.0.84` (.64) | OPNsense-style ARP router `fw-arp`: ipNetToPhysicalTable (dynamic, static, invalid, local, IPv6 rows) | |
| `127.0.0.85` (.65) | `Linux gw2 5.10`: ipNetToMediaTable only (class server -> `arp_collect: on`) | |
| `127.0.0.86` | ArubaOS 52-port `port-aos52` (49-52 at 10 G) | QA extra (phone overflow test) |
| `127.0.0.71` / `.72` / `.73` | Check Point `gw-hq` (2620) / ClusterXL member `gw-cl1` (net-snmp Linux OID + svn subtree, haInstalled 1) / Aruba Instant `instant-vc` (3 APs, 5 clients) | |
| `127.0.0.101-.110` | synthetic sims for `aruba_cx`, `aruba_wifi`, `checkpoint_gaia`, `fortinet_fortigate`, `cisco_ios`, `paloalto_panos`, `mikrotik_routeros`, `apc_ups`, `hpe_ilo`, `dell_idrac` (every OID of the template, from the contract tables) | |

```bash
python3 tests/sim/ports/gen_snmprec.py && python3 tests/sim/vendors/gen_snmprec.py   # regenerate (committed)
tests/sim/lab.sh start && backend/.venv/bin/python -I tests/sim/verify_2b2.py           # OK: ... match the contract
tests/sim/lab.sh swap snmp-port-aos tests/sim/ports/arubaos_nomac7                     # MAC of port 7 gone
snmpwalk -v2c -c public 127.0.0.81:1161 1.3.6.1.2.1.17.7.1.4.3.1    # Q-BRIDGE static (PortList FD 80 = 1-6,8,9)
snmpwalk -v2c -c public 127.0.0.83:1161 1.3.6.1.2.1.17.4.3.1        # dot1d FDB fallback
snmpwalk -v2c -c public 127.0.0.84:1161 1.3.6.1.2.1.4.35.1.4        # ipNetToPhysicalPhysAddress
```

`verify_2b2.py` walks every 2b-2 agent with pysnmp, applies an independent QA implementation of the contract
parsers (portlist bit order, bridge-port mapping, VLAN sets, FDB Q-BRIDGE/BRIDGE, PoE mapping, ARP) and asserts
the §11.6 tables and the vendor/synthetic key values; it checks the simulators, not the product.

## Phase 3 simulators (architecture.md §13.11; started by `tests/e2e/run.sh` unless `E2E_PHASE3=0`)

| Simulator | What | Self-check |
|---|---|---|
| `sim/ssh/fake_device.py` | asyncssh device lab 127.0.0.111-.122 tcp/8022: 8 personalities (`personalities/*.yaml`: Comware, ArubaOS-Switch with "Press any key" + ANSI, AOS-CX, Check Point clish / bash, Cisco IOS with enable, FortiGate, MikroTik), paging until the paging command, `% Invalid input`, password + public key (`client_key`), `legacy-only` / `legacy-strict`, `hang-after-login`, `huge` (9 MiB), host-key rotation, configs `configs/<driver>/v1|v2.txt` with volatile lines and secrets, JSON-lines command log, HTTP control | `selftest.py` (reference client), `engine_check.py` (runs the PRODUCT session engine + normaliser against the lab), `expected.py` (oracle: normalised expected configs + contract-gap check) |
| `sim/flows/send_flows.py` | NetFlow v5 / v9 / IPFIX / sFlow v5 datagrams from scenario YAML (`scenarios/s1..s6`), template-first or data-first, options sampling, IPv6, bi-directional, egress, IPFIX varlen + enterprise fields, floods; `--expect` = exact 1-minute aggregates | `selftest.py` (independent decoder), `decoder_check.py` (PRODUCT decoders + MinuteAggregator), `bench.py` (records/s table for the PR) |
| `sim/oidc/mock_idp.py` | Entra-shaped OIDC IdP (discovery, authorize with login_hint, token with PKCE + secret, JWKS), users with app roles / groups / overage, failure modes | — |
| `sim/kdc/setup_kdc.sh`, `client_token.py` | MIT KDC realm `MNIDOM.TEST` (users jdoe/asmith/nogroup/olduser, HTTP/localhost + HTTP/127.0.0.1 keytab, RC4-only keytab), SPNEGO tokens via python-gssapi, NTLM / garbage / 48 KB fixtures | — |
| `sim/telegram_mock.py` (extended) | `getUpdates` long poll fed by `POST /_inject` (presses, `/link`), `answerCallbackQuery`, `editMessageReplyMarkup`, `deleteWebhook`, reply_markup validation, `--conflict` / `/_control` 409 | — |
| `sim/smtp_sink.py` | aiosmtpd sink + HTTP `/messages` (decoded subject, text/html parts, remote resources) | — |
| `sim/kiosk_client.py` | NOC screen client from any loopback source address: view, WS (auth, no-auth, late auth), probe, hammer | — |
| `sim/newdev/` | new-device access switch 127.0.0.90 (`run1` baseline, `run2` new MACs), lab component `snmp-newdev-access` | — |

```bash
S=$(mktemp -d); backend/.venv/bin/python tests/sim/ssh/fake_device.py --lab --state-dir $S --control-port 18622 &
backend/.venv/bin/python tests/sim/ssh/selftest.py && backend/.venv/bin/python -W ignore tests/sim/ssh/engine_check.py
backend/.venv/bin/python tests/sim/flows/selftest.py && backend/.venv/bin/python tests/sim/flows/decoder_check.py
tests/sim/kdc/setup_kdc.sh start /tmp/kdc && backend/.venv/bin/python tests/sim/kdc/client_token.py spnego --kdc-dir /tmp/kdc
```

## Phase 3b simulators (architecture.md §14.11; started by `tests/e2e/run.sh` unless `E2E_PHASE3B=0`)

| Simulator | What | Self-check |
|---|---|---|
| `sim/bedrock/mock_bedrock.py` | Amazon Bedrock mock (one process, HTTP + HTTPS with the test PKI): Converse / ConverseStream (own event-stream encoder), scripted model (`scenarios/*.yaml`, purpose marker + question regex + language, JSONPath-style templates, forced tools), 30 error/behaviour modes, Bedrock-like request validation, KB Retrieve, Roles Anywhere CreateSession (X.509 signature + chain), control plane, IMDSv2, container credentials; every AWS request verified with **botocore SigV4Auth**; request log `GET /_requests`. In-process: `MockBedrock` / `pytest_fixture.py`. Details: `sim/bedrock/README.md` | `pytest tests/sim/bedrock` (contract vectors, signatures, sessions, scenarios, modes, cancel detection) |
| `sim/bedrock/pki.py` | Test PKI: root -> issuing CA -> leaves (RSA/EC P-256/P-384, expired, expiring, wrong CA, CA:TRUE, no digitalSignature, RSA 1024), PKCS#1/SEC1/encrypted keys, `.pfx`, TLS server cert, `sign-csr` for the CSR flow | (self-test above) |
| `sim/bedrock/injection/` | Prompt-injection corpus (17 attacker texts per surface) + `injection_checks.py` (answer / DOM / Telegram / outgoing-request checks) | (self-test above) |
| `sim/bedrock/snmp_x100/` | snmpsim data of a device with an unknown enterprise subtree (PEN 99999), forbidden ARP/USM/community tables and a value with secrets | — |
| `sim/telegram_mock.py` (extended) | `editMessageText` (must exist in the chat, "not modified", HTML/length checks), `reply_to_message_id` validation, `/_sent`, `/_control {"edit_too_old": true}` | — |
| `sim/ssh/configs/comware/risk-a.txt`, `risk-b.txt` | Comware change adding `snmp-agent community write` and removing `ssh server acl 2001` (spec 71) | — |

```bash
backend/.venv/bin/python -m pytest tests/sim/bedrock -q
backend/.venv/bin/python tests/sim/bedrock/mock_bedrock.py --port 0 --https-port 0 --pki /tmp/bpki   # READY {...}
```

## Other suites

```bash
cd backend && TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres .venv/bin/pytest -q
cd frontend && npm test -- --run
```
