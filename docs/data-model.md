# NetworkMonitor — Data Model (Phase 1 + Phase 2a + Phase 2b-1 + Phase 2b-2 + Phase 2b-3 + Phase 3 + Phase 3b)
# NetworkMonitor — Data Model (Phase 1 + Phase 2a + Phase 2b-1 + Phase 2b-2 + Phase 2b-3 + Phase 3 + 1.3)

Sections and rows marked **[2a]** belong to the Phase 2a contract (auto-discovery, device templates,
`snmp_custom` / `snmp_disk` sensors, Telegram channel). Everything else is the Phase 1 contract and is unchanged.
Phase 2a schema changes are all in **migration `0003`** (see [§ Phase 2a migration](#phase-2a-migration-0003)).
Sections marked **[2b-1]** (dependencies, `unreachable`, maintenance, acknowledge, escalation, fingerprinting,
HPE switch templates, topology) are in [§ Phase 2b-1](#2b-1-phase-2b-1-additions), migration `0004`.
Sections marked **[2b-2]** (switch ports view, template library, vendor templates) are in
[§ Phase 2b-2](#2b-2-phase-2b-2-additions), migration `0005`.
Sections marked **[2b-3]** (feature toggles, traps, syslog, event rules, flapping, SIEM forwarding, LAG,
Active Directory, audit log, timeline, Teams, API tokens) are in [§ Phase 2b-3](#2b-3-phase-2b-3-additions),
migration `0006`.
Sections marked **[3]** (config backup, flows, new-device alerts, SSO, reports, NOC screens, chat acknowledge)
are in [§ Phase 3](#3-phase-3-additions), migration `0007`.
Sections marked **[3b]** (assistant on Amazon Bedrock and the AI features) are in
[§ Phase 3b](#3b-phase-3b-additions), migration `0009`.
Sections marked **[1.3]** (sensor impact, important ports, LAG degraded, template options, IRF) are in
[§ Release 1.3](#13-release-13-additions), migration `0008`.

PostgreSQL ≥ 14 holds the data, and TimescaleDB is optional (see [architecture.md §6](architecture.md)). Alembic
migrations own the schema.

## General rules

- Every timestamp is `timestamptz` and is stored in UTC. The API renders them as ISO-8601 with a `Z` suffix.
- Enum-like columns are `text` with a `CHECK` constraint, not PG enums, so later migrations stay cheap.
- `created_at` / `updated_at`: `timestamptz NOT NULL DEFAULT now()`. The ORM sets `updated_at` on update.
- IDs are `integer GENERATED ALWAYS AS IDENTITY`, except `events.id`, which is `bigint`.
- JSON columns are `jsonb NOT NULL DEFAULT '{}'`, unless the column is noted as nullable.

## Enums

| Name | Values |
|---|---|
| `status` | `up`, `warning`, `down`, `paused`, `unknown` |
| `role` | `admin`, `viewer` |
| `language` | `en`, `he` |
| `sensor_type` | `ping`, `snmp_system`, `snmp_cpu`, `snmp_memory`, `snmp_interface`, `http`, `tcp_port`, `dns`, **[2a]** `snmp_custom`, `snmp_disk` |
| `snmp_version` | `v1`, `v2c`, `v3` |
| `notification_type` | `email`, `webhook`, **[2a]** `telegram` |
| `event_kind` | `status_change`, `notification`, `system`, **[2a]** `discovery` |
| **[2a]** `device_class` | `router`, `switch`, `access_point`, `printer`, `nas`, `server`, `workstation`, `unknown` (`router` covers firewalls and gateways) |
| **[2a]** `scan_status` | `running`, `completed`, `cancelled`, `failed` |
| **[2a]** `scan_trigger` | `manual`, `scheduled` |

**Status severity** is used for rollups and sorting, from highest to lowest: `down` (4) > `warning` (3) >
`unknown` (2) > `up` (1) > `paused` (0). **[2b-1]** adds `unreachable` between `warning` and `unknown`
([§ enum changes](#2b-1-enum-changes)).

## Tables

### `users`
| Column | Type | Constraints |
|---|---|---|
| id | integer | PK |
| username | varchar(64) | NOT NULL, `^[A-Za-z0-9._-]{3,64}$`, unique index on `lower(username)` |
| email | varchar(255) | NULL |
| password_hash | text | NOT NULL (argon2id, argon2-cffi defaults) |
| role | text | NOT NULL CHECK in role, DEFAULT `viewer` |
| language | text | NOT NULL CHECK in language, DEFAULT `en` |
| is_active | boolean | NOT NULL DEFAULT true |
| token_version | integer | NOT NULL DEFAULT 0. Incremented on password change, role change, deactivation and logout-all. JWTs carry it. |
| last_login_at | timestamptz | NULL |
| created_at, updated_at | timestamptz | |

### `groups`
| Column | Type | Constraints |
|---|---|---|
| id | integer | PK |
| parent_id | integer | NULL, FK `groups.id` ON DELETE CASCADE |
| name | varchar(128) | NOT NULL. Unique index on `(COALESCE(parent_id, 0), lower(name))`. |
| description | text | NOT NULL DEFAULT '' |
| snmp_credentials | jsonb | **NULL** = inherit from the parent (see SNMP credentials) |
| sort_order | integer | NOT NULL DEFAULT 0 |
| created_at, updated_at | timestamptz | |

The API layer prevents cycles: if `parent_id` would make the group its own ancestor, the API returns
`400 group_cycle`.

### `devices`
| Column | Type | Constraints |
|---|---|---|
| id | integer | PK |
| group_id | integer | NOT NULL, FK `groups.id` ON DELETE CASCADE, index |
| name | varchar(128) | NOT NULL |
| host | varchar(255) | NOT NULL. A hostname, an IPv4 address or an IPv6 address. |
| description | text | NOT NULL DEFAULT '' |
| enabled | boolean | NOT NULL DEFAULT true. When false, all of the device's sensors are effectively paused. |
| snmp_credentials | jsonb | **NULL** = inherit from the group chain |
| sort_order | integer | NOT NULL DEFAULT 0 |
| created_at, updated_at | timestamptz | |

### `sensors`
| Column | Type | Constraints |
|---|---|---|
| id | integer | PK |
| device_id | integer | NOT NULL, FK `devices.id` ON DELETE CASCADE, index |
| name | varchar(128) | NOT NULL |
| type | text | NOT NULL CHECK in sensor_type. Immutable after creation. |
| config | jsonb | NOT NULL. The per-type schema below. |
| interval_s | integer | NOT NULL DEFAULT 60, CHECK `10 <= interval_s <= 86400` |
| enabled | boolean | NOT NULL DEFAULT true (false = paused by the user) |
| status | text | NOT NULL CHECK in status, DEFAULT `unknown` |
| status_message | text | NOT NULL DEFAULT '' (e.g. "Request timed out", "latency_ms 612 > 500") |
| last_check_at | timestamptz | NULL |
| last_status_change_at | timestamptz | NULL |
| state | jsonb | NOT NULL DEFAULT '{}'. Internal poller state (e.g. the last SNMP counters). **Never exposed by the API.** |
| sort_order | integer | NOT NULL DEFAULT 0 |
| impact | text | **[1.3]** NOT NULL DEFAULT `critical` CHECK in (`critical`,`warning`,`none`): contribution to the device status ([§ 1.3](#13-sensor-impact)) |
| created_at, updated_at | timestamptz | |

Index: `(status)`.

The stored `status` is `paused` whenever `enabled = false` or the device has `enabled = false`. The API writes
this in the same transaction as the pause. When a sensor resumes, its status is set to `unknown` until the next
check runs.

### `sensor_channels`
The rows are created automatically from the sensor type's channel definitions when a sensor is created.
Thresholds start from the defaults and can be overridden.

| Column | Type | Constraints |
|---|---|---|
| id | integer | PK |
| sensor_id | integer | NOT NULL, FK `sensors.id` ON DELETE CASCADE |
| key | varchar(64) | NOT NULL. UNIQUE `(sensor_id, key)`. |
| unit | varchar(16) | NOT NULL (copied from the definition). **[2a]** For `snmp_custom` it is copied from `config.unit` and updated when a PATCH changes `config.unit`. |
| warning_min | double precision | NULL |
| warning_max | double precision | NULL |
| down_min | double precision | NULL |
| down_max | double precision | NULL |
| last_value | double precision | NULL |
| status | text | NOT NULL CHECK in (`up`,`warning`,`down`,`unknown`) DEFAULT `unknown` |

**Threshold semantics** (each limit is optional, and a null limit is ignored):

| Channel status | Rule |
|---|---|
| `down` | `value > down_max` OR `value < down_min` |
| `warning` | otherwise, `value > warning_max` OR `value < warning_min` |
| `up` | otherwise |

**Sensor status** after a check:

- If the check itself failed (timeout, unreachable, auth error, HTTP keyword mismatch, unexpected status code,
  DNS expected answer mismatch, ...), the sensor is `down` and `status_message` holds the error.
- Otherwise the sensor takes the worst status among its channels. A channel that returned no value this run
  (e.g. `ssl_days_left` on plain HTTP) keeps `last_value = NULL`, gets `status = 'unknown'`, and is ignored in
  the worst-of calculation.

### `measurements` (time-series; a hypertable when Timescale is available)
| Column | Type | Constraints |
|---|---|---|
| time | timestamptz | NOT NULL |
| sensor_id | integer | NOT NULL. **No FK** (for hypertable/bulk-delete performance). |
| channel | varchar(64) | NOT NULL (= `sensor_channels.key`) |
| value | double precision | NOT NULL |

- Index: `(sensor_id, channel, time DESC)`. In fallback mode there is also a BRIN index on `(time)`.
- There is no primary key.
- One row is written per channel per successful check. A failed check writes no rows. Its status lives in
  `sensors` and `events`.
- When a sensor is deleted, a background task runs `DELETE FROM measurements WHERE sensor_id = $1`. Orphan rows
  are harmless and age out through retention anyway.

### `events`
| Column | Type | Constraints |
|---|---|---|
| id | bigint | PK |
| time | timestamptz | NOT NULL DEFAULT now() |
| kind | text | NOT NULL CHECK in event_kind |
| sensor_id | integer | NULL, FK `sensors.id` ON DELETE SET NULL |
| device_id | integer | NULL, FK `devices.id` ON DELETE SET NULL |
| group_id | integer | NULL, FK `groups.id` ON DELETE SET NULL (the device's group at event time) |
| sensor_name | varchar(128) | NULL. A snapshot, so the event stays readable after a delete. |
| device_name | varchar(128) | NULL. A snapshot. |
| old_status | text | NULL CHECK in status |
| new_status | text | NULL CHECK in status |
| message | text | NOT NULL DEFAULT '' |

Indexes: `(time DESC)`, `(sensor_id, time DESC)`, `(device_id, time DESC)`.

- `status_change`: written for every sensor status transition, including to and from `paused` and `unknown`.
- `notification`: a failed notification. The `message` holds the channel name and the error.
- `system`: e.g. "NetworkMonitor started", "TimescaleDB not available — using plain PostgreSQL".
- **[2a]** `discovery`: a scheduled discovery scan found new hosts, or a scheduled scan failed. All
  sensor/device/group columns are NULL. The new-hosts event is written for **every** completed, non-baseline
  scheduled scan with `new_hosts > 0`, **whether or not** the profile has `notify_new_hosts`; only the
  notifications (email/webhook/Telegram) are opt-in. Example message: `Discovery "LAN": 2 new hosts: 10.0.0.5 (printer, brother), 10.0.0.9`.

### `notification_channels`
| Column | Type | Constraints |
|---|---|---|
| id | integer | PK |
| name | varchar(128) | NOT NULL, unique |
| type | text | NOT NULL CHECK in notification_type |
| config | jsonb | NOT NULL (the schemas below) |
| enabled | boolean | NOT NULL DEFAULT true |
| created_at, updated_at | timestamptz | |

- `email` config: `{"to": ["ops@example.com", ...]}` (1–20 addresses). The SMTP server comes from `settings.smtp`.
- `webhook` config:
  `{"url": "https://...", "headers": {"X-Token": "..."}, "verify_tls": true, "timeout_s": 10}`.
  `headers` defaults to `{}`, `verify_tls` to true, and `timeout_s` (1–60) to 10. The method is always POST with
  a JSON body (the payload is in [api.md](api.md#webhook-payload)).
- **[2a]** `telegram` config:
  `{"bot_token": "123456789:AAE...", "chat_id": "-1001234567890", "parse_mode": "HTML", "disable_notification": false}`.
  - `bot_token`: **secret** (write-only, `bot_token_set` in responses). Required on create. Validated with
    `^\d{3,20}:[A-Za-z0-9_-]{20,100}$`.
  - `chat_id`: string. Either a numeric id `^-?\d{1,20}$` (negative for groups/supergroups/channels, e.g.
    `-1001234567890`) or a public channel/group username `^@[A-Za-z][A-Za-z0-9_]{3,31}$`.
  - `parse_mode`: `"HTML"` (default) or `null` (plain text).
  - `disable_notification`: bool, default false (Telegram "silent" message).
  - Sending is described in [api.md § Telegram](api.md#telegram-messages-2a).

### `alert_rules`
| Column | Type | Constraints |
|---|---|---|
| id | integer | PK |
| name | varchar(128) | NOT NULL |
| sensor_id | integer | **NULL = global rule** (all sensors). FK `sensors.id` ON DELETE CASCADE. |
| on_statuses | text[] | NOT NULL, non-empty, subset of {`down`,`warning`,`up`} |
| delay_s | integer | NOT NULL DEFAULT 0, CHECK `0 <= delay_s <= 86400` |
| enabled | boolean | NOT NULL DEFAULT true |
| created_at, updated_at | timestamptz | |

### `alert_notified`
| Column | Type | Constraints |
|---|---|---|
| rule_id | integer | FK `alert_rules.id` ON DELETE CASCADE |
| sensor_id | integer | FK `sensors.id` ON DELETE CASCADE |
| status | text | NOT NULL CHECK in (`down`,`warning`): the problem status last notified |
| time | timestamptz | NOT NULL DEFAULT now() |
| | | PK `(rule_id, sensor_id)` |

A row is written when a rule sent a `down`/`warning` notification and deleted when it sent the `up`.

### `alert_rule_channels`
| Column | Type | Constraints |
|---|---|---|
| rule_id | integer | FK `alert_rules.id` ON DELETE CASCADE |
| channel_id | integer | FK `notification_channels.id` ON DELETE CASCADE |
| | | PK `(rule_id, channel_id)` |

**Alert semantics:**

- A `status_change` event to a status S matches a rule when all of these hold:
  - the rule is enabled;
  - the rule's `sensor_id` is NULL or equal to the event's sensor;
  - S is in `on_statuses`.
- A transition to `up` fires for a rule only when that rule actually **sent** a `down`/`warning` notification
  for this sensor before (at least one channel succeeded). This is persisted in `alert_notified`, so it
  survives restarts, pauses and `unknown` in between, and a delayed rule whose problem recovered within the
  delay sends no orphan `up`.
- Transitions to `paused` or `unknown` never fire.
- With `delay_s > 0`, the engine schedules an in-memory timer. When it expires, the engine sends only if the
  sensor's current status is still S, nothing else changed the status in between, and the rule still exists,
  is enabled and still matches (its current channels are used). Pending timers are lost on
  restart, which is acceptable for the MVP. **[2b-1] Superseded:** delays, escalation and reminders are
  persisted in `alert_state` ([§ alert state](#2b-1-alert-state-escalation-reminders-ack)); `alert_notified` is
  replaced by it.
- One notification is sent per (rule, channel). If a global rule and a sensor rule both target the same channel
  for the same event, the engine sends it once.

### `settings`
Key/value store: `key varchar(64) PK`, `value jsonb NOT NULL`, `updated_at`. Every key has a code default, so a
missing row means the default applies.

| key | value schema | default |
|---|---|---|
| `smtp` | `{"host": str, "port": int, "security": "none"\|"starttls"\|"ssl", "username": str\|null, "password": str\|null, "from_address": str, "from_name": str}` | `{"host":"","port":587,"security":"starttls","username":null,"password":null,"from_address":"","from_name":"NetworkMonitor"}` |
| `retention_days` | int, 1–3650 | `90` |
| `event_retention_days` | int, 1–3650 | `365` |
| `default_interval_s` | int, 10–86400 | `60` |
| `default_language` | `"en"` \| `"he"` | `"en"` (used for notifications and the login page) |
| `public_base_url` | str \| null | `null` (used for links in notifications, e.g. `http://netmon.local:8080`) |

## SNMP credentials (jsonb in `groups.snmp_credentials` / `devices.snmp_credentials`)

```json
{
  "version": "v2c",
  "port": 161,
  "timeout_s": 2,
  "retries": 1,
  "community": "public",
  "v3_username": null,
  "v3_security_level": null,
  "v3_auth_protocol": null,
  "v3_auth_key": null,
  "v3_priv_protocol": null,
  "v3_priv_key": null
}
```

| Field | Values and limits |
|---|---|
| `version` | `v1`, `v2c`, `v3` |
| `port` | 1–65535 |
| `timeout_s` | 1–30 |
| `retries` | 0–5 |
| `community` | Used by v1/v2c only |
| `v3_security_level` | `noAuthNoPriv`, `authNoPriv`, `authPriv` |
| `v3_auth_protocol` | `MD5`, `SHA`, `SHA224`, `SHA256`, `SHA384`, `SHA512` |
| `v3_priv_protocol` | `DES`, `AES128`, `AES192`, `AES256` |
| `v3_auth_key`, `v3_priv_key` | Secret. 8–64 characters. |

- **Resolution:** use the device's credentials. If they are NULL, walk up the group chain and use the first
  non-null `snmp_credentials`. If none is found, use the built-in default:
  `{"version":"v2c","port":161,"timeout_s":2,"retries":1,"community":"public"}`.
- **Secret fields** (`community`, `v3_auth_key`, `v3_priv_key`, `settings.smtp.password`, **[2a]**
  `notification_channels.config.bot_token` (telegram), and the discovery `extra_communities` / `extra_v3[].v3_auth_key` /
  `extra_v3[].v3_priv_key`) are write-only in the API (see
  [api.md § Secret fields](api.md#secret-fields)). Phase 1 stores them in plaintext in the DB. The DB is not
  exposed outside the host or container network.

## Sensor types: config and channels

`poller/sensor_types.py` is the code source of truth and must match this section. `GET /api/sensor-types` serves
these definitions.

- **Config keys:** the API rejects unknown config keys (`422`) and fills omitted keys with their defaults. The
  stored config is always complete.
- **Primary channel:** the channel shown in the tree and lists.
- **Default thresholds:** written as `warning_max`/`down_max` and `warning_min`/`down_min`. "—" means null.

### `ping`
Config:

| Key | Type | Default | Limits |
|---|---|---|---|
| `count` | int | 4 | 1–20 |
| `timeout_ms` | int | 1000 | 100–10000, per packet |
| `packet_size` | int | 56 | 16–1472 |

Channels (primary: `latency_ms`):

| key | unit | meaning | warning_max | down_max | min limits |
|---|---|---|---|---|---|
| `latency_ms` | ms | average round-trip time (RTT) of the replies | 100 | 500 | — |
| `packet_loss_pct` | % | 0–100 | 10 | 50 | — |

A loss of 100 % means the check failed: the sensor goes `down` with "Host unreachable" and no rows are written.

### `snmp_system`
Config: `{}` (none).

Channels (primary: `uptime_s`):

| key | unit | meaning | thresholds |
|---|---|---|---|
| `uptime_s` | s | `sysUpTime` / 100 | none |

On success, `status_message` = `sysName — sysDescr` (the first 200 characters).

### `snmp_cpu`
Config: `{}`.

Channels (primary: `cpu_load_pct`):

| key | unit | meaning | warning_max | down_max |
|---|---|---|---|---|
| `cpu_load_pct` | % | average of all `hrProcessorLoad` rows (1.3.6.1.2.1.25.3.3.1.2) | 80 | 95 |

If the walk returns no rows, the check failed with "HOST-RESOURCES-MIB hrProcessorLoad not supported".

### `snmp_memory`
Config:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `storage_index` | int \| null | null | null = the first `hrStorageTable` row whose `hrStorageType` = hrStorageRam (1.3.6.1.2.1.25.2.1.2) |
| `storage_descr` | str | "" | Informational, filled by the backend |

Channels (primary: `mem_used_pct`):

| key | unit | meaning | warning_max | down_max |
|---|---|---|---|---|
| `mem_used_pct` | % | used / size × 100 | 85 | 95 |
| `mem_used_bytes` | B | hrStorageUsed × hrStorageAllocationUnits | — | — |
| `mem_total_bytes` | B | hrStorageSize × hrStorageAllocationUnits | — | — |

### `snmp_interface`
Config:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `if_index` | int | **required** | |
| `if_name` | str | "" | Informational |
| `speed_bps_override` | int \| null | null | When null, uses ifHighSpeed × 10⁶, else ifSpeed |

Channels (primary: `in_bps`):

| key | unit | meaning | warning_max | down_max |
|---|---|---|---|---|
| `in_bps` | bit/s | Δ ifHCInOctets × 8 / Δt | — | — |
| `out_bps` | bit/s | Δ ifHCOutOctets × 8 / Δt | — | — |
| `in_util_pct` | % | in_bps / speed × 100 (omitted when the speed is unknown or 0) | — | — |
| `out_util_pct` | % | out_bps / speed × 100 | — | — |
| `oper_status` | code | IF-MIB ifOperStatus: 1 = up, 2 = down, 3 = testing, … 7 = lowerLayerDown | — | **1** (anything other than up → down) |
| `in_errors_ps` | /s | Δ ifInErrors / Δt | — | — |
| `out_errors_ps` | /s | Δ ifOutErrors / Δt | — | — |

Counter handling:

- The previous counters and their timestamp are kept in `sensors.state`:
  `{"t": epoch_float, "in_octets": int, "out_octets": int, "in_errors": int, "out_errors": int, "hc": bool}`.
- The first poll after creation or a restart with an empty state stores the counters and writes only
  `oper_status`.
- When a 32-bit counter wraps (Δ < 0 with `hc` false), the delta becomes Δ + 2³². A negative Δ on 64-bit
  counters is treated as a counter reset: skip that rate and store the new baseline.
- If the device has no ifHC* (SNMP v1 or an old agent), the check falls back to ifInOctets/ifOutOctets
  (`hc: false`).

### `http`
Config:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `url` | str | **required** | `http(s)://...`. The literal `{host}` is replaced with the device host. |
| `method` | `"GET"` \| `"HEAD"` | `"GET"` | |
| `timeout_s` | int | 10 | 1–60 |
| `expected_status` | int[] \| null | null | null = any 2xx/3xx is OK |
| `keyword` | str \| null | null | |
| `keyword_must_exist` | bool | true | false = the keyword must NOT appear |
| `verify_tls` | bool | true | |
| `follow_redirects` | bool | true | |
| `headers` | object | {} | |

Channels (primary: `response_time_ms`):

| key | unit | meaning | warning | down |
|---|---|---|---|---|
| `response_time_ms` | ms | time to the full response | max 1000 | max 5000 |
| `status_code` | code | HTTP status | — | — |
| `ssl_days_left` | d | days until the cert's notAfter (https only, fractional) | **min 14** | **min 3** |

Failure cases (`down`): a connection or TLS error, a status that is not expected, or a keyword rule violation.
A TLS verification error with `verify_tls=true` is a failure. `ssl_days_left` is still read from the peer
certificate when it is reachable.

### `tcp_port`
Config:

| Key | Type | Default | Limits |
|---|---|---|---|
| `port` | int | **required** | 1–65535 |
| `timeout_s` | int | 5 | 1–60 |

Channels (primary: `connect_time_ms`):

| key | unit | warning_max | down_max |
|---|---|---|---|
| `connect_time_ms` | ms | 500 | 2000 |

A refused connection or a timeout → `down`.

### `dns`
Config:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `query_name` | str | **required** | |
| `record_type` | `"A"` \| `"AAAA"` \| `"CNAME"` \| `"MX"` \| `"NS"` \| `"TXT"` \| `"PTR"` | `"A"` | |
| `server` | str \| null | null | null = use **the device host** as the DNS server |
| `port` | int | 53 | |
| `timeout_s` | int | 5 | 1–30 |
| `expected` | str \| null | null | When set, at least one answer's text (rdata `to_text()`, trailing dot stripped, case-insensitive) must equal it |

Channels (primary: `resolve_time_ms`):

| key | unit | warning_max | down_max |
|---|---|---|---|
| `resolve_time_ms` | ms | 200 | 1000 |

These results → `down`: NXDOMAIN, no answer, a timeout, or an expected-answer mismatch.

### **[2a]** New config field type `float`
`config_fields[].type` gains `float`: a JSON number (int or float, not bool). `min`/`max` still apply and may be
fractional. `ConfigField.min`/`max` become `float | None` in code. Only `snmp_custom.multiplier` uses it.

### **[2a]** `snmp_custom`
One SNMP GET of a single OID, interpreted as a gauge, a counter rate, or a string match. `requires_snmp: true`.

Config:

| Key | Type | Default | Meaning / limits |
|---|---|---|---|
| `oid` | str | **required** | Numeric dotted OID, regex `^\.?[0-2](\.\d+){1,127}$`. A leading dot is stripped on save. MIB names are **not** accepted. |
| `value_type` | `"gauge"` \| `"counter"` \| `"string"` | `"gauge"` | See below |
| `unit` | str | `""` | 0–16 characters. Copied to `sensor_channels.unit` of channel `value`. |
| `multiplier` | float | `1.0` | Range `-1e12`…`1e12`, not 0. Applied to the raw value (gauge) or to the per-second rate (counter). E.g. `8` for octets → bit/s, `0.1` for tenths of a degree. |
| `label` | str | `""` | 0–64 characters. When non-empty, the success `status_message` is `"<label>: <value text>"`. |
| `percent_of_oid` | str_nullable | null | Gauge only (422 otherwise). When set, both OIDs are fetched in **one** GET and `value = raw / divisor × 100` (multiplier ignored). |
| `expected` | str_nullable | null | String only (ignored otherwise). |
| `match` | `"equals"` \| `"contains"` | `"equals"` | String only. Case-sensitive, on the decoded, trimmed text. |

Channels (primary: `value`):

| key | unit | thresholds |
|---|---|---|
| `value` | `config.unit` | none by default (the user or a template sets them) |

Evaluation:

- **Missing OID** (`noSuchObject`, `noSuchInstance`, `endOfMibView`, or v1 `noSuchName`) → check failed,
  message `OID not found: <oid>`.
- **`gauge`**: an integer value is used as is. An OctetString is decoded (UTF-8, errors replaced, NULs and
  whitespace stripped) and parsed with `^[-+]?\d+(\.\d+)?` (e.g. UCD `laLoad` = `"0.15"`). Not numeric → check
  failed, `Value is not numeric: '<first 40 chars>'`. Then `value = raw × multiplier`, rounded to 6 decimals.
  - With `percent_of_oid`: if `raw < 0` or `divisor <= 0` (e.g. Printer-MIB `-3` "some remaining", `-2`
    "unknown"), the check is **ok**, the channel has **no value** (`unknown`) and the message is
    `"<label or oid>: level not reported (raw=<raw>)"`. The sensor is then `up` (worst-of ignores `unknown`).
- **`counter`**: per-second rate like `snmp_interface`. State in `sensors.state`:
  `{"t": epoch_float, "v": int, "bits": 32 | 64}`. The first poll stores the baseline and writes no value.
  `bits` comes from the ASN.1 type of the response: `Counter32` → 32; `Counter64` → 64; any other integer type
  → 64. Δ < 0 with `bits = 32` → Δ + 2³²; Δ < 0 with `bits = 64` → reset (skip, new baseline). A change of
  `bits` between polls → new baseline. `value = Δ / Δt × multiplier`, rounded to 3 decimals. A non-integer
  response → check failed, `Value is not a counter`.
- **`string`**: the decoded text (integers are rendered with `str()`). With `expected = null` the check is ok
  and `value = 1`. With `expected` set: a match (`equals` / `contains`) → ok, `value = 1`; no match → check
  failed (`down`), message `"<label or oid>: '<got, first 80 chars>' does not match '<expected>'"`.
  On success, `status_message` = `"<label>: <text>"` (or just the text when `label` is empty), 200 chars max.
- A PATCH that changes `oid`, `value_type` or `percent_of_oid` resets `sensors.state` to `{}`.
- Backend note: `snmp_client` gains `snmp_get_typed(host, creds, oids) -> {oid: (value, asn1_type_name)}`
  (type names as pysnmp class names: `Counter32`, `Counter64`, `Gauge32`, `Integer32`, `Integer`,
  `OctetString`, `TimeTicks`, ...), used by `snmp_custom`.

### **[2a]** `snmp_disk`
HOST-RESOURCES-MIB `hrStorageTable` usage of one storage entry, selected **by description** on every poll
(indexes are not stable across reboots on some agents). `requires_snmp: true`.

Decision: a dedicated type rather than `snmp_custom`, because usage needs three columns of one row
(size, used, allocation units) and the row has to be found by description.

Config:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `storage_descr` | str | **required** | 1–255 characters. Matched against `hrStorageDescr` (1.3.6.1.2.1.25.2.3.1.3): first an **exact** match; only when `storage_descr` is a Windows drive (`^[A-Za-z]:\\?$`, e.g. `C:\`) and there is no exact match, the first row whose description **starts with** it (Windows reports `C:\ Label:...  Serial Number ...`). No other prefix matching, so `/` never matches `/boot`. Rows of type RAM / virtual memory are never matched. |
| `storage_index` | int_nullable | null | Informational. The backend writes the matched index through `config_update`. |

Channels (primary: `disk_used_pct`):

| key | unit | meaning | warning_max | down_max |
|---|---|---|---|---|
| `disk_used_pct` | % | used / size × 100 (2 decimals) | 85 | 95 |
| `disk_used_bytes` | B | hrStorageUsed × hrStorageAllocationUnits | — | — |
| `disk_free_bytes` | B | (size − used) × units | — | — |
| `disk_total_bytes` | B | hrStorageSize × hrStorageAllocationUnits | — | — |

- Each run walks `hrStorageDescr` (cap 500 rows) and `hrStorageType`, then GETs
  `hrStorageAllocationUnits` (.4), `hrStorageSize` (.5) and `hrStorageUsed` (.6) for the matched index.
- No match → check failed, `Storage '<storage_descr>' not found`. Size 0 → check failed, `Storage '<descr>' reports size 0`.
- Excluded types: hrStorageRam `1.3.6.1.2.1.25.2.1.2`, hrStorageVirtualMemory `1.3.6.1.2.1.25.2.1.3`.
- Default name: `Disk <storage_descr>` (e.g. `Disk /`).
- Negative `Integer32` values (agents that overflow on big disks) are converted with `+ 2³²`.

## Status rollup (computed on read; not stored)

- **Device status:**
  - if `devices.enabled = false` → `paused`;
  - else if it has no sensors → `unknown`;
  - else if all sensors are `paused` → `paused`;
  - else the highest-severity status among its non-paused sensors.
  - **[1.3]** each sensor contributes its status through its `impact` first ([§ 1.3 Sensor impact](#13-sensor-impact)).
- **Group status:** the highest-severity status over its direct devices and its child groups, with paused
  members excluded. A group with no members, or only paused members → `unknown` or `paused` respectively.
  A child group that has no members at all (no devices anywhere below it) is not counted as a member, so an
  empty sub-group does not turn its parent `unknown`.
- `status_counts` (in the API) for a device or group counts the **sensors** beneath it, recursively, by status.

---

# **[2a]** Phase 2a additions

## Discovery tables

### `discovery_profiles`
A saved scan definition. It can run on demand or on a simple schedule.

| Column | Type | Constraints |
|---|---|---|
| id | integer | PK |
| name | varchar(128) | NOT NULL, unique index on `lower(name)` |
| targets | text[] | NOT NULL, 1–64 entries (syntax in [§ Scan targets](#scan-targets)) |
| group_id | integer | NOT NULL, FK `groups.id` **ON DELETE CASCADE**. Supplies the effective SNMP credentials and is the default import group. |
| options | jsonb | NOT NULL. [Scan options](#scan-options), **including secrets** (plaintext, like device credentials). |
| schedule_enabled | boolean | NOT NULL DEFAULT false |
| schedule_every_hours | integer | NOT NULL DEFAULT 24, CHECK `1 <= schedule_every_hours <= 720`. The UI offers Daily (24), Weekly (168) and "every N hours". |
| notify_new_hosts | boolean | NOT NULL DEFAULT false |
| notify_channel_ids | integer[] | NOT NULL DEFAULT '{}'. Ids of `notification_channels`. The API validates that they exist. Ids deleted later are ignored at send time. |
| last_run_at | timestamptz | NULL. Start of the last scan of this profile (manual or scheduled). |
| next_run_at | timestamptz | NULL. NULL when `schedule_enabled = false`. |
| last_scan_id | integer | NULL (no FK; the scan may have been pruned) |
| created_at, updated_at | timestamptz | |

Scheduling:

- When `schedule_enabled` turns true, or `schedule_every_hours` changes while enabled:
  `next_run_at = now() + 1 minute`.
- A background loop (`netmon.discovery.scheduler`) wakes every 30 s. For each enabled profile with
  `next_run_at <= now()` it starts a scan (`trigger = scheduled`) and sets
  `next_run_at = now() + schedule_every_hours`. If another scan is running, it does **not** start; it sets
  `next_run_at = now() + 10 minutes` and logs at info.
- Any scan of the profile sets `last_run_at` and `last_scan_id` when it starts.
- **Baseline rule:** a scheduled scan whose profile had `last_run_at IS NULL` before it started sends no
  new-host notification (otherwise the first run reports every host as new).

### `discovery_scans`
| Column | Type | Constraints |
|---|---|---|
| id | integer | PK |
| profile_id | integer | NULL, FK `discovery_profiles.id` ON DELETE SET NULL |
| profile_name | varchar(128) | NULL. Snapshot. |
| trigger | text | NOT NULL CHECK in scan_trigger |
| status | text | NOT NULL CHECK in scan_status |
| group_id | integer | NULL, FK `groups.id` ON DELETE SET NULL |
| targets | text[] | NOT NULL |
| options | jsonb | NOT NULL. The options used, **with secrets removed** (`extra_communities` → `[]`, v3 keys → null, each `extra_v3` entry keeps `v3_auth_key_set` / `v3_priv_key_set` so the API can report them), plus `extra_communities_count`, `extra_v3_count`. |
| credentials | jsonb | NOT NULL DEFAULT '[]'. **Internal, never returned by the API.** The ordered list of full SNMP credential objects tried (index 0 = the group's effective credentials, with the `snmp_port` override applied). Used by import. |
| total_hosts | integer | NOT NULL |
| scanned_hosts | integer | NOT NULL DEFAULT 0 |
| found_hosts | integer | NOT NULL DEFAULT 0 |
| new_hosts | integer | NOT NULL DEFAULT 0 |
| error | text | NULL (set when `failed`) |
| created_by | integer | NULL, FK `users.id` ON DELETE SET NULL (NULL for scheduled scans) |
| started_at | timestamptz | NOT NULL DEFAULT now() |
| finished_at | timestamptz | NULL |

- Index: `(started_at DESC)`. A partial unique index `ux_discovery_scans_running ON discovery_scans ((true)) WHERE status = 'running'`
  enforces **at most one running scan**.
- **Restart:** at startup (after migrations) every `running` scan becomes `failed` with
  `error = 'Interrupted by restart'` and `finished_at = now()`.
- **Retention:** when a scan finishes, delete all scans except the newest **20** by `id` (the running scan is
  never deleted). Results cascade.
- `scanned_hosts` counts hosts whose probing finished (found or not). It is flushed to the DB at most once per
  second and on finish.

### `discovery_results`
One row per **found** host (a host is found when ping answered, SNMP answered, or at least one TCP port was open).

| Column | Type | Constraints |
|---|---|---|
| id | integer | PK |
| scan_id | integer | NOT NULL, FK `discovery_scans.id` ON DELETE CASCADE |
| ip | inet | NOT NULL. UNIQUE `(scan_id, ip)`. |
| hostname | varchar(255) | NULL. Reverse DNS (PTR, trailing dot stripped), or the target name when the target was a hostname. When a hostname target resolves to an address another target already found, the first row written wins (UNIQUE `(scan_id, ip)`), so `hostname` may be the PTR name. |
| mac | varchar(17) | NULL. Best effort, lower-case `aa:bb:cc:dd:ee:ff` (see architecture). |
| ping_ok | boolean | NOT NULL |
| ping_rtt_ms | double precision | NULL |
| snmp_ok | boolean | NOT NULL |
| snmp_version | text | NULL CHECK in snmp_version |
| snmp_credential_index | integer | NULL. Index into `discovery_scans.credentials`. **Internal.** |
| sys_name | varchar(255) | NULL |
| sys_descr | text | NULL (first 1000 characters) |
| sys_object_id | varchar(255) | NULL |
| open_ports | integer[] | NOT NULL DEFAULT '{}' (sorted ascending) |
| device_class | text | NOT NULL CHECK in device_class |
| vendor | varchar(32) | NULL. A vendor slug from the [vendor table](#vendor-table). |
| suggested_template | varchar(32) | NOT NULL. A template id. |
| is_new | boolean | NOT NULL. See [new hosts](#new-hosts). |
| imported_device_id | integer | NULL, FK `devices.id` ON DELETE SET NULL. Set by import. |
| created_at | timestamptz | NOT NULL DEFAULT now() |

`existing_device_id` is **not stored**. The API computes it on read (and when the WS `discovery.result` is
sent): the smallest `devices.id` with `host = host(ip)` or (hostname not null and `lower(host) = lower(hostname)`).

### `discovery_known_hosts`
Every IP any finished (completed or cancelled) scan has found. Used to decide "new".

| Column | Type | Constraints |
|---|---|---|
| ip | inet | PK |
| first_seen_at | timestamptz | NOT NULL DEFAULT now() |
| last_seen_at | timestamptz | NOT NULL DEFAULT now() |
| last_scan_id | integer | NULL (no FK) |

#### New hosts
At the time a result row is written, `is_new = (ip NOT IN discovery_known_hosts) AND (no existing device matches)`.
When the scan finishes (completed or cancelled), all its result IPs are upserted into `discovery_known_hosts`.
`discovery_scans.new_hosts` = count of `is_new` rows. Known hosts are never pruned (one row per IP).

## Scan targets
Each entry of `targets` is one of:

| Form | Example | Rules |
|---|---|---|
| IPv4 CIDR | `192.168.1.0/24` | Prefix `/8`–`/32`. Host bits set are normalised (`192.168.1.5/24` → `192.168.1.0/24`). For prefixes ≤ /30 the network and broadcast addresses are excluded. |
| IPv4 range, short | `192.168.1.10-50` | Last octet range, start ≤ end ≤ 255 |
| IPv4 range, full | `192.168.1.10-192.168.2.20` | start ≤ end |
| Single IPv4 / IPv6 | `10.0.0.1`, `fd00::1` | IPv6 only as single addresses (no IPv6 CIDR/range) |
| Hostname | `nas.lan` | RFC 1123, ≤ 253 chars. Resolved at scan time (first A, else AAAA). Not resolvable → not found (counts as scanned). |

- Rejected addresses (whole request → `422 validation_error`, loc `["body","targets",i]`): multicast
  (`224.0.0.0/4`, `ff00::/8`), `0.0.0.0/8`, `240.0.0.0/4` (includes `255.255.255.255`), unspecified `::`,
  IPv6 with a **scope id** (`fe80::1%eth0`), and **IPv4-mapped IPv6** (`::ffff:0:0/96`, e.g.
  `::ffff:10.0.0.1` — give the IPv4 address instead). Loopback **is allowed** (QA simulators use `127.0.0.x`).
- A hostname target whose resolved address falls in any rejected class above (including a scope id or an
  IPv4-mapped address) is treated as not resolvable: not found, counts as scanned.
- Duplicates across entries are removed. **Total after expansion ≤ 4096**, else `422 too_many_hosts`
  (details `[{"count": 65534, "max": 4096}]`). A `/20` (4094 hosts) fits.
- Hosts are scanned in ascending address order (hostnames last, in given order).

## Scan options
Stored in `discovery_profiles.options`, sent in scan requests. Every key is optional on input; omitted keys
take the defaults. Unknown keys → 422.

| Key | Type | Default | Meaning / limits |
|---|---|---|---|
| `ping` | bool | true | ICMP echo, 1 packet |
| `ping_timeout_ms` | int | 1000 | 100–5000 |
| `snmp` | bool | true | SNMP probe |
| `snmp_port` | int \| null | null | 1–65535. null = the port of the group's effective credentials. When set, **every** credential tried uses this port (QA uses 1161). |
| `snmp_timeout_s` | int | 1 | 1–10, per credential attempt (retries = 0 during discovery) |
| `extra_communities` | str[] | [] | **Secret.** 0–5 entries, 1–64 chars each. Tried as `v2c` after the group credentials. |
| `extra_v3` | object[] | [] | **Secret keys.** 0–3 entries: `{v3_username, v3_security_level, v3_auth_protocol, v3_auth_key, v3_priv_protocol, v3_priv_key}` with the same rules as SNMP credentials. |
| `tcp` | bool | true | TCP connect probe |
| `tcp_ports` | int[] | `[22, 23, 53, 80, 443, 445, 631, 3389, 8080, 8443, 9100]` | 1–32 distinct ports, 1–65535 |
| `tcp_timeout_ms` | int | 1000 | 100–5000 |
| `dns` | bool | true | Reverse DNS for found hosts |

At least one of `ping`, `snmp`, `tcp` must be true (422 otherwise).

**Credential order** (stops at the first that answers): 1) the target group's effective credentials (device →
group chain → built-in default, see SNMP credentials), 2) each `extra_communities` entry as v2c, 3) each
`extra_v3` entry. All use `snmp_port` when set, `timeout_s = snmp_timeout_s`, `retries = 0`. Max 9 attempts per host, all within the **25 s SNMP budget** per host (attempts not started
within the budget are skipped). Ping and TCP results are kept regardless ([architecture.md § 9.2](architecture.md#92-scan-execution)).

## Classification
Inputs per host: `sys_object_id`, `sys_descr`, `sys_name`, open ports, and up to three **subtree probes**.
After a successful SNMP GET of `sysDescr.0` (1.3.6.1.2.1.1.1.0), `sysObjectID.0` (1.3.6.1.2.1.1.2.0) and
`sysName.0` (1.3.6.1.2.1.1.5.0), the scanner sends **one GETNEXT** with these varbinds and records which ones
return an OID that is still inside the requested subtree:

| Probe | OID | Means |
|---|---|---|
| `unifi_vap` | `1.3.6.1.4.1.41112.1.6` | UBNT-UniFi-MIB `unifiApSystem`/VAP table present → UniFi AP |
| `printer_mib` | `1.3.6.1.2.1.43` | Printer-MIB present → printer |
| `synology` | `1.3.6.1.4.1.6574` | SYNOLOGY-SYSTEM-MIB present → Synology NAS |
| `qnap` | `1.3.6.1.4.1.24681` | QNAP NAS-MIB present → QNAP NAS |

For FreeBSD agents (sysObjectID `1.3.6.1.4.1.8072.3.2.8`, or sysDescr starting with `FreeBSD`) the scanner
also walks `ifDescr` (1.3.6.1.2.1.2.2.1.2, max 200 rows) and sets `pf = true` when a row equals `pflog0`.

`enterprise` = the arc right after `1.3.6.1.4.1.` in `sys_object_id`. Regexes are case-insensitive.
**Rules are evaluated in order; the first match sets class and vendor.** Inside the table, `\|` is a regex `|` escaped for
Markdown. Vendor slugs outside the vendor table that rules can produce: `opnsense`, `pfsense`, `freebsd`, `linux`, `apple`.

| # | Condition | device_class | vendor |
|---|---|---|---|
| 1 | `/opnsense/` in sysDescr or sysName, or (sysDescr starts with `FreeBSD` and matches `/stable\/\d{2}\.\d/`) | router | opnsense |
| 2 | `/pfsense/` in sysDescr or sysName, or enterprise = 12325 | router | pfsense |
| 3 | probe `unifi_vap`, or sysDescr `/\b(UAP\|U6\|U7\|UAL\|UAE)[-\w]*/` (case-sensitive) or `/unifi.*(ap\b\|access point)/` | access_point | ubiquiti |
| 4 | enterprise 41112 or sysDescr `/\bUSW\|\bUS-\d\|unifi.*switch/` → if sysDescr `/\bUSW\|\bUS-\d\|switch/`: switch; elif `/EdgeOS\|EdgeRouter\|UDM\|UXG\|USG\|UniFiOS/`: router; else unknown | (as stated) | ubiquiti |
| 5 | probe `printer_mib`, or enterprise ∈ {2435, 1602, 1248, 253, 367, 1347, 641}, or sys_object_id starts with `1.3.6.1.4.1.11.2.3.9.` | printer | from vendor table (HP → hp) |
| 6 | probe `synology` or enterprise 6574 or sysDescr `/synology\|diskstation/` | nas | synology |
| 7 | probe `qnap` or enterprise 24681 | nas | qnap |
| 8 | enterprise 311 (Microsoft) | sysDescr `/server/` → server, else workstation | microsoft |
| 9 | FreeBSD with `pf = true` | router | freebsd |
| 10 | enterprise in the vendor table with a plain (not parenthesised) default class | sysDescr `/switch\|catalyst\|procurve\|nexus\|jetstream\|\bCRS\d\|\bEX\d{4}\|\bC(2\|3\|9)\d{3}\|\b(GS\|SG\|XS)\d{3}/` → switch; `/\b(access point\|aironet)\b\|\bAP\d/` → access_point; else the vendor's default class | from table |
| 11 | sys_object_id starts with `1.3.6.1.4.1.8072.3.2.` (net-snmp): `.10` → server/linux, `.8` → server/freebsd, `.13` → workstation/microsoft, `.16` → workstation/apple, other → server/null | (as stated) | (as stated) |
| 12 | any other SNMP answer: sysDescr `/switch/` → switch, `/router\|routeros\|gateway/` → router, `/printer\|jetdirect/` → printer, `/linux\|unix\|bsd/` → server, `/windows/` → workstation; else unknown | (as stated) | vendor table by enterprise, else null |
| 13 | no SNMP, ports: 9100 or 631 open → printer | printer | null |
| 14 | no SNMP: 3389 open → workstation (server if 53 also open) | workstation/server | null |
| 15 | no SNMP: 53 open and (80 or 443 open) → router | router | null |
| 16 | no SNMP: 22 open → server | server | null |
| 17 | no SNMP: 445 open → workstation | workstation | null |
| 18 | otherwise | unknown | null |

The port rules (13–18) are heuristics; the UI lets the user pick any template before import.

### Vendor table
`enterprise` → vendor slug. Confidence: **high** = verified from IANA assignments / common agent output as far
as the architect knows; **medium** = believed correct, verify with a real device if it matters.

| Enterprise | Vendor slug | Default class (rule 10) | Confidence | Notes |
|---|---|---|---|---|
| 9 | cisco | router | high | |
| 11 | hp | switch | high | HP/HPE incl. ProCurve (`.11.2.3.7.*`) and JetDirect printers (`.11.2.3.9.*`, rule 5) |
| 171 | d-link | switch | high | |
| 253 | xerox | (printer) | high | |
| 311 | microsoft | (rule 8) | high | |
| 318 | apc | unknown | high | UPS / PDU |
| 367 | ricoh | (printer) | high | |
| 641 | lexmark | (printer) | high | |
| 674 | dell | server | high | Dell servers/switches |
| 890 | zyxel | switch | high | |
| 1248 | epson | (printer) | high | |
| 1347 | kyocera | (printer) | medium | |
| 1602 | canon | (printer) | high | |
| 2011 | huawei | router | high | |
| 2435 | brother | (printer) | high | |
| 2636 | juniper | router | high | |
| 4413 | broadcom | switch | high | FASTPATH-based switches (some Netgear, Ubiquiti EdgeSwitch) |
| 4526 | netgear | switch | high | |
| 6574 | synology | (nas) | high | |
| 8072 | (net-snmp) | (rule 11) | high | Generic agent; vendor comes from sysDescr. OPNsense with os-net-snmp and UniFi APs often report `1.3.6.1.4.1.8072.3.2.10`/`.8`. |
| 11863 | tp-link | switch | high | Managed TP-Link only; Easy Smart switches have no SNMP |
| 12325 | pfsense | (rule 2) | high | Actually Fraunhofer FOKUS "begemot" — `bsnmpd`, used by pfSense and FreeBSD's base `bsnmpd`. Treated as pfSense when sysDescr does not say otherwise. |
| 12356 | fortinet | router | high | |
| 14823 | aruba | access_point | high | Aruba Networks (pre-HPE) |
| 14988 | mikrotik | router | high | |
| 24681 | qnap | (nas) | high | |
| 25461 | paloalto | router | high | |
| 41112 | ubiquiti | (rules 3–4) | high | |

net-snmp agent sub-OIDs under `1.3.6.1.4.1.8072.3.2.`: `8` = FreeBSD, `10` = Linux, `13` = Windows (win32),
`16` = macOS (confidence: high for 8/10, medium for 13/16).

**OPNsense detection** (the owner's firewall runs `os-net-snmp`, i.e. net-snmp): it reports
`sysObjectID = 1.3.6.1.4.1.8072.3.2.8` and `sysDescr` like
`FreeBSD OPNsense.localdomain 14.1-RELEASE-p5 FreeBSD 14.1-RELEASE-p5 stable/24.7-n267... amd64`. Rule 1 catches
it by the word `OPNsense` (usually the default hostname) or by the `stable/NN.N` build tag. If the host was
renamed and the tag is absent, rule 9 (`pflog0` interface) still classifies it as `router` / `freebsd`, and the
suggested template is still `opnsense`.

**UniFi AP detection** (controller-managed APs with SNMP enabled in the UniFi Network application): rule 3, by
the `unifi_vap` subtree probe (most reliable; works even when sysObjectID is the net-snmp Linux OID) **or** by
sysDescr/model patterns (`UAP-AC-Pro`, `U6-Lite`, `U7-Pro`, ...) **or** enterprise 41112 + AP pattern.

## Device templates (built-in, code)
Templates live in `netmon/templates/builtin.py` as data and are read-only. The API shape is in
[api.md § Device templates](api.md#device-templates-2a). Each template has:

- `id` (slug), `name` / `description` (`{"en": ..., "he": ...}`), `requires_snmp`, `priority` (int, higher wins
  for suggestions), `match` (list of rules), `entries` (what to create).
- **Match rule:** `{"device_classes": [..] | null, "vendors": [..] | null, "requires_snmp": bool, "any_open_ports": [..] | null}`.
  A rule matches a discovery result when every non-null field matches (`requires_snmp: true` needs
  `snmp_ok`; `vendors` matches `vendor`; `any_open_ports` needs at least one of them open). A template matches
  when any of its rules matches. **Suggested template** = the matching template with the highest priority.
  `generic_ping` matches everything, so a suggestion always exists.

### Entry kinds
| kind | Fields | Behaviour at apply time |
|---|---|---|
| `sensor` | `type`, `name` (en/he), `config`, `channels` (threshold overrides, as in POST /api/sensors), `interval_s` (int \| null = default), `probe` (bool) | Creates one sensor. With `probe: true`, the backend first runs the check once (exactly like `POST /api/sensors/test`) and creates the sensor only if the result has `ok: true`. |
| `snmp_interfaces` | `oper_up_only` (bool), `exclude_if_types` (int[]), `include_name_regex` (str \| null), `exclude_name_regex` (str \| null), `max` (int), `channels` | Enumerates interfaces like `GET /api/devices/{id}/snmp/interfaces`. Keeps interfaces with `oper_status = up` (if `oper_up_only`), `type` not in `exclude_if_types`, name (ifName, else ifDescr) matching include and not matching exclude (Python `re.search`, case-insensitive). Sorted by `if_index`; the first `max` are created as `snmp_interface` with `{"if_index", "if_name"}` and name = if_name. |
| `snmp_table` | `walk_oid`, `label_oid` (\| null), `label2_oid` (\| null), `percent_of_column` (\| null), `name` (en/he, with `{label}` and `{index}` placeholders), `config` (snmp_custom config without `oid`/`percent_of_oid`/`label`), `channels`, `max` | Walks `walk_oid`. For each row suffix `S` (sorted, first `max`): creates an `snmp_custom` sensor with `oid = walk_oid + "." + S`, `percent_of_oid = percent_of_column + "." + S` (when set), `label` = text of `label_oid.S` (+ `" (" + label2_oid.S + ")"` when label2 is set and non-empty), falling back to `S`. **Unique labels (2b-2, owner's IRF test):** when several selected rows share a label, and `label_oid` is ENTITY-MIB `entPhysicalName`, each gets `" · " + unit`, where unit is the chassis (`entPhysicalClass` 3) it sits in via `entPhysicalContainedIn`: the chassis name when all chassis names differ, else `Unit n` / `יחידה n` (chassis in index order); only when the device has more than one chassis. Labels still shared then get `" #n"`. Re-applying renames an existing sensor (same identity) whose name is still the old ambiguous one; a name the user changed is kept (skip reason `exists`, message `Renamed`). Name = `name` with `{label}`/`{index}` substituted, cut to 128 chars. |

### Idempotency ("apply" adds only what is missing)
A template entry is skipped with reason `exists` when the device already has a sensor with the same identity:

| type | identity |
|---|---|
| `ping`, `snmp_system`, `snmp_cpu`, `snmp_memory` | the type |
| `snmp_interface` | `config.if_index` |
| `snmp_disk` | `config.storage_descr` |
| `snmp_custom` | **[2b-1]** `(type, config.oid, config.percent_of_oid)` (leading dots stripped; a missing/empty `percent_of_oid` is null). Phase 2a was `config.oid` alone; existing sensors without `percent_of_oid` keep matching. Needed because the HPE templates read the PoE consumption column both as a percentage and in W. |
| `http` | `config.url` (exact string, before `{host}` substitution) |
| `tcp_port` | `config.port` |
| `dns` | `(config.query_name, config.record_type)` |

Skip reasons (enum `skip_reason`): `exists`, `probe_failed`, `snmp_error` (an enumeration/walk failed), `max_reached`
(rows beyond `max`, reported once per entry with the count in `message`), `no_rows` (walk returned nothing).

Sensor names use the requesting user's language (`users.language`); scheduled contexts never apply templates.

### Built-in templates

`IF_EXCLUDE` (the default `exclude_name_regex`):
`^(lo\d*|pflog\d*|pfsync\d*|enc\d*|gif\d*|gre\d*|ovpn[cs]?\d*|wg\d+|tun\d*|tap\d*|ipfw\d*|docker\d*|veth.*|virbr.*|br-.*|dummy\d*|sit\d*|ifb\d*|imq\d*|teql\d*|ip6tnl\d*|wifi\d+|ath\d+|ra\d+|rai\d+)$`
and `exclude_if_types = [24]` (softwareLoopback).

| id | name en / he | priority | match | entries |
|---|---|---|---|---|
| `generic_ping` | Ping only / פינג בלבד | 0 | `{}` (everything) | Ping |
| `http_server` | Web server / שרת אינטרנט | 10 | no-SNMP rule: `any_open_ports [80,443,8080,8443]` | Ping; HTTP `http://{host}/` probe; HTTPS `https://{host}/` (`verify_tls: false`) probe; HTTP 8080 `http://{host}:8080/` probe; HTTPS 8443 `https://{host}:8443/` (`verify_tls: false`) probe |
| `generic_snmp` | Generic SNMP device / התקן SNMP כללי | 20 | `requires_snmp` | Ping; System; CPU Load (probe); Memory (probe); `snmp_interfaces` (up only, IF_EXCLUDE, max 32) |
| `nas_server` | Server / NAS | 40 | `requires_snmp`, classes `[server, nas, workstation]` | Ping; System; CPU Load (probe); Memory (probe); Disk `/` (`snmp_disk`, probe); Disk `/volume1` (probe; Synology/QNAP data volume); Disk `C:\` (probe; Windows); `snmp_interfaces` (up only, IF_EXCLUDE, max 8) |
| `printer` | Printer / מדפסת | 60 | `requires_snmp`, classes `[printer]` | Ping; System; `snmp_table` toner/supplies; Page counter (probe); HTTP `http://{host}/` probe |
| `unifi_ap` | UniFi access point / נקודת גישה UniFi | 80 | `requires_snmp`, classes `[access_point]`, vendors `[ubiquiti]` | Ping; System; CPU Load (probe); Memory (probe); `snmp_interfaces` uplink (`include_name_regex: ^eth0$`, up only, max 1); `snmp_table` clients per VAP |
| `opnsense` | OPNsense / pfSense firewall / חומת אש OPNsense / pfSense | 90 | `requires_snmp`, vendors `[opnsense, pfsense]`; and `requires_snmp`, classes `[router]`, vendors `[freebsd]` | Ping; System; CPU Load (probe); Memory (probe); Disk `/` (probe); `snmp_interfaces` (up only, IF_EXCLUDE, max 16); Web GUI `https://{host}/` (`verify_tls: false`, probe) |

Hebrew names for the other generic templates: `generic_snmp` = "התקן SNMP כללי", `nas_server` = "שרת / NAS".

Entry details:

- **Printer supplies** (`snmp_table`): `walk_oid` = prtMarkerSuppliesLevel `1.3.6.1.2.1.43.11.1.1.9`,
  `label_oid` = prtMarkerSuppliesDescription `1.3.6.1.2.1.43.11.1.1.6`, `percent_of_column` =
  prtMarkerSuppliesMaxCapacity `1.3.6.1.2.1.43.11.1.1.8`, name `"Supply {label}"` / `"חומר מתכלה {label}"`,
  config `{"value_type": "gauge", "unit": "%"}`, channels `[{"key":"value","warning_min":15,"down_min":5}]`, max 8.
  Row suffixes are two arcs (`hrDeviceIndex.supplyIndex`, e.g. `1.1`). Confidence: high.
- **Page counter** (`sensor`, `snmp_custom`): prtMarkerLifeCount `1.3.6.1.2.1.43.10.2.1.4.1.1`,
  `value_type: gauge`, unit `pages`, label `Pages`, interval 3600. Confidence: high.
- **UniFi clients per VAP** (`snmp_table`): `walk_oid` = unifiVapNumStations
  `1.3.6.1.4.1.41112.1.6.1.2.1.8` (confidence **high**), `label_oid` = unifiVapEssId
  `1.3.6.1.4.1.41112.1.6.1.2.1.6` (confidence **high**), `label2_oid` = unifiVapRadio
  `1.3.6.1.4.1.41112.1.6.1.2.1.9` (values like `ng` / `na`; confidence **medium**). Name `"Clients {label}"` /
  `"לקוחות {label}"`, config `{"value_type": "gauge", "unit": "clients"}`, no thresholds, max 16.
  Example result: `Clients HomeWiFi (na)`. One VAP row exists per SSID per radio.
- Template sensor names (en / he): Ping / פינג, System / מערכת, CPU Load / עומס מעבד, Memory / זיכרון,
  `Disk {descr}` / `דיסק {descr}`, HTTP / HTTP, HTTPS / HTTPS, `HTTP 8080`, `HTTPS 8443`, Web GUI / ממשק ניהול,
  Page counter / מונה דפים, interfaces = if_name.

## Phase 2a migration (`0003`)
File `backend/netmon/migrations/versions/0003_phase2a.py`, `revision = "0003"`, `down_revision = "0002"`.
Plain PostgreSQL DDL only.

1. Widen CHECK constraints (inline column checks from 0001 have the PostgreSQL default names):
   - `ALTER TABLE sensors DROP CONSTRAINT sensors_type_check`, re-add `sensors_type_check` with the 10 types.
   - `ALTER TABLE notification_channels DROP CONSTRAINT notification_channels_type_check`, re-add with
     `('email','webhook','telegram')`.
   - `ALTER TABLE events DROP CONSTRAINT events_kind_check`, re-add with
     `('status_change','notification','system','discovery')`.
2. `CREATE TABLE discovery_profiles`, `discovery_scans`, `discovery_results`, `discovery_known_hosts` as above,
   with indexes `ux_discovery_profiles_name_lower`, `ix_discovery_scans_started_at`,
   `ux_discovery_scans_running` (partial), `ix_discovery_results_scan_id`.
3. `downgrade()`: drop the four tables, delete rows using the new enum values
   (`sensors` of the new types, `telegram` channels, `discovery` events), and restore the old checks.

---

# **[2b-1]** Phase 2b-1 additions

Scope: device dependencies + `unreachable` status, maintenance windows, acknowledge, two-stage escalation
(persistent alert state), device fingerprinting without SNMP + re-identify, the HPE switch templates (`hpe_arubaos_switch`, `hpe_comware`), `snmp_custom` `enum` mode, and the
topology map (LLDP/CDP links). All schema changes are in **migration `0004`**
([§ Phase 2b-1 migration](#2b-1-phase-2b-1-migration-0004)). Everything is additive except that
`alert_notified` is **replaced** by `alert_state` (rows are migrated).

## [2b-1] Enum changes

| Name | Values |
|---|---|
| `status` | `up`, `warning`, `down`, `paused`, `unknown`, **[2b-1]** `unreachable` |
| `event_kind` | `status_change`, `notification`, `system`, `discovery`, **[2b-1]** `acknowledge`, `maintenance` |
| **[2b-1]** `dependency_mode` | `inherit`, `none`, `device` |
| **[2b-1]** `maintenance_scope` | `global`, `group`, `device` |
| **[2b-1]** `maintenance_kind` | `once`, `weekly` |
| **[2b-1]** `alert_state_status` | `down`, `warning`, `up`, `suspended` |
| **[2b-1]** `classified_by` | `snmp`, `fingerprint`, `ports`, `none` |
| **[2b-1]** `confidence` | `high`, `medium`, `low` |
| **[2b-1]** `link_source` | `lldp`, `cdp`, `manual` |
| **[2b-1]** `topology_refresh_status` | `running`, `completed`, `failed` |

`sensor_channels.status` is unchanged (`up`,`warning`,`down`,`unknown`): `unreachable` is a sensor status only.
`alert_rules.on_statuses` is unchanged (subset of `down`,`warning`,`up`): `unreachable` never alerts.

**[2b-1] Status severity** (replaces the Phase 1 line; rollups, sorting, "worst first"):
`down` (5) > `warning` (4) > `unreachable` (3) > `unknown` (2) > `up` (1) > `paused` (0).
Rationale: a group containing a down router and its unreachable children shows `down` (the root cause);
a device whose only problem is a dead parent shows `unreachable`, which outranks `unknown`/`up` but never hides
a real `warning`/`down`. The code constant is `netmon.poller.types.SEVERITY`; the frontend mirror is
`frontend/src/lib/status.ts`.

## [2b-1] Device dependencies

### Columns
`groups` gains:

| Column | Type | Constraints |
|---|---|---|
| dependency_mode | text | NOT NULL DEFAULT `inherit` CHECK in dependency_mode |
| dependency_device_id | integer | NULL, FK `devices.id` ON DELETE SET NULL |
| dependency_sensor_id | integer | NULL, FK `sensors.id` ON DELETE SET NULL |

`devices` gains the same three columns (same names, same FKs), plus the identity/topology columns in later
sections.

- `mode = device` with `dependency_device_id IS NULL` (the parent was deleted through a cascade) behaves as
  `none`. The device-delete API resets dependents to `none` explicitly in the same transaction
  (`UPDATE devices/groups SET dependency_mode='none', dependency_device_id=NULL, dependency_sensor_id=NULL WHERE dependency_device_id=:id`).
- `dependency_sensor_id` must be a sensor of `dependency_device_id` (API `422`, loc
  `["body","dependency","sensor_id"]`). With mode `inherit`/`none` both ids are stored NULL.
- A root group with `inherit` = `none`.

### Effective dependency (computed, not stored)
For device D:
1. `D.dependency_mode = device` → parent = `D.dependency_device_id` (source `device`).
2. `none` → no parent.
3. `inherit` → walk D's group, then its ancestors: the first group whose mode is not `inherit` decides
   (`device` → that group's device/sensor, source `group`; `none` → no parent). Root reached → no parent.
4. **Self rule:** an *inherited* parent equal to D itself is ignored (D has no parent). This makes "group
   *Branch* depends on *branch-gw*" work while *branch-gw* lives in *Branch*. An explicit self-dependency is a
   cycle (below).

The **dependency sensor** S of parent P: the configured `dependency_sensor_id`, else P's **primary
availability sensor** = P's enabled `ping` sensor with the lowest `(sort_order, id)`; if P has no enabled ping
sensor, S = none ("whole device" mode).

**Parent down** (evaluated at the moment it is needed, from the DB):
- P disabled (`enabled = false`) → **not** down (children evaluate normally).
- S set → `S.status ∈ {down, unreachable}`.
- No S → every non-paused sensor of P has status `down` or `unreachable`, and there is at least one.
- Chains are transitive automatically: if P is itself unreachable, its ping sensor is `unreachable`, which
  counts as down.

### Cycles
Validated by the API on every change of a device's or group's dependency fields **and** on device moves
(`group_id` change) and group moves (`parent_id` change, which changes what the devices below inherit): compute the effective parent of every device; following parents from any device must not
return to it. Otherwise `400 dependency_cycle`, `details: [{"device_ids": [3, 7, 3]}]` (the cycle path). The
check is in `netmon.services.dependencies.find_cycle(inventory) -> list[int] | None` (pure).

### Status evaluation with a dependency (`netmon.poller.processor.persist`)
After `evaluate()`: if the evaluated status is `down` (check failed **or** a down threshold) **and** the
device has an effective parent that is down → the sensor's status is **`unreachable`** instead, and
`status_message = "Unreachable (parent <parent device name> down): <original message>"`. The channels keep
their evaluated values/statuses. `warning`/`up` are never changed. The parent state is read in the same
transaction (one query; only on a failing check).

**Parent down for a check** (review H2): for this rule the parent counts as down when it is down now **or** its
dependency sensor (whole-device mode: any of its non-paused sensors) changed status *out of* `down`/`unreachable`
at or after the check's start time (`checked_at`), i.e. the parent recovered while this child's check was
running. Such a check failed because of the parent, so the child becomes `unreachable`, not `down`.

**Child before parent** (review H2): when a check of C evaluates `down`, C has an effective parent P and P is
**not** down:
- the scheduler re-checks P's dependency sensor right away (`run_soon([S])`, jitter 0; whole-device mode: all
  of P's enabled sensors), and
- the alert engine holds the new problem: a new problem row of a sensor whose device has an effective parent
  gets `due_at = t + max(R.delay_s, hold)`, with `hold` = the parent's interval plus timeout
  (`S.interval_s + S.timeout_s` of the dependency sensor S; whole-device mode: the largest `interval_s +
  timeout_s` among P's enabled sensors). This applies with `delay_s = 0` too; escalation/reminder timers are
  unchanged. If P goes down meanwhile, the cascade turns C `unreachable` and the held row is deleted
  (nothing was notified) before anything is sent.
`delay_s ≥ the parent's interval` (owner note) remains the robust setting; the hold covers the common case
without it.

**Cascade** (`netmon.services.dependencies.on_status_change`, called from `events.published` for
`status_change` events): when a sensor of device P changes status, for every device C whose effective parent
is P (and whose dependency sensor is this sensor, or "whole device" mode):
- P **became down** → each sensor of C with status `down` becomes `unreachable` (via `set_sensor_status`-like
  code, writes a `status_change` event `down → unreachable` with the message above, keeps `last_check_at`).
- P **is no longer down** → each sensor of C with status `unreachable` is queued for an immediate re-check
  (`scheduler.run_soon(sensor_ids)`, random jitter 0–5 s each; does not move the regular schedule). A
  `run_soon` for a sensor that is **running at that moment** is not dropped: one follow-up run is queued and
  starts right after the running check finishes (at most one pending follow-up per sensor). The status
  stays `unreachable` until that check runs; then it becomes whatever the check says. A child that is
  genuinely down after the parent recovered goes `unreachable → down` and **alerts** like any new problem.
- The cascade re-evaluates "P is down" inside the transaction that changes C's sensors (not from an earlier
  snapshot), so a parent that recovered meanwhile does not leave C stuck `unreachable`.
- The cascade recurses naturally (C's own sensor changes trigger C's children).

Interaction with other statuses: `paused` always wins (paused sensors are not checked); `unknown` (resume,
stale start) is replaced by the next check result; startup does not touch `unreachable` sensors.

**Rollup:** `unreachable` takes part in the worst-of like any status. `status_counts` gain an `unreachable`
key.

## [2b-1] Maintenance windows

### `maintenance_windows`
| Column | Type | Constraints |
|---|---|---|
| id | integer | PK |
| name | varchar(128) | NOT NULL |
| enabled | boolean | NOT NULL DEFAULT true |
| scope | text | NOT NULL CHECK in maintenance_scope |
| group_id | integer | NULL, FK `groups.id` ON DELETE CASCADE. Required iff scope = group. |
| device_id | integer | NULL, FK `devices.id` ON DELETE CASCADE. Required iff scope = device. |
| kind | text | NOT NULL CHECK in maintenance_kind |
| starts_at | timestamptz | `once` only (else NULL) |
| ends_at | timestamptz | `once` only. `starts_at < ends_at`, span ≤ 366 days. |
| weekdays | smallint[] | `weekly` only: ISO weekdays 1 = Monday … 7 = Sunday, 1–7 distinct values, stored sorted. NULL for `once`. |
| start_time | time | `weekly` only: local wall-clock start (minute precision) |
| duration_min | integer | `weekly` only: 1–10080 (7 days) |
| comment | text | NOT NULL DEFAULT '' (≤ 1000 chars) |
| created_by | integer | NULL, FK `users.id` ON DELETE SET NULL |
| created_at, updated_at | timestamptz | |

Table CHECK: `(scope='group') = (group_id IS NOT NULL) AND (scope='device') = (device_id IS NOT NULL)` and
`(kind='once' AND starts_at IS NOT NULL AND ends_at IS NOT NULL AND ends_at > starts_at AND weekdays IS NULL AND start_time IS NULL AND duration_min IS NULL) OR (kind='weekly' AND starts_at IS NULL AND ends_at IS NULL AND weekdays IS NOT NULL AND start_time IS NOT NULL AND duration_min BETWEEN 1 AND 10080)`.

### Time zone
New settings key **`timezone`**: an IANA zone name (e.g. `Asia/Jerusalem`). Default: the `TZ` environment
variable if it is a valid IANA name (`zoneinfo.ZoneInfo(TZ)` succeeds), else `"UTC"`. Validated with
`zoneinfo` on `PUT /api/settings` (422). `POST /api/setup` **ignores** an invalid `timezone` (the default
above is stored instead and a warning is logged), because the wizard has no field to correct it. The setup wizard sends the browser zone
(`Intl.DateTimeFormat().resolvedOptions().timeZone`) so no one has to configure it. The backend adds the
**`tzdata`** PyPI package as a dependency (Windows and slim images have no system zone database).

Only `weekly` windows use it. `once` windows are absolute instants (the UI converts from the configured zone
for display and input).

### Occurrences (pure function `netmon.services.maintenance.occurrences(window, tz, start, end)`)
- `weekly`: for every local date `d` with ISO weekday in `weekdays`: start = `datetime.combine(d, start_time, tzinfo=tz)`
  (Python `zoneinfo`, `fold=0`; a start time inside a DST gap therefore lands after the gap), end = start +
  `duration_min` real minutes. Occurrences may cross midnight and overlap each other (overlaps just merge).
- `once`: `[starts_at, ends_at)`.
- A window is **active** at instant t when it is enabled and some occurrence has `start <= t < end`.

### Coverage
A device is **in maintenance** at t when an active window has scope `global`, or scope `device` with its id, or
scope `group` with its group or any ancestor of its group. Maintenance is **not a status**: checks run,
measurements and statuses are stored as usual; it is a flag.

- Sensors/devices expose `in_maintenance` (from the device). Groups do not.
- `events.in_maintenance boolean NOT NULL DEFAULT false`: for `status_change` and `acknowledge` events, true
  when the event's device was in maintenance at event time.
- **Notifications:** while a device is in maintenance no problem, escalation or reminder notification is sent
  for its sensors (see [alert state](#2b-1-alert-state-escalation-reminders-ack)). **Exception:** a recovery
  (`up`) for a problem that **was** notified before is still sent, so nobody is left with a dangling DOWN.
- **When a device leaves maintenance** and one of its sensors has a problem whose notification was
  suppressed (`alert_state.maintenance_suppressed`), and the problem is still active and not acknowledged, the
  problem notification is sent **once** (`after_maintenance: true`) and the escalation timer restarts from the
  window end. A problem that started and ended inside the window sends nothing.

### Tracker
`netmon.services.maintenance.MaintenanceTracker` (on the runtime, `rt.maintenance`): keeps the windows in
memory (reloaded after every window mutation), evaluates coverage, and runs a loop that wakes at the next
occurrence boundary or after 30 s, whichever is first, and when `rt.clock` changes (tests). On a change of the
active set it: writes one `maintenance` event per window that started/ended (message
`Maintenance "Night patch" started` / `... ended`; `device_id`/`group_id` set for those scopes,
`device_name` snapshot), broadcasts `maintenance.changed`, requests a rollup, and hands the devices that left
maintenance to the alert engine. The **first** evaluation after process start only establishes the active set:
it writes no events and broadcasts nothing, so a window that is already active at startup has no `started`
event (its later `ended` event can appear without a `started` one), and a window that ended while the process
was down writes no `ended` event (its suppressed alerts are still handled, see leaving maintenance). API: `in_maintenance(device_id) -> bool`, `active_window_ids() -> list[int]`,
`window_for(device_id) -> (window_id, name, ends_at) | None` (the covering active occurrence with the latest end).

## [2b-1] Acknowledge

`sensors` gains:

| Column | Type | Constraints |
|---|---|---|
| ack_at | timestamptz | NULL. NULL = not acknowledged. |
| ack_user_id | integer | NULL, FK `users.id` ON DELETE SET NULL |
| ack_by | varchar(64) | NULL. Username snapshot. |
| ack_comment | text | NOT NULL DEFAULT '' (≤ 1000 chars) |
| ack_status | text | NULL CHECK in (`down`,`warning`): the status that was acknowledged |

- Allowed only when the sensor status is `down` or `warning` (not `unreachable`: it never alerts, and its
  cause is the parent, which is what should be acknowledged). Otherwise `409 not_acknowledgeable`.
- Re-acknowledging an acknowledged sensor replaces comment/user/time.
- **Auto-clear:** any status change of the sensor clears all five columns (to up, to another problem status,
  to unreachable/paused/unknown). No extra event is written for the auto-clear (the `status_change` event
  is the record); a `sensor.ack` WS message with `acknowledgement: null` is sent.
- Effect: the acknowledged problem sends no further problem/escalation/reminder notifications; its recovery is
  still sent to everyone notified. Status and rollups are unchanged (an acknowledged down sensor is still
  `down`).
- `acknowledge` events: `sensor_id`/`device_id`/`group_id`/names set, `old_status = new_status =` the sensor
  status, message `Acknowledged by admin: <comment>` (or without `: <comment>` when empty) /
  `Acknowledgement removed by admin`.

## [2b-1] Escalation and persistent alert state

### `alert_rules` gains
| Column | Type | Constraints |
|---|---|---|
| escalation_after_s | integer | NULL, CHECK `60 <= escalation_after_s <= 604800` |
| escalation_channel_ids | integer[] | NOT NULL DEFAULT '{}'. Validated to exist on write; ids deleted later are ignored at send time. |
| repeat_every_s | integer | NULL, CHECK `300 <= repeat_every_s <= 604800`. Reminder to the primary channels. |

API validation (422): `escalation_after_s` and a non-empty `escalation_channel_ids` must be given together;
`escalation_after_s >= delay_s`; escalation/repeat require `down` or `warning` in `on_statuses`.

### [2b-1] Alert state, escalation, reminders, ack
**`alert_state`** replaces `alert_notified` and the in-memory delay timers (survives restarts).

| Column | Type | Constraints |
|---|---|---|
| rule_id | integer | FK `alert_rules.id` ON DELETE CASCADE |
| sensor_id | integer | FK `sensors.id` ON DELETE CASCADE |
| status | text | NOT NULL CHECK in alert_state_status |
| event_id | bigint | NULL (no FK; events age out). The status_change event being notified. |
| problem_since | timestamptz | NOT NULL. Start of the current problem; kept unchanged when the row turns `up` (the recovery time is the recovery event's `time`). |
| due_at | timestamptz | NULL. When the problem (or recovery, for status `up`) notification is due. NULL = nothing pending. |
| primary_sent_at | timestamptz | NULL |
| escalation_due_at | timestamptz | NULL |
| escalated_at | timestamptz | NULL |
| next_reminder_at | timestamptz | NULL |
| reminders_sent | integer | NOT NULL DEFAULT 0 |
| maintenance_suppressed | boolean | NOT NULL DEFAULT false |
| notified_channel_ids | integer[] | NOT NULL DEFAULT '{}'. Every channel that successfully received a problem/escalation/reminder for this problem. |
| sending_since | timestamptz | NULL. **[review H3]** In-flight marker of the pass that is sending for this row (see the alert loop). |
| updated_at | timestamptz | NOT NULL DEFAULT now() |
| | | PK `(rule_id, sensor_id)`; index on `sensor_id` |

A rule R **matches** status S for a sensor when R is enabled, `R.sensor_id` is NULL or the sensor, and S ∈
`R.on_statuses`. **Still active** for a row = the sensor's current status ∈ {down, warning} ∩ `R.on_statuses`.

**On a `status_change` event E (sensor X, new status S, time t):**

| S | Action per rule |
|---|---|
| `down` / `warning` | For each rule R matching S: if there is no row, or the row status ∉ {down, warning} → **new problem**: upsert `status=S, event_id=E, problem_since=t, due_at=t+R.delay_s, primary_sent_at=NULL, escalation_due_at = t+R.escalation_after_s` (NULL when no escalation), `escalated_at=NULL, next_reminder_at=NULL, reminders_sent=0, maintenance_suppressed=false`, **keeping** `notified_channel_ids` (an unsent recovery). If the row is already `down`/`warning` → **change within the problem**: `status=S, event_id=E, due_at=t+R.delay_s` (Phase 1 behaviour: every down↔warning transition notifies); additionally, when R has escalation, `escalated_at IS NULL` and `escalation_due_at IS NULL` (it was consumed while the row was not active), `escalation_due_at = t + R.escalation_after_s` (re-armed); rest unchanged. Rows of rules that do **not** match S: `due_at=NULL` (a pending delayed notification is dropped, as in Phase 1). |
| `up` | For each row of X: `notified_channel_ids` empty → delete. Else if R is enabled and `up` ∈ `R.on_statuses` → `status=up, event_id=E, due_at=t+R.delay_s` (`problem_since` **kept**: the original problem start), escalation/reminder cleared. Else delete (no recovery wanted). |
| `unreachable` / `paused` / `unknown` | For each row: `notified_channel_ids` empty → delete; else `status=suspended`, `due_at`/`escalation_due_at`/`next_reminder_at` = NULL (a later `up` sends the recovery; a later `down` is a new problem). |

**Alert loop** (`AlertEngine` task): wakes at the earliest of `due_at`, `escalation_due_at`,
`next_reminder_at` (one query), on any `notify()` from the event path, on `rt.clock` changes, and at least every
30 s. It locks due rows (`FOR UPDATE SKIP LOCKED`) and, per row, using `now = rt.clock.now()`:

- **Maintenance must be known first** (review H1): the runtime creates and starts (`reload` + first `evaluate`)
  the `MaintenanceTracker` **before** `AlertEngine.start()`. Defensively, while the tracker is missing or not yet
  initialized, the loop treats every device as in maintenance for sending purposes: it sends nothing for problem,
  escalation and reminder stages and leaves those rows untouched (still due), so they are processed once
  coverage is known. Recoveries are not held.
- **Sends happen outside the engine lock** (review H3): a pass plans under the lock in one short transaction
  that also stamps every row it will send for with an in-flight marker (`alert_state.sending_since =
  now`, cleared when the pass applies its result), then releases the lock, sends (≤ 16 in parallel), and
  re-takes the lock only to apply the results (`notified_channel_ids`, `primary_sent_at`, …) to rows that still
  carry its marker. The event path, acknowledge and leaving-maintenance never wait for a channel. A row with
  `sending_since` set counts as **notified** for the `up`/`unreachable`/`paused`/`unknown` rules of the
  `status_change` table (it is not deleted, so a recovery is still sent once the in-flight send has landed in
  `notified_channel_ids`), and is skipped by other passes. A marker older than 10 minutes (crash) is ignored.

1. **Problem due** (`status ∈ {down,warning}`, `due_at <= now`): not still active → `due_at=NULL` (delete the
   row if nothing was notified). Acknowledged → `due_at=NULL`. Device in maintenance → `due_at=NULL,
   maintenance_suppressed=true`. Else send stage `problem` to R's current channels; on ≥ 1 success add them to
   `notified_channel_ids`, `primary_sent_at=now`; always `due_at=NULL`; `next_reminder_at = now +
   R.repeat_every_s` when set.
2. **Recovery due** (`status = up`, `due_at <= now`): sensor still `up` → send stage `recovery` to the enabled
   channels in `notified_channel_ids` (even in maintenance), then delete the row. Not up → `due_at=NULL`.
3. **Escalation due**: still active, not acknowledged, R still has escalation → send stage `escalation` to
   `R.escalation_channel_ids`, add successes to `notified_channel_ids`, `escalated_at=now`. In maintenance →
   `maintenance_suppressed=true`. In every case `escalation_due_at=NULL`.
4. **Reminder due**: still active, not acknowledged, R still has `repeat_every_s`, not in maintenance → send
   stage `reminder` to R's channels, `reminders_sent += 1`. Then `next_reminder_at = now + R.repeat_every_s`
   (also when skipped for maintenance); NULL when acknowledged / not active / repeat removed. After a long
   downtime only one overdue reminder is sent (no burst).

**Leaving maintenance** (from the tracker): for rows of the device's sensors with `maintenance_suppressed`,
still active and not acknowledged, two independent re-arms:
- **Problem:** only when the problem notification was never sent (`primary_sent_at IS NULL`) and nothing is
  pending (`due_at IS NULL`) → `due_at = now` (sent once by step 1, payload `after_maintenance: true`). A
  problem already notified before the window is **not** sent again (its suppressed stage was an escalation).
- **Escalation:** when R has escalation, `escalated_at IS NULL` and `escalation_due_at IS NULL` →
  `escalation_due_at = now + R.escalation_after_s`, regardless of `due_at`.
- The flag stays `true` only while a problem re-arm is pending (it is the persisted "after maintenance" marker,
  so it survives a restart between the window end and the send; step 1 clears it). In every other case —
  nothing to re-arm, only the escalation re-armed, not active, acknowledged — `maintenance_suppressed=false`.
The check runs on every coverage change and once at startup, so a window that ended while the process was down
is handled too.

**Acknowledge** sets `due_at`/`escalation_due_at`/`next_reminder_at` = NULL on the sensor's down/warning rows
(defensive; steps 1/3/4 also check). **Unacknowledge** (review M1) re-arms what the acknowledgement
prevented, for the sensor's still-active down/warning rows:
- `primary_sent_at IS NULL` → `due_at = now` (the problem notification was never sent; sent by step 1, normal
  payload; in maintenance step 1 suppresses it as usual);
- R has escalation and `escalated_at IS NULL` → `escalation_due_at = now + R.escalation_after_s`;
- R has `repeat_every_s` and `primary_sent_at IS NOT NULL` → `next_reminder_at = now + repeat_every_s`
  (reminders resume; with a pending problem send, step 1 arms them).

**De-duplication:** one send per `(sensor_id, event_id, stage, channel_id)` within the process (bounded
in-memory set, as in Phase 1). Reminders count as distinct stages (`reminder:<n>`), and the after-maintenance
problem send is distinct from a pre-window problem send of the same event. A send failure writes a `notification` event; there is no retry.

**Restart:** rows are persisted; overdue rows are processed right after startup (validated against the current
state), once the maintenance tracker has evaluated coverage (above). The Phase 1 sentence "Pending timers are
lost on restart" no longer applies. Sends are at-least-once: a crash between a send and the result being applied
can repeat that send after the restart.

## [2b-1] Device identity and fingerprinting

### Columns
`discovery_results` gains:

| Column | Type | Constraints |
|---|---|---|
| fingerprint | jsonb | NULL (NULL = fingerprinting off or nothing to fingerprint). Schema below. |
| model | varchar(64) | NULL |
| classified_by | text | NOT NULL DEFAULT `ports` CHECK in classified_by |
| confidence | text | NOT NULL DEFAULT `low` CHECK in confidence |

`devices` gains `identity jsonb NULL`: the last identification (from import or `POST /api/devices/{id}/identify`),
shape `DeviceIdentity` in api.md.

### Fingerprint JSON
```json
{ "http": [ { "port": 443, "scheme": "https", "status": 200, "title": "Login | OPNsense", "server": "OPNsense",
              "realm": null, "markers": ["opnsense"], "redirects": 1, "error": null } ],
  "ssh": { "port": 22, "banner": "SSH-2.0-OpenSSH_9.7" },
  "telnet": null }
```
- `http`: one entry per open port of {80, 443, 8080, 8443} (443/8443 → `https`, others `http`); always present
  (possibly `[]`). `error` is a short text (`timeout`, `tls error`, `connection refused`, ...) with the other
  fields null/[]. `title` ≤ 200 chars (first `<title>`, HTML-unescaped, whitespace collapsed), `server` = the
  `Server` header ≤ 200, `realm` = `realm="..."` of the first `WWW-Authenticate` header ≤ 200, `markers` =
  sorted subset of the marker list below found in title + body.
- `ssh`: when 22 is open; the first line received within the timeout (≤ 255 printable ASCII chars), else
  `{"port":22,"banner":null}`.
- `telnet`: when 23 is open; up to 512 bytes read within the timeout, IAC sequences (0xFF + 2 bytes; SB…SE)
  removed, non-printables dropped, first non-empty line ≤ 255 chars. Nothing is ever sent.

**Markers** (case-insensitive, regex `\b<marker>\b` over title + the first 64 KiB of the body):
`opnsense, pfsense, unifi, ubiquiti, airos, edgeos, mikrotik, routeros, tp-link, tplink, easy smart, omada,
procurve, aruba, arubaos, officeconnect, comware, h3c, hpe, cisco, netgear, prosafe, d-link, dlink, zyxel,
fortinet, fortigate, synology, diskstation, qnap, laserjet, officejet, brother, canon, kyocera, ricoh, epson,
xerox, lexmark, ilo, idrac, supermicro, megarac`.

### Fingerprint classification rules (pure, `netmon.discovery.classify.classify_fingerprint(fp) -> FpMatch | None`)
Applied when **SNMP did not answer**, and also when SNMP answered but rules 1–12 ended in class `unknown`
(then the SNMP vendor is kept if the fingerprint rule gives none). Evaluated **before** the port rules 13–18;
first match wins. Fields: **T** = all HTTP titles, **S** = Server headers, **R** = realms, **M** = markers,
**B** = SSH banner, **N** = telnet banner. Regexes are case-insensitive.

| # | Condition | class | vendor | confidence | Notes |
|---|---|---|---|---|---|
| F1 | T or M `opnsense` | router | opnsense | high | Login page title `Login \| OPNsense` |
| F2 | T or M `pfsense` | router | pfsense | high | |
| F3 | T `RouterOS\|mikrotik\|webfig`, or B starts `SSH-2.0-ROSSSH` | router | mikrotik | high | ROSSSH is MikroTik's SSH server |
| F4 | T `UniFi OS` | router | ubiquiti | medium | UDM/UCG consoles |
| F5 | T `UniFi Network` | server | ubiquiti | medium | Self-hosted controller (8443) |
| F6 | T or M `edgeos\|edgerouter` → router; `edgeswitch` → switch; `airos` → access_point; other M `unifi\|ubiquiti` → unknown | (stated) | ubiquiti | medium / low for the last | |
| F7 | T or M `TL-SG\d+\w*E\b\|easy smart` | switch | tp-link | medium | Easy Smart (TL-SG105E/108E/116E/1016DE/1024DE …). Title/body text varies by firmware |
| F8 | T `\bTL-SG\|\bT\d{4}\|jetstream` → switch; T or M `omada\|\bEAP\d` → access_point; other M `tp-link\|tplink` → unknown | (stated) | tp-link | medium / low | |
| F9 | B `Comware` | switch | h3c | high | Comware SSH banner `SSH-2.0-Comware-x.y` |
| F10 | T or M `comware\|h3c\|officeconnect\|procurve\|arubaos\|aruba.*switch\|hpe.*switch` | switch | hp (h3c for comware/h3c) | medium | ProCurve/ArubaOS-Switch/OfficeConnect web UIs |
| F11 | S `^eHTTP` | switch | hp | medium | Old ProCurve web server |
| F12 | T or M `\bilo\b\|integrated lights-out` | server | hp | high | |
| F13 | T or M `idrac` | server | dell | high | |
| F14 | T or M `supermicro\|megarac` or T `\bIPMI\b` | server | null (supermicro when that word) | medium | BMC |
| F15 | T or M `synology\|diskstation` | nas | synology | high | DSM ports 5000/5001 are not probed; 80/443 redirect usually shows it |
| F16 | T or M `qnap\|\bQTS\b` | nas | qnap | medium | |
| F17 | T or M `laserjet\|officejet\|pagewide\|designjet`, or S `HP HTTP Server\|HP-ChaiSOE` | printer | hp | high (title) / medium (server) | |
| F18 | T or M `brother`, or S `^debut` | printer | brother | high / medium | |
| F19 | T or M `canon`, or S `CANON HTTP Server\|KS_HTTP` | printer | canon | medium | |
| F20 | T or M `kyocera\|command center` , or S `KM-MFP-http` | printer | kyocera | medium | |
| F21 | T or M `ricoh\|web image monitor` | printer | ricoh | medium | |
| F22 | T or M `epson` → printer/epson; `xerox` → printer/xerox; `lexmark` → printer/lexmark | printer | (stated) | medium | |
| F23 | T or M `fortigate\|fortinet`, or S `^xxxxxxxx-xxxxx$` | router | fortinet | medium | |
| F24 | B `Cisco-\d`, or S `cisco`, or T `cisco` | T `\bSG\d{3}\|\bCBS\d{3}\|catalyst\|switch` → switch, else router | cisco | medium | Banner `SSH-2.0-Cisco-1.25` does not say the device type |
| F25 | T or M `netgear\|prosafe` | T `\b(GS\|XS\|MS)\d{3}` → switch, else unknown | netgear | medium / low | |
| F26 | T or M `d-link\|dlink` | T `\b(DGS\|DES\|DXS)-` → switch, else unknown | d-link | medium / low | |
| F27 | T or M `zyxel` | T `\b(GS\|XGS\|XS)\d{3,4}` → switch, else unknown | zyxel | medium / low | |
| F28 | S `Microsoft-IIS` | server | microsoft | low | |
| F29 | B `OpenSSH.*(Ubuntu\|Debian\|Raspbian)` | server | linux | low | |

**Model** — the regexes below are tried, in the order listed, against the HTTP titles (**T**), then the HTTP
realms (**R**, the `realm="..."` of `WWW-Authenticate`), then the telnet banner (**N**): the first text that
yields a match wins (T → R → N; within one text, the first regex in the list). Stored ≤ 64 chars, else null. R
is what gives `DGS-1210-28` for the `realm="DGS-1210-28"` fingerprint sim. Regexes:
`\b(TL-[A-Z]{2,5}\d{2,5}[A-Z]{0,3})\b`,
`\b(J[L]?\d{3,4}A)\b` (HPE part numbers), `OfficeConnect\s+(?:Switch\s+)?(\d{4}S?)`,
`\b((?:DS|RS)\d{3,4}\+?)(?!\w)` (no trailing `\b`, so `DS920+` keeps its `+`),
`\b((?:GS|XS|MS)\d{3}[A-Z]{0,4})\b`, `\b((?:DGS|DES|DXS)-\d{4}[-\w]*)\b`, `\b(U[67]-[\w-]+|UAP-[\w-]+)\b`.

These rules are **heuristics**. Confidence `high` means "the string is vendor-specific as far as the architect
knows", never certainty; the UI shows the confidence and the user always chooses the template.

**`classified_by` / `confidence` for every result:** SNMP rules 1–10 → `snmp`/`high`; 11–12 → `snmp`/`medium`
(12 ending in `unknown` → fingerprint rules may take over); fingerprint match → `fingerprint`/rule confidence;
port rules 13–17 → `ports`/`low`; rule 18 → `none`/`low`.

New vendor slugs: `h3c`, `supermicro`; `aruba` now also comes from enterprise 47196.

### Vendor table additions
| Enterprise | Vendor slug | Default class (rule 10) | Confidence | Notes |
|---|---|---|---|---|
| 25506 | h3c | switch | high | H3C / HPE Comware (OfficeConnect 1920 non-S, 1950, 5130, 5510 …) |
| 47196 | aruba | switch | medium | Aruba (HPE) AOS-CX switches (6100/6200/6300 …). Classified only; no dedicated template |

Rule 10's switch regex gains `officeconnect|comware|arubaos|aruba.*switch`.

### Template match: new field `enterprises`
Match rules gain `"enterprises": int[] | null`: matches when the result's `sys_object_id` enterprise is in
the list (`null` = any). `suggest()` gains the `sys_object_id` argument. Existing templates get
`"enterprises": null`.

### `snmp_table` entry additions
| Field | Type | Default | Meaning |
|---|---|---|---|
| `skip_zero` | bool | false | Rows whose walked value is `0` or not numeric are ignored (not counted towards `max`). |
| `entity_sensor_scale` | bool | false | **[1.2.1]** ENTITY-SENSOR-MIB tables: at apply time read `entPhySensorScale` (`…99.1.1.1.2`) and `entPhySensorPrecision` (`…99.1.1.1.3`) per row and store `multiplier = 10^(3·(scale−9) − precision)` (times the entry's own multiplier) in the sensor config. Unreadable/out-of-range values count as units / 0. Omitted from the parsed template when false. |
| `filters` | `{"oid": str, "in": int[] \| null, "not_in": int[] \| null}[]` | `[]` | Each filter GETs `oid + "." + S` for the row suffix S; the row is kept only when the value (integer) is in `in` (when set) and not in `not_in` (when set). A missing/non-integer value drops the row. All filters must pass. |

`config` of an `snmp_table` entry may contain every `snmp_custom` key except `oid`/`percent_of_oid`/`label`
(so also `value_type: "enum"` with `value_map` / `value_map_default`). New entry field `interval_s: int | null`
(default null = `settings.default_interval_s`) sets the interval of the created sensors.

## [2b-1] `snmp_custom`: new value type `enum`
`value_type` gains **`enum`**, for integer status columns (fan/PSU/sensor states). New config keys:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `value_map` | `value_map` (new config field type): object, key = integer as a decimal string, value = `{"status": "up"\|"warning"\|"down", "text": str}` (`text` 0–32 chars) | `{}` | **Required non-empty** for `enum` (422 otherwise); ignored for other types. ≤ 32 entries. |
| `value_map_default` | `"up"` \| `"warning"` \| `"down"` | `"warning"` | Status for a value not in the map |

Evaluation (`enum`): the response must be an integer (else check failed, `Value is not an integer`). The
channel `value` = the raw integer (no multiplier). The check is **ok** and the sensor status is the mapped
status (`CheckResult.status_override`, new optional field: `evaluate()` uses `worst(override, channel
statuses)` for an ok result). `status_message` = `"<label>: <text> (<raw>)"` (text = the map text, or
`unexpected value` when unmapped). Thresholds still apply to `value` but none are set by default.
`GET /api/sensor-types` lists `value_map` (type `value_map`) and `value_map_default` (type `enum`). The
frontend renders `value_map` as an editable table (value, status, text).

Decision: a mapping on `snmp_custom` rather than a new sensor type — same GET path, one extra branch, and the
templates can create it from tables.

## [2b-1] HPE switch templates

Two templates, chosen automatically by sysObjectID enterprise (no conditional rows):

| id | name en / he | priority | match |
|---|---|---|---|
| `hpe_arubaos_switch` | HPE ArubaOS-Switch / ProCurve / מתג HPE ArubaOS | 50 | `requires_snmp`, classes `[switch]`, `enterprises [11]` |
| `hpe_comware` | HPE Comware / H3C switch / מתג HPE Comware | 50 | `requires_snmp`, classes `[switch]`, `enterprises [25506]` |

Class `switch` keeps HP JetDirect printers (also enterprise 11) out. **Aruba CX (47196) gets no template**: the
architect is not confident about its CPU/memory/health MIBs; CX devices fall to `generic_snmp` (probed
HOST-RESOURCES CPU/memory). Descriptions: en `Ping, system, CPU, memory, hardware health, PoE and active
ports`, he `פינג, מערכת, מעבד, זיכרון, תקינות חומרה, PoE ופורטים פעילים`.

Every vendor row is either `probe: true` (single OID) or an `snmp_table` (a walk with no rows → skipped
`no_rows`), so unsupported objects never create sensors. Confidence: **high** = matches the published MIB as far
as the architect knows; **medium** = believed correct, verify; **low** = unsure. **The owner verifies all
vendor rows on his switches.**

Common enum maps (stored in the entry config):

- `HPICF_SENSOR_MAP` (hpicfSensorStatus: unknown(1), bad(2), warning(3), good(4), notPresent(5)):
  `{"1":{"status":"warning","text":"unknown"},"2":{"status":"down","text":"bad"},"3":{"status":"warning","text":"warning"},"4":{"status":"up","text":"good"}}`,
  default `warning`. Enum values: **medium-high** confidence.
- `HH3C_ERROR_MAP` (hh3cEntityExtErrorStatus): `{"2":{"status":"up","text":"normal"}}`, default **`down`**
  (every other value is a fault: postFailure(3), fanError(41), psuError(51), rpsError(61), hardwareFaulty(91), …).
  `normal(2)` / `notSupported(1)` / `entityAbsent(4)`: **medium** confidence.

### `hpe_arubaos_switch` entries
| # | Entry | Details | Confidence |
|---|---|---|---|
| 1 | Ping | | |
| 2 | System | uptime | high |
| 3 | `CPU` (`snmp_custom` gauge, probe) | `1.3.6.1.4.1.11.2.14.11.5.1.9.6.1.0` hpSwitchCpuStat, unit `%`, label `CPU`, channel value warning_max 80 / down_max 95 | **high** (2530/2540/2920/2930/3810/5400). **low** on OfficeConnect 1820/1920S (also enterprise 11): may be absent → skipped |
| 4 | `Memory {index}` (`snmp_table`) | walk hpLocalMemAllocBytes `1.3.6.1.4.1.11.2.14.11.5.1.1.2.1.1.1.7`, `percent_of_column` hpLocalMemTotalBytes `…1.1.1.5`, gauge `%`, 85/95, max 8. Index = slot / stack member (`1` standalone) | **medium** (NETSWITCH-MIB hpLocalMemEntry .5 Total, .6 Free, .7 Alloc) |
| 5 | `{label}` health (`snmp_table`) | walk hpicfSensorStatus `1.3.6.1.4.1.11.2.14.11.1.2.6.1.4`, `label_oid` hpicfSensorDescr `…6.1.7`, `filters [{"oid":"1.3.6.1.4.1.11.2.14.11.1.2.6.1.4","not_in":[5]}]` (skip notPresent), config `{"value_type":"enum","value_map":HPICF_SENSOR_MAP,"value_map_default":"warning"}`, name `{label}` (e.g. `Fan Sensor`, `Power Supply Sensor`, `Over-temperature Sensor`), max 16, interval 300 | table/column layout **medium-high** (HP-ICF-CHASSIS hpicfSensorEntry: .1 Index, .2 ObjectId, .3 Number, .4 Status, .5 Warnings, .6 Failures, .7 Descr). hpicfSensorObjectId (.2) is not needed: the description names the sensor |
| 6 | `PoE {index} usage` (`snmp_table`) | walk pethMainPseConsumptionPower `1.3.6.1.2.1.105.1.3.1.1.4`, `percent_of_column` pethMainPsePower `…1.1.2`, `filters [{"oid":"1.3.6.1.2.1.105.1.3.1.1.2","not_in":[0]}]`, gauge `%`, warning_max 80 / down_max 95, max 8 | **high** (RFC 3621). Present only on PoE models |
| 7 | `PoE {index} power` (`snmp_table`) | walk `…105.1.3.1.1.4` (consumption), same filter, gauge unit `W`, no thresholds, max 8 | high |
| 8 | `PoE {index} budget` (`snmp_table`) | walk `…105.1.3.1.1.2` (nominal), `skip_zero`, gauge unit `W`, `interval_s` 3600, max 8 | high |
| 9 | `snmp_interfaces` | up only, `exclude_if_types [1, 24, 53, 131, 136]` (other, loopback, propVirtual/VLAN, tunnel, l3ipvlan), `exclude_name_regex` IF_EXCLUDE, max 52 | high |

ArubaOS-Switch has no plain temperature gauge in this template (the over-temperature *status* is row 5; the
`hpSystemAirTempTable` value is a string like `38C`, confidence low — future item).

### `hpe_comware` entries
| # | Entry | Details | Confidence |
|---|---|---|---|
| 1 | Ping | | |
| 2 | System | uptime | high |
| 3 | `CPU {label}` (`snmp_table`) | walk hh3cEntityExtCpuUsage `1.3.6.1.4.1.25506.2.6.1.1.1.1.6`, `label_oid` entPhysicalName `1.3.6.1.2.1.47.1.1.1.1.7`, `skip_zero`, gauge `%`, 80/95, max 8 | OID **high**; non-zero row selection **medium** (only boards/MPUs report CPU; a board at exactly 0 % at apply time is skipped — re-apply adds it) |
| 4 | `Memory {label}` (`snmp_table`) | walk hh3cEntityExtMemUsage `…1.1.1.1.8`, label entPhysicalName, `skip_zero`, gauge `%`, 85/95, max 8 | as row 3 |
| 5 | `Temperature {label}` (`snmp_table`) | walk hh3cEntityExtTemperature `…1.1.1.1.12`, label entPhysicalName, `skip_zero`, gauge unit `°C`, warning_max 65 / down_max 80, max 16 | column **medium**; thresholds are generic defaults (Comware's own threshold `.13` is not used) |
| 6 | `{label}` fan/PSU (`snmp_table`) | walk hh3cEntityExtErrorStatus `…1.1.1.1.19`, label entPhysicalName, `filters [{"oid":"1.3.6.1.2.1.47.1.1.1.1.5","in":[6,7]}, {"oid":"1.3.6.1.4.1.25506.2.6.1.1.1.1.19","not_in":[1,4]}]` (entPhysicalClass powerSupply(6)/fan(7); skip notSupported/absent), config `{"value_type":"enum","value_map":HH3C_ERROR_MAP,"value_map_default":"down"}`, max 16, interval 300 | entPhysicalClass values **high** (ENTITY-MIB); ErrorStatus column number `.19` **medium** |
| 7–9 | PoE usage / power / budget | identical to `hpe_arubaos_switch` rows 6–8 | MIB high; Comware support **medium** |
| 10 | `snmp_interfaces` | as ArubaOS row 9 (Comware `Vlan-interface*` is type 136, `NULL0` type 1, `InLoopBack0` type 24) | high |

`hh3cEntityExtEntry` is indexed by `entPhysicalIndex`, so the created sensors use fixed
`<column>.<entPhysicalIndex>` OIDs (stable on a fixed chassis/stack: medium). Comware sysObjectIDs are
`1.3.6.1.4.1.25506.11.1.<model>` (medium); OfficeConnect 1920 (non-S)/1950 are Comware, OfficeConnect
1820/1920S report enterprise 11.

**Future items (not 2b-1):** per-port PoE status (`pethPsePortTable`), ArubaOS temperature value, Aruba CX template.

## [2b-1] Topology

### `devices` gains
| Column | Type | Constraints |
|---|---|---|
| lldp_chassis_id | varchar(255) | NULL. Normalised (below) own `lldpLocChassisId`, written by refresh. |
| lldp_sys_name | varchar(255) | NULL. `lldpLocSysName`. |

### `topology_neighbors` (unmanaged nodes)
| Column | Type | Constraints |
|---|---|---|
| id | integer | PK |
| chassis_id | varchar(255) | NOT NULL, UNIQUE (normalised; CDP neighbours use `cdp:<deviceId>`) |
| sys_name | varchar(255) | NULL |
| sys_descr | text | NULL (≤ 1000) |
| mgmt_address | varchar(64) | NULL (first IPv4, else IPv6) |
| first_seen, last_seen | timestamptz | NOT NULL DEFAULT now() |

### `topology_links`
| Column | Type | Constraints |
|---|---|---|
| id | integer | PK |
| source | text | NOT NULL CHECK in link_source |
| a_device_id | integer | NOT NULL, FK `devices.id` ON DELETE CASCADE |
| a_if_index | integer | NULL |
| a_port | varchar(255) | NOT NULL DEFAULT '' (ifName, else ifDescr, else LLDP port id) |
| b_device_id | integer | NULL, FK `devices.id` ON DELETE CASCADE |
| b_neighbor_id | integer | NULL, FK `topology_neighbors.id` ON DELETE CASCADE |
| b_if_index | integer | NULL |
| b_port | varchar(255) | NOT NULL DEFAULT '' |
| first_seen, last_seen | timestamptz | NOT NULL DEFAULT now() (manual: creation / update time) |
| missed_refreshes | integer | NOT NULL DEFAULT 0 |
| stale | boolean | NOT NULL DEFAULT false |
| created_by | integer | NULL, FK `users.id` ON DELETE SET NULL (manual only) |

- CHECK exactly one of `b_device_id` / `b_neighbor_id` is set; CHECK `b_device_id IS NULL OR b_device_id <> a_device_id`.
- **Canonical order** for device–device links: `a_device_id < b_device_id` (endpoints swapped when needed), so
  both sides' LLDP reports collapse into one row.
- UNIQUE index `ux_topology_links_ends` on `(source, a_device_id, a_port, COALESCE(b_device_id,0), COALESCE(b_neighbor_id,0), b_port)`.
- Manual links are never stale and never touched by refresh. A discovered link with the same endpoints and
  ports as a manual one is still stored; the graph API hides discovered duplicates of manual links.

### `topology_layout`
`node_key varchar(32) PK` (`d:<device_id>` or `n:<neighbor_id>`), `x double precision NOT NULL`,
`y double precision NOT NULL`, `updated_at`. One global layout. Rows of deleted nodes are removed by the
refresh and ignored by the graph API.

### `topology_refreshes`
`id` PK, `trigger` (`manual`|`scheduled`), `status` (topology_refresh_status), `started_at`, `finished_at` NULL,
`devices_total`, `devices_ok`, `devices_failed`, `links_found` (integers, default 0), `error` text NULL,
`created_by` FK users SET NULL. Partial unique index on `status='running'`; startup marks `running` rows
`failed` ("Interrupted by restart"); keep the newest 20.

### Settings key
`topology_refresh_min`: int, `0` (off) or 5–1440, default `30`.

### SNMP data (LLDP-MIB `1.0.8802.1.1.2`, CDP)
| Object | OID | Index | Confidence |
|---|---|---|---|
| lldpLocChassisIdSubtype / lldpLocChassisId | `1.0.8802.1.1.2.1.3.1.0` / `.1.3.2.0` | scalar | high |
| lldpLocSysName | `1.0.8802.1.1.2.1.3.3.0` | scalar | high |
| lldpLocPortIdSubtype / PortId / PortDesc | `1.0.8802.1.1.2.1.3.7.1.2` / `.3` / `.4` | `lldpLocPortNum` | high |
| lldpRemChassisIdSubtype / ChassisId | `1.0.8802.1.1.2.1.4.1.1.4` / `.5` | `lldpRemTimeMark.lldpRemLocalPortNum.lldpRemIndex` | high |
| lldpRemPortIdSubtype / PortId / PortDesc | `…4.1.1.6` / `.7` / `.8` | same | high |
| lldpRemSysName / SysDesc | `…4.1.1.9` / `.10` | same | high |
| lldpRemManAddrIfSubtype | `1.0.8802.1.1.2.1.4.2.1.3` | `TimeMark.LocalPortNum.Index.AddrSubtype.<len>.<addr octets>` (address is in the **index**) | high for the layout; address subtype 1 = IPv4 (len 4), 2 = IPv6 (len 16) |
| cdpCacheAddressType / Address | `1.3.6.1.4.1.9.9.23.1.2.1.1.3` / `.4` | `cdpCacheIfIndex.cdpCacheDeviceIndex` | high (type 1 = IPv4, 4 octets) |
| cdpCacheDeviceId / DevicePort / Platform | `….1.1.6` / `.7` / `.8` | same; **local side = ifIndex directly** | high |

The time mark (first index arc) is ignored; LocalPortNum and RemIndex identify the neighbour row.

**Normalisation:** chassis/port id subtype 4 (`macAddress`) or any 6-octet value under subtype 3/4 →
`aa:bb:cc:dd:ee:ff`; subtype 5 (`networkAddress`) → the address text; other subtypes → decoded text when
printable, else lower-case hex `0x…`.

**Local port → ifIndex/ifName** (in this order, against the device's ifName/ifDescr walk):
1. `lldpLocPortIdSubtype` 5 (`interfaceName`) or 7 (`local`) and the port id text equals an `ifName` or `ifDescr` → that ifIndex.
2. `lldpLocPortDesc` equals an `ifName` or `ifDescr` → that ifIndex.
3. `lldpLocPortNum` is an existing ifIndex → that ifIndex.
4. Else `a_if_index = NULL`, `a_port` = port desc or id.

Per family: **ArubaOS-Switch/ProCurve**: lldpLocPortNum = ifIndex = port number, ifName `1`, `2`, `A1` …
(confidence **medium-high**; step 3 catches it, step 1 usually too — port id subtype 7 "local" = port
number). **Comware**: lldpLocPortNum is **not** the ifIndex in general (confidence **medium**); the port id is
usually subtype 5 with the interface name (`GigabitEthernet1/0/1`), so step 1 resolves it. Step 3 is a
last-resort guess and may be wrong on Comware — that is why it comes last. Comware 7 may also populate the
LLDP-V2 MIB; only the v1 LLDP-MIB is used here (if a device answers only V2, it shows no links — report it).

### Refresh (`netmon.topology.refresh`)
- Devices walked: `enabled` devices that have at least one SNMP sensor (`requires_snmp` type) — ping-only
  devices are skipped. Concurrency 8 (own semaphore), each device a 30 s budget, effective SNMP credentials,
  the shared SNMP client/engine. Per device: local scalars, lldpLocPortTable, lldpRemTable,
  lldpRemManAddrTable, cdpCacheTable (missing tables = no rows, not a failure), and ifName/ifDescr. A device
  counts as **ok** when the lldpLocChassisId GET or any walk succeeded; a timeout/auth failure → failed.
- **Matching** a neighbour row to a device (first rule that gives exactly one device; ties → lowest id):
  1. a management address equals a device `host` (IP text compare after normalisation);
  2. normalised remote chassis id equals a device's `lldp_chassis_id`;
  3. remote sysName equals (case-insensitive) a device `lldp_sys_name`, `name`, or `host`; also with the
     domain stripped (`sw1.lan` ↔ `sw1`).
  Unmatched → upsert a `topology_neighbors` row by chassis id (sys_name, sys_descr, mgmt_address, last_seen).
- The remote port: matched device → resolve `b_if_index` against **that** device's ifName/ifDescr with rule 1/2
  (port id subtype 5/7 or port desc), else NULL; `b_port` = port id or desc text.
- Upsert links (canonical order), `last_seen = now`, `missed_refreshes = 0`, `stale = false`.
- **Stale:** for every non-manual link not seen in this refresh where at least one endpoint device was walked
  **ok and reported LLDP/CDP data** in this refresh (a non-empty lldpLocPortTable, lldpRemTable or
  cdpCacheTable): `missed_refreshes += 1`; `>= 3` → `stale = true`. An endpoint that answers SNMP but has no
  LLDP/CDP data could never report the link, so its successful walk does not count as a miss. Stale links are
  never deleted automatically.
- **Promotion:** a neighbour that now matches a device (rules above) has its links re-pointed to the device
  (duplicates merged, keeping the oldest `first_seen`), its layout row renamed when the device has none, and is
  deleted. Neighbours with no links left are deleted at the end.
- **Startup:** `topology_refreshes` rows still `running` (left by a crash or kill) are marked `failed` with
  `error = 'Interrupted by restart'` and `finished_at = now()` before anything else uses the runner.
- One refresh at a time. Scheduled by a loop (like discovery) every `topology_refresh_min` minutes (first run 2
  minutes after startup); off when 0.

### Dependency suggestions (pure, `netmon.topology.suggest_dependencies(graph, root_device_id)`)
- Graph = non-stale device–device links (manual included). Root = the request's `root_device_id`, else the
  device with the most such links (ties → lowest id).
- BFS from the root; each reached device's suggested parent = its BFS predecessor (ties → the neighbour with
  more links, then lowest id). The root and unreached devices get `null`.
- Nothing is applied automatically; applying goes through the bulk endpoint, which validates cycles.

## [2b-1] Phase 2b-1 migration (`0004`)
File `backend/netmon/migrations/versions/0004_phase2b1.py`, `revision = "0004"`, `down_revision = "0003"`.
Plain PostgreSQL DDL only.

`upgrade()`:
1. Widen checks: `sensors_status_check` (`status` + `unreachable`), `events_old_status_check`,
   `events_new_status_check` (same list), `events_kind_check` (+ `acknowledge`, `maintenance`).
2. `ALTER TABLE groups ADD dependency_mode …, dependency_device_id …, dependency_sensor_id …`.
3. `ALTER TABLE devices ADD dependency_mode, dependency_device_id, dependency_sensor_id, identity jsonb NULL,
   lldp_chassis_id, lldp_sys_name`; index `ix_devices_dependency_device_id`.
4. `ALTER TABLE sensors ADD ack_at, ack_user_id, ack_by, ack_comment, ack_status`.
5. `ALTER TABLE events ADD in_maintenance boolean NOT NULL DEFAULT false`.
6. `ALTER TABLE alert_rules ADD escalation_after_s, escalation_channel_ids, repeat_every_s` (+ checks).
7. `CREATE TABLE alert_state` (+ `ix_alert_state_sensor_id`; includes the **[review H3]** in-flight column
   `sending_since timestamptz NULL` — 0004 is unreleased, so it goes into the same `CREATE TABLE`); copy
   `INSERT INTO alert_state (rule_id, sensor_id, status, problem_since, primary_sent_at, notified_channel_ids)
   SELECT n.rule_id, n.sensor_id, n.status, n.time, n.time, COALESCE((SELECT array_agg(channel_id) FROM alert_rule_channels c WHERE c.rule_id = n.rule_id), '{}') FROM alert_notified n`;
   `DROP TABLE alert_notified`.
8. `CREATE TABLE maintenance_windows` (+ `ix_maintenance_windows_group_id`, `ix_maintenance_windows_device_id`).
9. `ALTER TABLE discovery_results ADD fingerprint, model, classified_by, confidence`;
   `UPDATE discovery_results SET classified_by='snmp', confidence='medium' WHERE snmp_ok`.
10. `CREATE TABLE topology_neighbors, topology_links, topology_layout, topology_refreshes` with
    `ux_topology_links_ends`, `ix_topology_links_b_device_id`, `ix_topology_links_b_neighbor_id`,
    `ux_topology_refreshes_running` (partial).

`downgrade()` (reverse order): drop the topology tables, `maintenance_windows`; recreate `alert_notified` and
copy `alert_state` rows with `status IN ('down','warning') AND cardinality(notified_channel_ids) > 0`, drop
`alert_state`; drop the added columns; `UPDATE sensors SET status='down' WHERE status='unreachable'`,
`UPDATE events SET old_status='down' WHERE old_status='unreachable'` (same for `new_status`),
`DELETE FROM events WHERE kind IN ('acknowledge','maintenance')`; restore the old checks. Settings rows
`timezone`/`topology_refresh_min` are harmless and stay.

---

# **[2b-2]** Phase 2b-2 additions

Scope: the **switch ports view** (port inventory job, MAC table, ARP, reverse DNS, OUI vendors, uplink detection,
health strip) and the **vendor template library** (templates as YAML data files, user overrides, reload,
`verified` / `confidence`, new vendor templates, new sensor type `snmp_walk_count`, small `snmp_custom` /
template additions). All schema changes are in **migration `0005`**
([§ Phase 2b-2 migration](#2b-2-phase-2b-2-migration-0005)). Everything is **additive** to 2b-1: no column
changes meaning, no row is rewritten.

API shapes: [api.md § Phase 2b-2](api.md#2b-2-phase-2b-2-contract). Components and the faceplate parser:
[architecture.md § 11](architecture.md#11-phase-2b-2-switch-ports-view-vendor-template-library).

## [2b-2] Enum changes

| Name | Values |
|---|---|
| `sensor_type` | … 2a/2b-1 values, **[2b-2]** `snmp_walk_count` |
| `skip_reason` | `exists`, `probe_failed`, `snmp_error`, `max_reached`, `no_rows`, **[2b-2]** `not_applicable` |
| **[2b-2]** `inventory_mode` | `auto`, `on`, `off` (for `devices.port_inventory` and `devices.arp_collect`) |
| **[2b-2]** `port_kind` | `physical`, `lag`, `vlan`, `mgmt`, `other` |
| **[2b-2]** `port_state` | `up`, `down`, `admin_down`, `absent` (computed, not stored) |
| **[2b-2]** `port_mode` | `access`, `trunk` |
| **[2b-2]** `duplex` | `full`, `half`, `unknown` |
| **[2b-2]** `poe_detection` | `disabled`, `searching`, `delivering`, `fault`, `test`, `other_fault` |
| **[2b-2]** `connected_kind` | `none`, `client`, `ap`, `switch` (computed) |
| **[2b-2]** `uplink_reason` | `neighbor`, `mac_count` (computed) |
| **[2b-2]** `health_category` | `cpu`, `memory`, `temperature`, `poe`, `psu`, `fan` (computed) |
| **[2b-2]** `source_state` | `ok`, `empty`, `error`, `skipped`, `unmapped` |
| **[2b-2]** `template_source` | `builtin`, `user` |
| `confidence` (2b-1) | `high`, `medium`, `low` — now also used per template entry |

`device_class` is **unchanged** (no `firewall` / `ups` class): `router` keeps covering firewalls and
gateways; UPS / BMC templates match by enterprise / sysObjectID, not by class.

## [2b-2] Devices: inventory flags

`devices` gains:

| Column | Type | Constraints |
|---|---|---|
| port_inventory | text | NOT NULL DEFAULT `auto`, CHECK in inventory_mode (`devices_port_inventory_check`) |
| arp_collect | text | NOT NULL DEFAULT `auto`, CHECK in inventory_mode (`devices_arp_collect_check`) |

**Effective class** of a device (used by `auto`, by uplink detection and by the neighbour icon):
`devices.identity.device_class` when `identity` is not null, else `port_inventory_status.detected_class`
(below), else `unknown`.

**Eligibility** (evaluated by the scheduler on every pass, and by the API for `ports_effective` /
`arp_effective`):

| Flag | `on` | `auto` | `off` |
|---|---|---|---|
| ports (`port_inventory`) | device `enabled` | device `enabled` AND ≥ 1 sensor of a `requires_snmp` type AND effective class = `switch` | never |
| ARP (`arp_collect`) | device `enabled` | device `enabled` AND ≥ 1 `requires_snmp` sensor AND effective class = `router` | never |

**Class detection for devices without `identity`** (manually added, or imported before 2b-1): for an `auto`
device with ≥ 1 SNMP sensor whose `identity` is null and whose `detected_at` is null or older than 24 h, the
scheduler GETs `sysDescr.0`, `sysObjectID.0`, `sysName.0` (effective credentials **with the device's own
`retries`**) plus **one GETNEXT with the five `SUBTREE_PROBES` varbinds** (as discovery, so a Check Point
gateway behind the net-snmp Linux sysObjectID is found by the `checkpoint` probe), and runs the pure
`classify()` rules 1–12 (no FreeBSD walk, `snmp_ok = true`). The class is written to
`port_inventory_status.detected_class` / `detected_at`. 10 s budget per device.
**Detection failure** (SNMP error / timeout): the previous `detected_class` is **kept** (only a device that never
had a class gets `unknown`), and `detected_at = now − 23 h` so detection retries in ≈ 1 h; a failure never
demotes a known class. A later identify / import that fills `identity` takes precedence.
`PATCH /api/devices/{id}` clears `detected_at` (and makes the device due) only on a **real change**: `host`
differs from the stored host, or the merged `snmp_credentials` (after secret merging) differ from the stored
credentials. Re-sending unchanged credentials (the device form always sends them) changes nothing.

## [2b-2] `switch_ports` (latest snapshot per port)

One row per ifTable row of an inventoried device. Only the latest snapshot is kept: each **successful** run
upserts every row it saw and deletes the device's rows it did not see.

| Column | Type | Constraints / meaning |
|---|---|---|
| device_id | integer | NOT NULL, FK `devices.id` ON DELETE CASCADE |
| if_index | integer | NOT NULL. PK `(device_id, if_index)` |
| name | varchar(255) | NOT NULL DEFAULT '' — ifName, else ifDescr |
| descr | varchar(255) | NOT NULL DEFAULT '' — ifDescr |
| alias | varchar(255) | NOT NULL DEFAULT '' — ifAlias |
| if_type | integer | NULL — ifType |
| kind | text | NOT NULL CHECK in port_kind (rule below) |
| admin_status | text | NOT NULL — IF-MIB name (`up`, `down`, `testing`, `unknown`, `dormant`, `notPresent`, `lowerLayerDown`), as in `services/snmp_enum.IF_STATUS`; unknown codes → `unknown` |
| oper_status | text | NOT NULL — same names |
| speed_bps | bigint | NULL — ifHighSpeed × 10⁶ when > 0, else ifSpeed when > 0, else NULL |
| duplex | text | NULL CHECK in duplex — dot3StatsDuplexStatus: 1 → `unknown`, 2 → `half`, 3 → `full`; no row → NULL |
| mac | varchar(17) | NULL — ifPhysAddress, `aa:bb:cc:dd:ee:ff`; not 6 octets → NULL |
| last_change_at | timestamptz | NULL — see [last change](#last-change) |
| bridge_port | integer | NULL — dot1dBasePort mapped to this ifIndex |
| pvid | integer | NULL — dot1qPvid of `bridge_port` |
| mode | text | NULL CHECK in port_mode |
| untagged_vlans | integer[] | NOT NULL DEFAULT '{}' — sorted asc |
| tagged_vlans | integer[] | NOT NULL DEFAULT '{}' — sorted asc |
| poe_admin_enabled | boolean | NULL (no PoE row for the port → all `poe_*` NULL) |
| poe_detection | text | NULL CHECK in poe_detection |
| poe_class | smallint | NULL — 0–4 |
| poe_power_w | double precision | NULL — vendor per-port power, see [PoE](#poe-per-port) |
| in_octets / out_octets | numeric(20,0) | NULL — raw counters of this snapshot (HC when available) |
| counter_bits | smallint | NULL — 32 or 64 (which counters the octets are) |
| in_errors / out_errors | bigint | NULL — raw ifInErrors / ifOutErrors |
| in_bps / out_bps | double precision | NULL — rate since the previous snapshot |
| in_errors_delta / out_errors_delta | bigint | NULL — errors since the previous snapshot |
| mac_count | integer | NOT NULL DEFAULT 0 — present learned MACs on this port |
| collected_at | timestamptz | NOT NULL — time of the run that wrote the row |

Index: none beyond the PK.

**`kind`** (pure `netmon.ports.collect.port_kind(if_type, name)`), first match wins:
1. `name` matches `(?i)^(m-gigabitethernet|mgmt|management|oobm|mgmteth|ma\d)` → `mgmt`;
2. `if_type` ∈ {6 ethernetCsmacd, 62 fastEther, 69 fastEtherFX, 117 gigabitEthernet} → `physical`;
3. `if_type` ∈ {161 ieee8023adLag, 54 propMultiplexor} → `lag` (ArubaOS `Trk*`, Comware `Bridge-Aggregation*`;
   medium);
4. `if_type` ∈ {53 propVirtual, 136 l3ipvlan, 135 l2vlan} → `vlan`;
5. else `other`.

**Rates.** With the previous row of the same port (same `counter_bits`):
`Δt = collected_at − previous.collected_at` (seconds, must be > 0); `Δ = octets − previous.octets`; `Δ < 0` with
32 bits → `Δ + 2³²`; `Δ < 0` with 64 bits → rate NULL; `in_bps = Δ × 8 / Δt` (rounded to 1 decimal). No
previous row, a changed `counter_bits`, or a missing counter → NULL. Error deltas: `Δ ≥ 0` → Δ, `Δ < 0` →
NULL (counter reset / 32-bit wrap is not corrected for errors). Counters come from ifHCInOctets/ifHCOutOctets
when that column answered for the port (`counter_bits = 64`), else ifInOctets/ifOutOctets (32).

### Last change
`ifLastChange` (TimeTicks of sysUpTime at the last oper change). With `sysUpTime.0` from the same run:
`ifLastChange = 0` or `ifLastChange > sysUpTime` → NULL; else
`last_change_at = collected_at − (sysUpTime − ifLastChange) / 100 s`. (sysUpTime wraps after 497 days; the
value is then wrong, accepted.)

## [2b-2] `port_macs` (MAC table with first/last seen)

| Column | Type | Constraints / meaning |
|---|---|---|
| id | bigint | PK, GENERATED ALWAYS AS IDENTITY |
| device_id | integer | NOT NULL, FK `devices.id` ON DELETE CASCADE |
| if_index | integer | NOT NULL — the port it was last learned on |
| mac | varchar(17) | NOT NULL — `aa:bb:cc:dd:ee:ff` |
| vlan | integer | NULL — NULL when the FDB came from BRIDGE-MIB (no VLAN) |
| present | boolean | NOT NULL DEFAULT true — in the latest successful FDB read |
| first_seen | timestamptz | NOT NULL — first seen **on this port** |
| last_seen | timestamptz | NOT NULL |

Indexes: `ux_port_macs_key` UNIQUE on `(device_id, mac, COALESCE(vlan, -1))`, `ix_port_macs_mac` on `(mac)`,
`ix_port_macs_mac_prefix` on `(mac varchar_pattern_ops)` (MAC prefix search),
`ix_port_macs_device_if` on `(device_id, if_index)` WHERE `present`.

Write rules for a run whose FDB read succeeded (`sources.fdb` = `ok` or `empty`):
- every learned entry → upsert by key: `present = true`, `last_seen = now`; a new row gets `first_seen = now`; an
  existing row whose `if_index` changed (the MAC moved) gets the new `if_index` and `first_seen = now`;
- every other row of the device with `present = true` → `present = false` (`last_seen` kept);
- `switch_ports.mac_count` = number of present rows per port.
A run whose FDB read failed (`error`) leaves `port_macs` and `mac_count` untouched.
**Retention:** a daily task deletes rows with `present = false AND last_seen < now − port_mac_retention_days`.

## [2b-2] `arp_entries` (MAC → IP from L3 devices)

| Column | Type | Constraints / meaning |
|---|---|---|
| source_device_id | integer | NOT NULL, FK `devices.id` ON DELETE CASCADE |
| ip | varchar(45) | NOT NULL — IPv4 dotted text (IPv6 entries are ignored in 2b-2). PK `(source_device_id, ip)` |
| mac | varchar(17) | NOT NULL |
| if_index | integer | NULL |
| present | boolean | NOT NULL DEFAULT true |
| first_seen, last_seen | timestamptz | NOT NULL (`first_seen` resets when the IP's MAC changes) |

Indexes: `ix_arp_entries_mac` on `(mac)`, `ix_arp_entries_ip` on `(ip)`. Same present/retention rules as
`port_macs` (keyed by IP), using `port_mac_retention_days`.

**MAC → IP** (read side): the present rows for that MAC over all sources; `ip` = the one with the newest
`last_seen` (ties: lowest IP numerically), `ips` = all of them sorted numerically. If none is present, the
newest non-present row is used for `ip` (shown as last known), and `ips` is `[]`.

## [2b-2] `dns_cache` (reverse DNS)

| Column | Type | Constraints |
|---|---|---|
| ip | varchar(45) | PK |
| name | varchar(255) | NULL (NULL = no PTR / lookup failed) |
| resolved_at | timestamptz | NOT NULL |

- Filled **only by the inventory job** (never on an API request), after the ARP read, for IPs of present
  `arp_entries` whose cache row is missing, older than 24 h (name found) or older than 1 h (no name).
- Lookup: `socket.getnameinfo((ip, 0), socket.NI_NAMEREQD)` (system resolver, works on Windows) run in a
  **dedicated module-level `ThreadPoolExecutor(max_workers=8)`** (`loop.run_in_executor(_dns_pool, …)`), never
  the default executor (pysnmp resolves every SNMP target through `loop.getaddrinfo` on the default executor;
  slow PTR lookups there would stall all polling). 2 s timeout, at most 8 concurrent **in total** (one
  module-level `asyncio.Semaphore(8)` shared by all devices), at most 256 lookups per job pass. The trailing dot is stripped; names are lower-cased.
- Off when `settings.reverse_dns = false` (the cache is then not read either: `hostname` is null).
- Test hook: with `NETMON_TEST_MODE=1`, `NETMON_TEST_PTR_FILE` (a JSON object `{"10.0.0.11": "pc.lan"}`)
  replaces the resolver; IPs missing from the file resolve to no name.
- Rows older than 7 days are deleted by the daily task.

## [2b-2] `port_inventory_status`

| Column | Type | Constraints / meaning |
|---|---|---|
| device_id | integer | PK, FK `devices.id` ON DELETE CASCADE |
| last_run_at | timestamptz | NULL — start of the last run (ok or not) |
| last_ok_at | timestamptz | NULL — start of the last run whose ifTable read succeeded |
| duration_ms | integer | NULL |
| error | text | NULL — last failure (`Timeout`, `ifTable not available`, SNMP error text), NULL after an ok run |
| ports | integer | NOT NULL DEFAULT 0 |
| macs | integer | NOT NULL DEFAULT 0 — present `port_macs` rows |
| arp_entries | integer | NOT NULL DEFAULT 0 — present `arp_entries` rows with this device as source |
| sources | jsonb | NOT NULL DEFAULT '{}' — see below |
| detected_class | text | NULL CHECK in device_class |
| detected_at | timestamptz | NULL |

`sources` keys (each a `source_state`): `if_xtable`, `duplex`, `bridge`, `vlans`, `fdb`, `poe`, `arp`, plus
`fdb_mib` (`"q_bridge"` \| `"bridge"` \| null) and `vlan_mib` (`"static"` \| `"current"` \| null). `skipped` =
not attempted (v1 for ifXTable; ARP not enabled; ports not enabled on an ARP-only device). `empty` = the walk
answered with no rows (the MIB is not implemented). `unmapped` = PoE rows exist but could not be mapped to
ifIndexes.

## [2b-2] Settings keys
| Key | Type | Default | Limits | Meaning |
|---|---|---|---|---|
| `port_inventory_min` | int | `10` | `0` (off) or 5–1440 | Interval of the port inventory / ARP job per device |
| `uplink_mac_threshold` | int | `5` | 1–1000 | A port with **more than** this many present MACs is an uplink |
| `port_mac_retention_days` | int | `30` | 1–365 | Retention of non-present `port_macs` / `arp_entries` rows |
| `reverse_dns` | bool | `true` | | Reverse DNS for ARP IPs |

## [2b-2] Port inventory: SNMP data

All walks use the device's effective credentials and the shared SNMP client (`snmp_walk`, per-v3-credential
engines). Walk caps: ifTable/ifXTable/dot1d/dot1q/PoE columns 5000 rows each, FDB and ARP 20000 rows each.
A walk that returns **exactly the cap** is treated as truncated: that source is `error` (the previous data is
kept, a WARNING is logged), so a truncated FDB/ARP never marks the tail `present = false`.

| # | Object | OID | Index | Required | Confidence |
|---|---|---|---|---|---|
| 1 | sysUpTime.0 | `1.3.6.1.2.1.1.3.0` (GET) | scalar | no | high |
| 2 | ifDescr / ifType / ifSpeed / ifPhysAddress / ifAdminStatus / ifOperStatus / ifLastChange | `1.3.6.1.2.1.2.2.1` `.2` `.3` `.5` `.6` `.7` `.8` `.9` | ifIndex | **ifDescr yes** (no rows or an error → run failed) | high |
| 3 | ifInOctets / ifInErrors / ifOutOctets / ifOutErrors | `1.3.6.1.2.1.2.2.1` `.10` `.14` `.16` `.20` | ifIndex | no | high |
| 4 | ifName / ifHCInOctets / ifHCOutOctets / ifHighSpeed / ifAlias | `1.3.6.1.2.1.31.1.1.1` `.1` `.6` `.10` `.15` `.18` | ifIndex | no (skipped on v1) | high |
| 5 | dot3StatsDuplexStatus | `1.3.6.1.2.1.10.7.2.1.19` | dot3StatsIndex = ifIndex | no | high |
| 6 | dot1dBasePortIfIndex | `1.3.6.1.2.1.17.1.4.1.2` | dot1dBasePort | no | high |
| 7 | dot1qPvid | `1.3.6.1.2.1.17.7.1.4.5.1.1` | dot1dBasePort | no | high |
| 8 | dot1qVlanStaticEgressPorts / UntaggedPorts | `1.3.6.1.2.1.17.7.1.4.3.1.2` / `.4` | dot1qVlanIndex | no | high |
| 8b | fallback: dot1qVlanCurrentEgressPorts / UntaggedPorts | `1.3.6.1.2.1.17.7.1.4.2.1.4` / `.5` | dot1qVlanTimeMark.dot1qVlanIndex | used only when 8 has no rows | high |
| 9 | dot1qVlanFdbId | `1.3.6.1.2.1.17.7.1.4.2.1.3` | TimeMark.VlanIndex → FdbId | no | high |
| 10 | dot1qTpFdbPort / dot1qTpFdbStatus | `1.3.6.1.2.1.17.7.1.2.2.1.2` / `.3` | dot1qFdbId.<6 MAC octets> | no | high |
| 10b | fallback: dot1dTpFdbPort / dot1dTpFdbStatus | `1.3.6.1.2.1.17.4.3.1.2` / `.3` | <6 MAC octets> | used only when 10 has no rows | high |
| 11 | pethPsePortAdminEnable / DetectionStatus / PowerClassifications | `1.3.6.1.2.1.105.1.1.1.3` / `.6` / `.10` | pethPsePortGroupIndex.pethPsePortIndex | no | high |
| 12 | ipNetToPhysicalPhysAddress / Type | `1.3.6.1.2.1.4.35.1.4` / `.6` | ifIndex.addrType.addrLen.<addr octets> | ARP sources only | high |
| 12b | fallback: ipNetToMediaPhysAddress / Type | `1.3.6.1.2.1.4.22.1.2` / `.4` | ifIndex.a.b.c.d | used only when 12 gives no IPv4 rows | high |

Vendor notes: **Cisco IOS** keeps the FDB per VLAN (`community@vlan` / v3 context): the MAC table is usually
empty → `sources.fdb = empty` (documented limitation, no context support in 2b-2). **Aruba CX** Q-BRIDGE and
FDB support: medium (the CX sim exercises both fallbacks). **Comware** fdbId ≠ VLAN on some releases: handled by
#9.

### Pure parsers (`netmon.ports.collect`, exhaustively unit-tested)
- **`portlist(octets: bytes) -> set[int]`** (Q-BRIDGE PortList): bit 7 (MSB) of octet 0 = bridge port 1, bit 0
  of octet 0 = port 8, MSB of octet 1 = port 9, … A non-bytes value → empty set.
- **Bridge port → ifIndex:** from #6. When #6 has no rows, bridge port `n` → ifIndex `n` if that ifIndex
  exists (ArubaOS-Switch style; `sources.bridge = empty`).
- **VLANs per port** (bridge port `p`): `untagged_vlans = {v : p ∈ Untagged_v}`,
  `tagged_vlans = {v : p ∈ Egress_v \ Untagged_v}`; `mode = trunk` if `tagged_vlans` ≠ ∅, else `access` if
  `untagged_vlans` ≠ ∅, else NULL. `pvid` = #7 for `p`; when #7 is missing and exactly one untagged VLAN exists,
  that VLAN. Ports without a bridge port: all NULL / empty.
- **FDB** (#10): suffix = `fdbId.m1.m2.m3.m4.m5.m6`; keep rows with status **learned(3)** only; bridge port 0 or
  unmapped → dropped. `vlan` = the VLAN whose dot1qVlanFdbId (#9) equals `fdbId` (lowest VLAN when several);
  when #9 has no rows, `vlan = fdbId`. **Fallback** (#10b): suffix = `m1…m6`, status learned(3) only,
  `vlan = NULL`. Duplicate (mac, vlan) rows → the first by suffix order wins.
- **ARP** (#12): suffix `ifIndex.1.4.a.b.c.d` = IPv4 (addrType 1 = ipv4, length 4); other addrTypes ignored.
  (Some agents omit the length arc: `ifIndex.1.a.b.c.d` is also accepted.) Keep Type ∈ {3 dynamic, 4 static}
  (drop 1 other, 2 invalid, 5 local). MAC must be 6 octets and not `00:00:00:00:00:00` / `ff:ff:ff:ff:ff:ff`.
  **Fallback** (#12b): suffix `ifIndex.a.b.c.d`, Type ∈ {3, 4} (drop 1 other, 2 invalid).
- **MAC text:** 6 octets → lower-case `aa:bb:cc:dd:ee:ff`. **Locally administered** = bit 0x02 of the first
  octet (random/private MACs of phones and VMs).

### PoE per port
- `pethPsePortDetectionStatus`: 1 disabled, 2 searching, 3 delivering (deliveringPower), 4 fault, 5 test,
  6 other_fault. `pethPsePortAdminEnable`: 1 → true, 2 → false. `pethPsePortPowerClassifications`: 1–5 →
  class 0–4.
- **(group, port) → ifIndex** (pure `map_pse_ports(keys, ports)`), first rule that maps every row:
  1. every PSE `port` value is distinct and is the ifIndex of a `physical` port → ifIndex = `port`
     (ArubaOS-Switch standalone; Comware, medium);
  2. else, using the **name tail** of each physical port — member = the first number in the name when the name
     has ≥ 2 numbers, else 1; port number = the last number — (group, port) → the physical port with
     member = `group` and port number = `port` (ArubaOS stacks `2/14`, CX `1/1/14`; medium);
  3. else nothing is mapped and `sources.poe = unmapped`.
- **`poe_power_w`** (per-port power): RFC 3621 has no per-port power column. No vendor OID is used in 2b-2 (the
  architect is not confident about the HP-ICF-POE-MIB / HH3C PSE port power columns) → always NULL. The code
  keeps a table `PORT_POWER_OIDS: dict[str, tuple[str, float]]` (vendor slug → column OID indexed like
  pethPsePortTable, multiplier to W), **empty in 2b-2**; the owner's snmpwalk (architecture § 11.8) fills it.

### Uplinks, neighbours and tile fields (computed on read, pure `netmon.ports.view`)
Inputs: the port row, non-stale `topology_links` of the device, the effective class of linked devices, the
linked interface sensor, `settings.uplink_mac_threshold`.

- **Neighbours of a port**: non-stale links (any source, manual included) where (`a_device_id` = device AND
  (`a_if_index` = if_index OR (`a_if_index` IS NULL AND lower(`a_port`) ∈ {lower(name), lower(descr)}))) or
  the same on the `b_*` side. Sorted: device neighbours before unmanaged neighbours, then link id. The
  **primary neighbour** is the first.
- **is_uplink** = some neighbour is a device whose effective class ∈ {`switch`, `router`, `access_point`}
  (`uplink_reason = neighbor`), else `mac_count > uplink_mac_threshold` (`uplink_reason = mac_count`), else false
  (`uplink_reason = null`).
- **state**: `admin_status = down` → `admin_down`; else `oper_status = up` → `up`; else
  `oper_status = notPresent` → `absent`; else `down`.
- **alert** (never for `admin_down`) = any of the `alert_reasons`, in this order:
  `sensor` (the linked `snmp_interface` sensor is `down` or `warning`), `errors`
  (`in_errors_delta + out_errors_delta > 0`, NULLs as 0), `poe_fault` (`poe_detection` ∈ {`fault`,
  `other_fault`}).
- **connected_kind**: state ≠ `up` → `none`; primary neighbour is a device of class `switch` or `router` →
  `switch`; class `access_point` → `ap`; otherwise → `client` (also for a MAC-count uplink and for unmanaged
  LLDP neighbours such as phones).
- **trunk** = `mode = trunk`.
- **Linked sensor**: the `snmp_interface` sensor of the device with `config.if_index = if_index` (lowest id).
- **MAC list of a port**: present `port_macs` rows sorted by `vlan` (NULL first), then `mac`. Hidden (count
  only) when `is_uplink`. **first_mac** (summary) = the first of the list ordered by (has an IP desc,
  `last_seen` desc, `mac`); null for uplinks.
- **Read cost:** `GET /api/devices/{id}/ports` loads MAC details (ARP/IP, DNS, OUI, monitored device) only for
  ports that are not uplinks (uplink ports need only `mac_count`); the port detail with
  `include_uplink_macs=true` loads them for that port (capped at 1000). The device map used for neighbours is
  built once per request, not per port / search row.

### Health strip (computed on read, pure `netmon.ports.health.categorize(sensor) -> category | None`)
Over the device's sensors, first matching rule wins (regexes case-insensitive, on the sensor **name**):

| # | Rule | Category |
|---|---|---|
| 1 | name `^poe\b` | `poe` |
| 2 | `snmp_custom` and (name `temp\|טמפרטורה` or `config.unit` ∈ {`°C`, `C`}) | `temperature` |
| 3 | type `snmp_cpu`, or type `snmp_custom` and name `\bcpu\b\|מעבד` | `cpu` |
| 4 | type `snmp_memory`, or `snmp_custom` and name `\bmem(ory)?\b\|זיכרון` | `memory` |
| 5 | `snmp_custom` and name `\bfans?\b\|cooling\|מאוורר` | `fan` |
| 6 | `snmp_custom` and name `power supply\|\bpsu\b\|\bpower\b\|ספק כוח` | `psu` |

One item per category that has ≥ 1 sensor, order cpu, memory, temperature, poe, psu, fan:
- `status` = worst (severity) over the category's non-paused sensors (`paused` when all are paused);
- `value`/`unit`: cpu, memory, poe → the max primary-channel `last_value` over sensors whose unit is `%`
  (null if none), unit `%`; temperature → max over sensors with unit `°C`/`C`, unit `°C`; psu, fan → null, `""`;
- `text`: psu, fan, and temperature without a value → `"<n up>/<n total>"`; others `""`;
- `sensor_ids` sorted by id; `primary_sensor_id` = the worst sensor (ties: higher value, then lowest id).
Temperature is checked before cpu/memory so readings named after a component ("Temperature CPU" on Check Point,
"CPU temperature" on MikroTik) land in `temperature`; a status sensor without `temp`/°C such as HPE `CPU Board`
stays `cpu` (2b-2 QA finding #3).
These rules cover the 2b-1 HPE names (`Fan Sensor`, `Power Supply Sensor`, `Over-temperature Sensor`,
`CPU Board`, `PSU 1`, `PoE 1 usage`, …) and the 2b-2 templates. Renamed sensors may leave the strip.

### OUI vendor lookup (`netmon.ports.oui`)
- Data: **`backend/netmon/data/oui.tsv.gz`** (package data). Gzip (deterministic: `mtime=0`) of UTF-8 text,
  sorted by prefix, one line per IEEE **MA-L** assignment: `AABBCC<TAB>Organization Name`. Lines starting with
  `#` are header comments: `# source: https://standards-oui.ieee.org/oui/oui.csv`, `# retrieved: <date>`,
  `# entries: <n>`. Names: whitespace collapsed, trimmed, cut to 64 chars. Expected size ≈ 38 000 lines,
  **≈ 0.4–0.6 MB** gzipped (QA asserts < 1 MB).
- Source and licence: the IEEE Registration Authority public MA-L listing (`oui.csv`), freely downloadable
  from IEEE; it is factual registry data. The same file is redistributed by permissively licensed projects
  (e.g. Python `netaddr`, BSD). The repo's `NOTICE`/README credits "OUI data © IEEE Registration Authority,
  from the public MA-L listing". (Not legal advice: the owner may confirm.) MA-M / MA-S (28/36-bit blocks) are
  **not** included in 2b-2.
- Lookup: first 3 octets → name, else null. Loaded lazily once per process (dict ≈ 4 MB). A locally
  administered MAC returns null vendor (`locally_administered: true` in the API) without a lookup.
- Updates: maintainers run `python backend/scripts/update_oui.py` (stdlib only: urllib, csv, gzip; honours
  `HTTPS_PROXY`), which downloads `oui.csv`, writes the file and prints the entry count. **No runtime
  download.**

## [2b-2] New sensor type `snmp_walk_count`
Counts the rows of one table column, and the rows whose integer value is in a set (e.g. "APs up", "VPN tunnels
up", "Wi-Fi clients"). `requires_snmp: true`.

Config:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `oid` | str | **required** | Numeric column OID (same regex as `snmp_custom.oid`, leading dot stripped) |
| `count_values` | int_list | null | Values counted as `matched`; null = every row matches |
| `label` | str | `""` | 0–64 chars |
| `max_rows` | int | `5000` | 1–10000 (walk cap) |

Channels (primary `matched`), unit `#`, no default thresholds: `rows`, `matched`, `unmatched`.

- Walk error/timeout → check failed (message = the SNMP error). No rows → ok, all channels 0.
- An integer value in `count_values` → matched; anything else (other integers, non-integers) → unmatched.
  `count_values = null` → `matched = rows`, `unmatched = 0`.
- `status_message` = `"<label or oid>: <matched> of <rows>"`.
- Idempotency identity: `(type, oid, tuple(sorted(count_values)) | None)`.
- **Template probe rule:** a `probe: true` entry of this type is created only when the probe is ok **and**
  `rows ≥ 1`; otherwise it is skipped `probe_failed` with message `No rows`.
- Default name (API): `Count <oid>`. i18n `sensorType.snmp_walk_count` = "SNMP row count" / "ספירת שורות SNMP".

## [2b-2] `snmp_custom`: `percent_base`
New config key `percent_base`: `"total"` (default, unchanged 2a behaviour: `raw / divisor × 100`) or `"sum"`:
`value = raw / (raw + other) × 100`, where `percent_of_oid` names the *other part* (e.g. free memory next to
used memory, CISCO-MEMORY-POOL has no total column). With `"sum"`: `raw < 0`, `other < 0` or
`raw + other ≤ 0` → ok, no value (as `percent_of_oid` today). The key is stored only when given (absent = `"total"`, so the stored config of existing
sensors is unchanged). `"sum"` without `percent_of_oid` → 422 (loc
`["body","config","percent_base"]`). A PATCH changing the **effective** `percent_base` resets `sensors.state`
(compared normalised: `config.get("percent_base") or "total"` on both sides, so writing `"total"` to a sensor
that had no key is not a change). Not part of the
idempotency identity. `GET /api/sensor-types` lists it as type `enum`, options `["total","sum"]`.

## [2b-2] Template library (data files)

Templates move from `netmon/templates/builtin.py` to YAML files. **Behaviour of the nine 2a/2b-1 templates is
identical** (same entries, order, configs, thresholds, names, priorities, match).

### Format decision: YAML
YAML (PyYAML ≥ 6, **`yaml.safe_load` only**) over JSON because templates are hand-written by maintainers and
users: comments carry the per-OID notes, regexes need no double escaping, value maps can be shared inside a
file with anchors (`&map` / `*map`), and diffs stay readable. New backend dependency `PyYAML>=6.0.1` (wheels for
Windows/Linux/macOS). `.json` files are also accepted (parsed with `json.loads`, same schema) for generated
templates (the later UI editor).

YAML pitfalls the loader **defends against** (the schema below rejects them): unquoted `on`/`off`/`yes`/`no`
become booleans; unquoted `1.3.6.1…` OIDs are strings already but `1.10` would be a float → OIDs must be
strings (quote them: `'1.3.6.1.2.1.1.3.0'`); value-map keys may be YAML ints or strings and are normalised to
decimal strings.

### Locations, precedence
- **Built-in library:** `backend/netmon/templates/library/<vendor>/<id>.yaml` (package data;
  `pyproject.toml` `package-data` gains `templates/library/*/*.yaml` and `data/*.gz`).
  Vendor folders: `generic`, `ubiquiti`, `opnsense`, `hpe`, `aruba`, `checkpoint`, `fortinet`, `cisco`,
  `paloalto`, `mikrotik`, `apc`, `dell`.
- **User directory:** `${NETMON_DATA_DIR}/templates/` (created empty at startup if missing), read recursively,
  files `*.yaml`, `*.yml`, `*.json` (case-insensitive extension), max 200 files, each ≤ 256 KiB.
- A **user template replaces the built-in template with the same id** entirely (`source: "user"`,
  `overrides_builtin: true`). A user file that fails validation does not override (the built-in stays, the
  error is reported).
- The file name stem must equal `id` (`hpe/hpe_comware.yaml` → `hpe_comware`).
- Duplicate ids inside one source: files are processed in sorted relative-path order (forward slashes); the
  first valid one wins, later ones are errors.
- Loaded at startup and by `POST /api/device-templates/reload`. The registry is swapped atomically; a reload
  never touches existing sensors.
- Final list order (API, suggestion): `priority` desc, then `id` asc. (Same order as 2b-1 for the nine
  existing templates.)

### Schema (`schema: 1`)
Unknown keys anywhere → error. "default" = value used when the key is absent.

**Template (top level)**

| Key | Type | Default | Rules |
|---|---|---|---|
| `schema` | int | **required** | must be `1` (else `unsupported schema version: <n>`) |
| `id` | str | **required** | `^[a-z][a-z0-9_]{1,31}$`, equals the file stem |
| `version` | int | `1` | ≥ 1; bumped by the author on every change (informational) |
| `name` | `{en, he}` | **required** | `en` required (1–128 chars); `he` optional, default = `en` |
| `description` | `{en, he}` | `{en:"", he:""}` | ≤ 500 chars each; `he` default = `en` |
| `vendor` | str \| null | null | informational slug |
| `verified` | bool | `false` | true only after a test on real equipment |
| `verified_note` | str | `""` | ≤ 200 chars, e.g. `"2930F WC.16.11, 2026-11"` |
| `requires_snmp` | bool | `false` | |
| `priority` | int | **required** | 0–1000 |
| `match` | rule[] | **required** | 1–20 rules |
| `entries` | entry[] | **required** | 1–100 entries |

**Match rule** (all keys optional; absent = null = "any", except `requires_snmp` default false). A rule matches
when every non-null field matches; a template matches when any rule matches.

| Key | Type | Matches when |
|---|---|---|
| `device_classes` | device_class[] | result class in list |
| `vendors` | str[] | result vendor in list |
| `requires_snmp` | bool | true → `snmp_ok` |
| `any_open_ports` | int[] | ≥ 1 of them open |
| `enterprises` | int[] | (2b-1) enterprise of sysObjectID in list |
| **[2b-2]** `sys_object_id_prefixes` | str[] | sysObjectID (leading dot stripped) equals a prefix or starts with `prefix + "."` (prefixes: numeric OIDs, no trailing dot) |
| **[2b-2]** `sys_descr_regex` | str | `re.search(regex, sys_descr or "", re.IGNORECASE)` (must compile) |

`suggest()` / `template_matches()` / `rule_matches()` gain the keyword argument `sys_descr: str | None = None`;
`classify.suggest_template()` passes the result's `sys_descr`.

**Entries — common keys** (in addition to the 2a/2b-1 fields)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `kind` | `sensor` \| `snmp_interfaces` \| `snmp_table` | **required** | |
| `confidence` | `high` \| `medium` \| `low` \| null | null | Confidence in the OID(s) of this entry. **Required (non-null) for entries that read a vendor OID** (an OID under `1.3.6.1.4.1.`), i.e. `snmp_custom`/`snmp_walk_count` sensors and `snmp_table`s with a vendor `walk_oid`; null allowed elsewhere. |
| `notes` | str | `""` | ≤ 500 chars, English, shown in the template details (maintainer notes, MIB name) |

**`sensor` entry**: `type` (required, a sensor type), `name` (required `{en, he?}`), `config` (`{}`),
`channels` (`[]`), `interval_s` (null, else 10–86400), `probe` (false), **[2b-2]** `when` (`[]`).
`config` must pass `validate_config(type, config)` and `channels` `validate_channel_overrides`.

**[2b-2] `when`** (sensor entries only): list of `{oid, in, not_in}` (same shape as `snmp_table.filters`, but
`oid` is a complete instance OID). At apply time each OID is GET (one GET for all); the entry is created only
when every value is an integer passing `in`/`not_in`. Otherwise it is skipped with reason **`not_applicable`**,
message `Condition not met: <oid>` (missing OID / SNMP error → also `not_applicable`). Order per entry:
`exists` check → `when` → `probe`. `when` GETs share the apply budget and the per-device probe semaphore.

**`snmp_interfaces` entry**: `oper_up_only` (true), `exclude_if_types` (`[24]`), `include_name_regex` (null),
`exclude_name_regex` (**absent → `IF_EXCLUDE`**, explicit `null` → none), `max` (**required**, 1–1000),
`channels` (`[]`).

**`snmp_table` entry**: `walk_oid` (**required**), `name` (**required**), `config` (**required**; validated as
`snmp_custom` config with a dummy `oid`, without `oid`/`percent_of_oid`/`label`), `label_oid` (null),
`label2_oid` (null), `percent_of_column` (null), `channels` (`[]`), `max` (8, 1–1000), `skip_zero` (false),
`filters` (`[]`), `interval_s` (null).

All OIDs: strings matching `^\.?[0-2](\.\d+){1,127}$` (leading dot stripped). Regexes must compile.
`value_map` keys must be integers (YAML int or decimal string).

The in-memory / API template dict = the 2b-1 dict **plus** `version`, `verified`, `verified_note`, `vendor`,
`source`, `overrides_builtin`, `file` (path relative to its root, forward slashes), entries plus `confidence`,
`notes`, sensor entries plus `when`, rules plus `sys_object_id_prefixes`, `sys_descr_regex`. Absent keys are
filled with their defaults, so every key is always present.

### Load errors
Pure `netmon.templates.library.load_dir(root, source) -> (templates, errors)`; any error rejects the **whole
file** (other files still load). The per-file handler also catches `RecursionError` (deeply nested YAML/JSON →
`invalid YAML: nesting too deep` / `invalid JSON: nesting too deep`) and, as a safety net, any other exception
(`invalid value: <exception text>`), so no file can make a reload fail or drop the other user templates. The
size cap (`file too large`, 256 KiB) is checked before reading.

```json
{ "source": "user", "file": "acme/acme_ups.yaml", "template_id": "acme_ups",
  "path": "entries[2].config.value_map", "message": "value_map keys must be integers" }
```

`template_id` is null when the id could not be read; `path` is `""` for file-level errors. Messages (English,
fixed prefixes so tests can match): `invalid YAML: <parser message>` / `invalid JSON: …`, `file too large`,
`too many files` (once, for the files beyond 200), `not a mapping`, `unsupported schema version: <n>`,
`missing key: <key>`, `unknown key: <key>`, `invalid value: <detail>`, `id must match the file name (expected
"<stem>")`, `duplicate id "<id>" (also in <file>)`, `invalid OID: <oid>`, `invalid regex: <detail>`, `unknown
sensor type: <type>`, `invalid config: <validate_config message>`, `invalid channels: <message>`,
`confidence required for vendor OIDs`. The **built-in library must load with zero errors** (a unit test);
at runtime built-in errors are logged at ERROR and reported like user errors.

### Module layout and compatibility
- `netmon/templates/library.py`: schema validation + `load_dir()` (reads files; otherwise pure).
- `netmon/templates/registry.py`: `Registry` (templates sorted, `by_id`, `errors`, `loaded_at`), `reload(rt)`,
  `current()`. Reload is synchronous file I/O run via `asyncio.to_thread`.
- `netmon/templates/builtin.py` keeps the pure helpers `enterprise()`, `rule_matches()`,
  `template_matches()`, `suggest()`, `IF_EXCLUDE`, `SKIP_REASONS` (+ `not_applicable`) and the functions
  `list_templates()` / `get_template(id)` (deep copies from `registry.current()`). `TEMPLATES` stays a
  module-level **list that is updated in place** on every (re)load; `BY_ID` likewise (dict updated in place).
  `TEMPLATE_IDS` (frozenset) is **removed**: `api/discovery.py` validates with `registry.current().by_id`.
  At import, `builtin.py` loads the built-in library (package files only, no user dir) so pure unit tests work
  without a runtime.
- Tests: `test_discovery_units.py::test_templates_data` is **updated** for 2b-2 (the id list gains the new
  templates in the order above; the key sets gain the new keys). All other existing tests stay unchanged and
  must pass. **Identity proof:** before deleting the Python data, a fixture
  `backend/tests/fixtures/templates_2b1.json` is generated from the 2b-1 `builtin.TEMPLATES`; a test asserts that
  the loaded nine templates, with every 2b-2-only key removed, equal the fixture exactly (including order).

### Example file
```yaml
# backend/netmon/templates/library/hpe/hpe_arubaos_switch.yaml
schema: 1
id: hpe_arubaos_switch
version: 2
vendor: hp
verified: false            # flip after the owner's 2b-1 test (architecture § 11.8)
verified_note: ""
name: {en: "HPE ArubaOS-Switch / ProCurve", he: "מתג HPE ArubaOS"}
description:
  en: "Ping, system, CPU, memory, hardware health, PoE and active ports"
  he: "פינג, מערכת, מעבד, זיכרון, תקינות חומרה, PoE ופורטים פעילים"
requires_snmp: true
priority: 50
match:
  - {device_classes: [switch], requires_snmp: true, enterprises: [11]}
entries:
  - {kind: sensor, type: ping, name: {en: Ping, he: פינג}}
  - {kind: sensor, type: snmp_system, name: {en: System, he: מערכת}}
  - kind: sensor
    type: snmp_custom
    name: {en: CPU, he: מעבד}
    config: {oid: '1.3.6.1.4.1.11.2.14.11.5.1.9.6.1.0', value_type: gauge, unit: '%', label: CPU}
    channels: [{key: value, warning_max: 80, down_max: 95}]
    probe: true
    confidence: high
    notes: "hpSwitchCpuStat (STATISTICS-MIB). May be absent on OfficeConnect 1820/1920S."
  - kind: snmp_table
    walk_oid: '1.3.6.1.4.1.11.2.14.11.1.2.6.1.4'      # hpicfSensorStatus
    label_oid: '1.3.6.1.4.1.11.2.14.11.1.2.6.1.7'     # hpicfSensorDescr
    name: {en: '{label}', he: '{label}'}
    config:
      value_type: enum
      value_map: &hpicf
        1: {status: warning, text: unknown}
        2: {status: down, text: bad}
        3: {status: warning, text: warning}
        4: {status: up, text: good}
      value_map_default: warning
    filters: [{oid: '1.3.6.1.4.1.11.2.14.11.1.2.6.1.4', not_in: [5]}]
    max: 16
    interval_s: 300
    confidence: medium
  # … memory, PoE ×3, snmp_interfaces exactly as in 2b-1 …
  - {kind: snmp_interfaces, oper_up_only: true, exclude_if_types: [1, 24, 53, 131, 136], max: 52}
```

### Converted templates (identical behaviour)
| id | file | verified |
|---|---|---|
| `generic_ping` | `generic/generic_ping.yaml` | true (no OIDs) |
| `http_server` | `generic/http_server.yaml` | true (no OIDs) |
| `generic_snmp` | `generic/generic_snmp.yaml` | true (standard MIBs) |
| `nas_server` | `generic/nas_server.yaml` | false |
| `printer` | `generic/printer.yaml` | false |
| `unifi_ap` | `ubiquiti/unifi_ap.yaml` | false |
| `opnsense` | `opnsense/opnsense.yaml` | false |
| `hpe_arubaos_switch` | `hpe/hpe_arubaos_switch.yaml` | false (until the owner confirms) |
| `hpe_comware` | `hpe/hpe_comware.yaml` | false (until the owner confirms) |

Confidence values for the converted vendor rows are the ones in the 2b-1 tables (`medium-high` → `medium`).

## [2b-2] Classification changes
- **Vendor table additions:**

  | Enterprise | Vendor slug | Default class (rule 10) | Confidence | Notes |
  |---|---|---|---|---|
  | 232 | hp | server | high | Compaq/HPE server management (iLO, Insight agents) |
  | 2620 | checkpoint | router | high | Check Point (Gaia) |

- **Subtree probe addition:** `checkpoint` = `1.3.6.1.4.1.2620.1.6` (svn MIB; present on every Gaia gateway,
  also when sysObjectID is the net-snmp Linux OID). The probe GETNEXT carries five varbinds.
- **New rule 9a** (after rule 9, before rule 10): enterprise 2620 **or** probe `checkpoint` → `router` /
  `checkpoint`. `classified_by = snmp`, `confidence = high` (like rules 1–10).
- **Fingerprint rule F23a** (after F23): T or M `check point|gaia portal|\bgaia\b` → router / checkpoint,
  medium. Markers gain `check point`, `gaia`.
- Rule 10's switch regex is unchanged (Aruba CX `Swch` in sysDescr is covered by enterprise 47196's default
  class `switch`).
- **Rule 10 exception for enterprise 14823 (Aruba Wi-Fi)**, evaluated in rule 10 **before** the switch regex
  (the regex's `arubaos` would otherwise turn every Instant AP / controller into a switch): sysDescr matches
  `(?i)\(MODEL:\s*S\d{4}|\bswitch\b` (Mobility Access Switch S1500/S2500/S3500) → `switch` / `aruba`; else →
  `access_point` / `aruba` (Instant / AOS10 APs `MODEL: 515`, `IAP-…`; Mobility Controllers `MODEL:
  Aruba7010…`, no controller class). Rule number 10, confidence medium. Unit vectors: `ArubaOS (MODEL: 515),
  Version 8.10.0.6` → access_point; `ArubaOS (MODEL: Aruba7010-US), Version 8.7.1.0` → access_point;
  `ArubaOS (MODEL: S2500-24P), Version 7.4` → switch. Class detection (§ Devices: inventory flags) uses the
  same rule.

## [2b-2] New vendor templates

All ship `verified: false`. Priority **50** (like the HPE templates; the enterprise/sysObjectID rules do not
overlap, and 50 beats `nas_server` 40 and `generic_snmp` 20). Every vendor row is `probe: true`, a `when`
condition, or an `snmp_table` (no rows → `no_rows`), so missing objects never create sensors. Confidence as in
2b-1: **high** = matches the published MIB as far as the architect knows; **medium** = believed correct, verify;
**low** = unsure (expect corrections). Thresholds are generic defaults.

Shared value maps (YAML anchors inside each file):

- `CONDITION_MAP` (CPQ* condition): `1 warning other`, `2 up ok`, `3 warning degraded`, `4 down failed`; default `warning`.
- `DELL_STATUS_MAP` (ObjectStatusEnum): `1 warning other`, `2 warning unknown`, `3 up ok`, `4 warning nonCritical`, `5 down critical`, `6 down nonRecoverable`; default `warning`.
- `CISCO_ENVMON_MAP` (CiscoEnvMonState): `1 up normal`, `2 warning warning`, `3 down critical`, `4 down shutdown`, `6 down notFunctioning`; default `warning` (`5 notPresent` is filtered).
- `ENTITY_SENSOR_MAP` (EntitySensorStatus): `1 up ok`, `2 warning unavailable`, `3 down nonoperational`; default `warning`.

`IFX` below = `snmp_interfaces` with `oper_up_only: true`, `exclude_if_types: [1, 24, 53, 131, 136]`,
IF_EXCLUDE; `max` as stated.

### `aruba_cx` — Aruba CX switch (`aruba/aruba_cx.yaml`)
Match: `requires_snmp`, `enterprises [47196]`. Name en "Aruba CX switch" / he "מתג Aruba CX".
**ARUBAWIRED-\* MIBs are not used**: the architect is not confident about their column layout and value types;
the template uses standard MIBs only and is completed after the owner's snmpwalk (§ 11.8).

| # | Entry | Details | Confidence |
|---|---|---|---|
| 1–2 | Ping, System | | |
| 3 | CPU Load (`snmp_cpu`, probe) | HOST-RESOURCES / UCD (generic) | **low** (CX support unknown) |
| 4 | Memory (`snmp_memory`, probe) | generic | **low** |
| 5 | `Temperature {label}` (`snmp_table`) | walk entPhySensorValue `1.3.6.1.2.1.99.1.1.1.4`, label entPhysicalName `1.3.6.1.2.1.47.1.1.1.1.7`, `filters [{oid: 1.3.6.1.2.1.99.1.1.1.1, in: [8]}]` (type celsius), gauge `°C`, 65/80, max 16 | MIB high; CX support **medium**; scale/precision (`.2`/`.3`) ignored (low) |
| 6 | `{label} status` (`snmp_table`) | walk entPhySensorOperStatus `1.3.6.1.2.1.99.1.1.1.5`, label entPhysicalName, enum `ENTITY_SENSOR_MAP`, max 32, interval 300 | MIB high; CX support **medium** |
| 7–9 | PoE usage / power / budget | identical to `hpe_arubaos_switch` rows 6–8 | high |
| 10 | IFX max 52 | | high |

### `aruba_wifi` — Aruba Instant / Mobility Controller (`aruba/aruba_wifi.yaml`)
Match: `requires_snmp`, `enterprises [14823]`. Name "Aruba Wi-Fi (Instant / controller)" / "Aruba Wi-Fi
(Instant / בקר)". One template for both product lines: every row is probed, so the MIB that answers wins
(Instant virtual controller: AI-AP-MIB `1.3.6.1.4.1.14823.2.3.3.1`; Mobility Controller: WLSX-WLAN-MIB /
WLSX-SWITCH-MIB `1.3.6.1.4.1.14823.2.2.1`). Poll the **Instant virtual controller IP** (it answers for the
cluster).

| # | Entry | Details | Confidence |
|---|---|---|---|
| 1–2 | Ping, System | | |
| 3 | CPU Load, Memory (generic, probe) | | low |
| 4 | `APs up` (`snmp_walk_count`, probe) | Instant aiAPStatus `1.3.6.1.4.1.14823.2.3.3.1.2.1.1.11`, `count_values [1]` (up(1), down(2)), label `APs up`, channel `unmatched` warning_max 0 | MIB root medium; column **low** |
| 5 | `AP {label}` (`snmp_table`) | walk aiAPStatus `…2.1.1.11`, label aiAPName `…2.1.1.2`, enum `{1 up "up", 2 down "down"}` default warning, max 64, interval 300 | **low** |
| 6 | `Wi-Fi clients` (`snmp_walk_count`, probe) | Instant aiClientIPAddress `1.3.6.1.4.1.14823.2.3.3.1.2.4.1.3`, `count_values` null (counts clients; a non-index column, since the MAC index column `…4.1.1` may be not-accessible) | **low** |
| 7 | `APs up` (`snmp_walk_count`, probe) | controller wlanAPStatus `1.3.6.1.4.1.14823.2.2.1.5.2.1.4.1.19`, `count_values [1]`, `unmatched` warning_max 0 | **low** |
| 8 | `AP {label}` (`snmp_table`) | walk wlanAPStatus `…5.2.1.4.1.19`, label wlanAPName `…5.2.1.4.1.3`, same enum, max 64, interval 300 | **low** |
| 9 | `Wi-Fi clients` (`snmp_custom` gauge, probe) | wlsxSwitchTotalNumStationsAssociated `1.3.6.1.4.1.14823.2.2.1.1.3.2.0`, unit `clients` | **low** |
| 10 | IFX max 8 | | high |

Rows 4 and 7 have different OIDs, so both are idempotent separately; on a given device only one family answers.
**Clients per AP is not included** (the architect is not confident about the AI-AP-MIB radio table columns);
it is added after the owner's snmpwalk.

### `checkpoint_gaia` — Check Point Gaia firewall (`checkpoint/checkpoint_gaia.yaml`)
Match: `requires_snmp`, `vendors [checkpoint]`; and `requires_snmp`, `enterprises [2620]`. Name "Check Point
Gaia firewall" / "חומת אש Check Point Gaia". CHECKPOINT-MIB root `1.3.6.1.4.1.2620`.

| # | Entry | Details | Confidence |
|---|---|---|---|
| 1–2 | Ping, System | | |
| 3 | `CPU` (`snmp_custom` gauge, probe) | procUsage `1.3.6.1.4.1.2620.1.6.7.2.4.0`, `%`, 80/95 | medium-high → `medium` |
| 4 | `Memory` (`snmp_custom` gauge, probe) | memActiveReal64 `1.3.6.1.4.1.2620.1.6.7.4.4.0`, `percent_of_oid` memTotalReal64 `…7.4.3.0`, `%`, 85/95 | medium |
| 5 | `Concurrent connections` (gauge, probe) | fwNumConn `1.3.6.1.4.1.2620.1.1.25.3.0`, unit `conn` | **high** |
| 6 | `Connection table usage` (gauge, probe) | fwNumConn with `percent_of_oid` fwConnTableLimit `1.3.6.1.4.1.2620.1.1.25.10.0`, `%`, 80/95. Limit 0 ("automatic") → no value (ok) | medium |
| 7 | `ClusterXL state` (`snmp_custom` string, no `expected`) | haState `1.3.6.1.4.1.2620.1.5.6.0` (`active`, `standby`, …), label `ClusterXL`; `when [{oid: 1.3.6.1.4.1.2620.1.5.2.0, in: [1]}]` (haInstalled) | haState **high**; haInstalled = 1 on members **medium** |
| 8 | `ClusterXL status` (`snmp_custom` enum) | haStatCode `1.3.6.1.4.1.2620.1.5.101.0`, map `{0 up OK, 1 warning warning, 2 down problem}`, default `down`; same `when`; interval 60 | **low-medium** → `low` |
| 9 | `VPN tunnels up` (`snmp_walk_count`, probe) | tunnelState `1.3.6.1.4.1.2620.500.9002.1.3`, `count_values [3]` (active(3); destroy(4), idle(129), phase1(130), down(131), init(132)), `unmatched` warning_max 0. Lists **permanent tunnels** only | **low** |
| 10 | `Temperature {label}` (`snmp_table`) | walk tempertureSensorValue `1.3.6.1.4.1.2620.1.6.7.8.1.1.3` (DisplayString, parsed as number), label `…8.1.1.2`, gauge `°C`, 70/85, max 16 | medium (appliances only; VMs → `no_rows`) |
| 11 | `{label}` fans (`snmp_table`) | walk fanSpeedSensorStatus `1.3.6.1.4.1.2620.1.6.7.8.2.1.6`, label `…8.2.1.2`, enum `{0 up normal, 1 down abnormal, 2 warning unknown}`, default warning, max 16, interval 300 | **low-medium** → `low` |
| 12 | `Power supply {label}` (`snmp_table`) | walk powerSupplyStatus `1.3.6.1.4.1.2620.1.6.7.9.1.1.2` (DisplayString), label powerSupplyIndex `…9.1.1.1`, `value_type: string`, `expected: "Up"`, `match: equals`, max 8, interval 300 | medium |
| 13 | Disk `/` (`snmp_disk`, probe) | HOST-RESOURCES | high |
| 14 | Disk `/var/log` (`snmp_disk`, probe) | Gaia log partition | high |
| 15 | IFX max 32 | | high |

### `fortinet_fortigate` (`fortinet/fortinet_fortigate.yaml`)
Match: `requires_snmp`, `enterprises [12356]`. FORTINET-FORTIGATE-MIB `1.3.6.1.4.1.12356.101`.

| # | Entry | Details | Confidence |
|---|---|---|---|
| 1–2 | Ping, System | | |
| 3 | `CPU` (gauge, probe) | fgSysCpuUsage `1.3.6.1.4.1.12356.101.4.1.3.0`, `%`, 80/95 | high |
| 4 | `Memory` (gauge, probe) | fgSysMemUsage `…101.4.1.4.0`, `%`, 85/95 | high |
| 5 | `Sessions` (gauge, probe) | fgSysSesCount `…101.4.1.8.0`, unit `sessions` | high |
| 6 | `Disk` (gauge, probe) | fgSysDiskUsage `…101.4.1.6.0` `percent_of_oid` fgSysDiskCapacity `…101.4.1.7.0`, `%`, 85/95 (capacity 0 → no value) | high |
| 7 | `HA mode` (enum, probe) | fgHaSystemMode `1.3.6.1.4.1.12356.101.13.1.1.0`, `{1 up standalone, 2 up active-active, 3 up active-passive}` | high |
| 8 | `HA sync {label}` (`snmp_table`) | walk fgHaStatsSyncStatus `1.3.6.1.4.1.12356.101.13.2.1.1.12`, label fgHaStatsHostname `…13.2.1.1.11`, enum `{0 warning unsynchronized, 1 up synchronized}`, max 8 | medium |
| 9 | `VPN tunnels up` (`snmp_walk_count`, probe) | fgVpnTunEntStatus `1.3.6.1.4.1.12356.101.12.2.2.1.20`, `count_values [2]` (down(1), up(2)), `unmatched` warning_max 0 | medium |
| 10 | IFX max 32 | | high |

### `cisco_ios` — Cisco IOS / IOS-XE (`cisco/cisco_ios.yaml`)
Match: `requires_snmp`, `enterprises [9]`.

| # | Entry | Details | Confidence |
|---|---|---|---|
| 1–2 | Ping, System | | |
| 3 | `CPU {index}` (`snmp_table`) | walk cpmCPUTotal5minRev `1.3.6.1.4.1.9.9.109.1.1.1.1.8`, `%`, 80/95, max 8 | high |
| 4 | `Memory {label}` (`snmp_table`) | walk ciscoMemoryPoolUsed `1.3.6.1.4.1.9.9.48.1.1.1.5`, `percent_of_column` ciscoMemoryPoolFree `…48.1.1.1.6`, config `percent_base: sum`, label ciscoMemoryPoolName `…48.1.1.1.2`, `%`, 85/95, max 4 | high (IOS); IOS-XE pools medium |
| 5 | `Temperature {label}` (`snmp_table`) | walk ciscoEnvMonTemperatureStatusValue `1.3.6.1.4.1.9.9.13.1.3.1.3`, label `…13.1.3.1.2`, `skip_zero`, `°C`, 65/80, max 16 | high |
| 6 | `{label}` temperature state | walk ciscoEnvMonTemperatureState `…13.1.3.1.6`, label `…3.1.2`, enum `CISCO_ENVMON_MAP`, filter `not_in [5]`, max 16, interval 300 | high |
| 7 | `{label}` fan | walk ciscoEnvMonFanState `…13.1.4.1.3`, label `…4.1.2`, same map/filter, max 16, interval 300 | high |
| 8 | `{label}` power supply | walk ciscoEnvMonSupplyState `…13.1.5.1.3`, label `…5.1.2`, same map/filter, max 8, interval 300 | high |
| 9 | IFX max 52 | | high |

### `paloalto_panos` (`paloalto/paloalto_panos.yaml`)
Match: `requires_snmp`, `enterprises [25461]`.

| # | Entry | Details | Confidence |
|---|---|---|---|
| 1–2 | Ping, System | | |
| 3 | `CPU {index}` (`snmp_table`) | walk hrProcessorLoad `1.3.6.1.2.1.25.3.3.1.2`, `%`, 80/95, max 4 (management/data plane rows) | MIB high; row meaning medium |
| 4 | `Session utilization` (gauge, probe) | panSessionUtilization `1.3.6.1.4.1.25461.2.1.2.3.1.0`, `%`, 80/95 | high |
| 5 | `Active sessions` (gauge, probe) | panSessionActive `…25461.2.1.2.3.3.0`, unit `sessions` | high |
| 6 | `HA state` (string, probe) | panSysHAState `…25461.2.1.2.1.11.0`, label `HA` | medium |
| 7 | `HA peer state` (string, probe) | panSysHAPeerState `…25461.2.1.2.1.12.0`, label `HA peer` | medium |
| 8 | IFX max 32 | | high |

### `mikrotik_routeros` (`mikrotik/mikrotik_routeros.yaml`)
Match: `requires_snmp`, `enterprises [14988]`. MIKROTIK-MIB `1.3.6.1.4.1.14988.1.1.3` (health).

| # | Entry | Details | Confidence |
|---|---|---|---|
| 1–4 | Ping, System, CPU Load (probe), Memory (probe) | HOST-RESOURCES | high |
| 5 | `Temperature` (gauge, probe) | mtxrHlTemperature `1.3.6.1.4.1.14988.1.1.3.10.0`, multiplier 0.1, `°C`, 60/75 | medium (absent on many RouterOS 7 models) |
| 6 | `CPU temperature` (gauge, probe) | mtxrHlProcessorTemperature `…1.1.3.11.0`, ×0.1, `°C`, 70/85 | medium |
| 7 | `Voltage` (gauge, probe) | mtxrHlVoltage `…1.1.3.8.0`, ×0.1, unit `V` | medium |
| 8 | `{label}` temperature (`snmp_table`) | walk mtxrGaugeValue `1.3.6.1.4.1.14988.1.1.3.100.1.3`, label mtxrGaugeName `…100.1.2`, filter mtxrGaugeUnit `…100.1.4` `in [1]` (celsius), `°C`, 70/85, max 8 | medium (RouterOS ≥ 6.47 / 7) |
| 9 | `{label}` voltage | same walk, filter `in [3]` (dV), ×0.1, `V`, max 8 | medium |
| 10 | `{label}` fan | same walk, filter `in [2]` (rpm), unit `rpm`, max 8 | medium |
| 11 | IFX max 32 | | high |

Rows 5–7 and 8–10 may both exist on some models (different OIDs → duplicates of the same reading); accepted for
an unverified template.

### `apc_ups` — APC UPS (`apc/apc_ups.yaml`)
Match: `requires_snmp`, `enterprises [318]`. PowerNet-MIB `1.3.6.1.4.1.318.1.1.1`. No interfaces.

| # | Entry | Details | Confidence |
|---|---|---|---|
| 1–2 | Ping, System | | |
| 3 | `Battery capacity` (gauge, probe) | upsAdvBatteryCapacity `1.3.6.1.4.1.318.1.1.1.2.2.1.0`, `%`, **warning_min 50 / down_min 20** | high |
| 4 | `Runtime remaining` (gauge, probe) | upsAdvBatteryRunTimeRemaining `…1.1.1.2.2.3.0` (TimeTicks), multiplier `0.0001666667` (→ minutes), unit `min`, **warning_min 15 / down_min 5** | high |
| 5 | `Output load` (gauge, probe) | upsAdvOutputLoad `…1.1.1.4.2.3.0`, `%`, 80/95 | high |
| 6 | `Output status` (enum, probe, interval 30) | upsBasicOutputStatus `…1.1.1.4.1.1.0`: `1 warning unknown`, `2 up on line`, **`3 down on battery`**, `4 warning smart boost`, `5 warning timed sleeping`, `6 warning software bypass`, `7 down off`, `8 warning rebooting`, `9 warning switched bypass`, `10 down hardware failure bypass`, `11 down sleeping until power return`, `12 warning smart trim`, `13 up eco mode`, `14 warning hot standby`, `15 warning battery test`; default warning | high (1–12), medium (13–15) |
| 7 | `Battery status` (enum, probe) | upsBasicBatteryStatus `…1.1.1.2.1.1.0`: `1 warning unknown`, `2 up normal`, `3 down low`, `4 down fault`; default warning | high |
| 8 | `Battery replacement` (enum, probe, interval 3600) | upsAdvBatteryReplaceIndicator `…1.1.1.2.2.4.0`: `1 up ok`, `2 warning replace battery`; default warning | high |
| 9 | `Battery temperature` (gauge, probe) | upsAdvBatteryTemperature `…1.1.1.2.2.2.0`, `°C`, 40/50 | high |

"Alerting defaults" = these thresholds/maps: on battery → sensor `down` → the global alert rules notify.

### `hpe_ilo` — HPE iLO / ProLiant (`hpe/hpe_ilo.yaml`)
Match: `requires_snmp`, `enterprises [232]`; and `requires_snmp`, `sys_descr_regex: 'Integrated Lights-Out|\biLO ?\d'`.

| # | Entry | Details | Confidence |
|---|---|---|---|
| 1–2 | Ping, System | | |
| 3 | `Server health` (enum, probe) | cpqHeMibCondition `1.3.6.1.4.1.232.6.1.3.0`, `CONDITION_MAP` | high |
| 4 | `Power supply {index}` (`snmp_table`) | walk cpqHeFltTolPowerSupplyCondition `1.3.6.1.4.1.232.6.2.9.3.1.4`, filter cpqHeFltTolPowerSupplyPresent `…9.3.1.3` `in [3]`, `CONDITION_MAP`, max 8, interval 300 | medium |
| 5 | `Fan {index}` (`snmp_table`) | walk cpqHeFltTolFanCondition `1.3.6.1.4.1.232.6.2.6.7.1.9`, filter cpqHeFltTolFanPresent `…6.7.1.4` `in [3]`, `CONDITION_MAP`, max 16, interval 300 | medium |
| 6 | `Temperature {index}` (`snmp_table`) | walk cpqHeTemperatureCelsius `1.3.6.1.4.1.232.6.2.6.8.1.4`, `skip_zero`, gauge `°C`, no thresholds (server-specific), max 32 | medium |
| 7 | `Disk {index}` (`snmp_table`) | walk cpqDaPhyDrvCondition `1.3.6.1.4.1.232.3.2.5.1.1.37`, `CONDITION_MAP`, max 32, interval 300 | medium (Smart Array) |
| 8 | Web `https://{host}/` (`http`, `verify_tls: false`, probe) | | |

### `dell_idrac` — Dell iDRAC (`dell/dell_idrac.yaml`)
Match: `requires_snmp`, `sys_object_id_prefixes ['1.3.6.1.4.1.674.10892.5']`; and `requires_snmp`,
`enterprises [674]`, `sys_descr_regex: 'idrac'`. IDRAC-MIB-SMIv2 `1.3.6.1.4.1.674.10892.5`.

| # | Entry | Details | Confidence |
|---|---|---|---|
| 1–2 | Ping, System | | |
| 3 | `System status` (enum, probe) | globalSystemStatus `1.3.6.1.4.1.674.10892.5.2.1.0`, `DELL_STATUS_MAP` | high |
| 4 | `{label}` power supply | walk powerSupplyStatus `1.3.6.1.4.1.674.10892.5.4.600.12.1.5`, label powerSupplyLocationName `…600.12.1.8`, `DELL_STATUS_MAP`, max 8, interval 300 | medium |
| 5 | `{label}` fan | walk coolingDeviceStatus `…5.4.700.12.1.5`, label coolingDeviceLocationName `…700.12.1.8`, `DELL_STATUS_MAP`, max 16, interval 300 | medium |
| 6 | `{label}` (temperature) | walk temperatureProbeReading `…5.4.700.20.1.6`, ×0.1, label temperatureProbeLocationName `…700.20.1.8`, `skip_zero`, `°C`, no thresholds, max 16 | medium |
| 7 | `Disk {label}` | walk physicalDiskComponentStatus `1.3.6.1.4.1.674.10892.5.5.1.20.130.4.1.24`, label physicalDiskName `…130.4.1.2`, `DELL_STATUS_MAP`, max 32, interval 300 | medium |
| 8 | Web `https://{host}/` (`http`, `verify_tls: false`, probe) | | |

Final library order (priority desc, id asc): `opnsense` (90), `unifi_ap` (80), `printer` (60), `apc_ups`,
`aruba_cx`, `aruba_wifi`, `checkpoint_gaia`, `cisco_ios`, `dell_idrac`, `fortinet_fortigate`,
`hpe_arubaos_switch`, `hpe_comware`, `hpe_ilo`, `mikrotik_routeros`, `paloalto_panos` (50), `nas_server` (40),
`generic_snmp` (20), `http_server` (10), `generic_ping` (0).

## [2b-2] Phase 2b-2 migration (`0005`)
File `backend/netmon/migrations/versions/0005_phase2b2.py`, `revision = "0005"`, `down_revision = "0004"`.
Plain PostgreSQL DDL only (no Timescale calls; none of the new tables is a hypertable).

`upgrade()`:
1. `ALTER TABLE sensors DROP CONSTRAINT IF EXISTS sensors_type_check`, re-add with the 10 types of 0003 +
   `'snmp_walk_count'`.
2. `ALTER TABLE devices ADD COLUMN port_inventory text NOT NULL DEFAULT 'auto' CONSTRAINT
   devices_port_inventory_check CHECK (port_inventory IN ('auto','on','off'))`; same for `arp_collect`
   (`devices_arp_collect_check`).
3. `CREATE TABLE switch_ports (…)` with `PRIMARY KEY (device_id, if_index)` and the checks
   `switch_ports_kind_check`, `switch_ports_duplex_check`, `switch_ports_mode_check`,
   `switch_ports_poe_detection_check`, `switch_ports_counter_bits_check` (`IN (32,64)`),
   `switch_ports_poe_class_check` (`BETWEEN 0 AND 4`).
4. `CREATE TABLE port_macs (…)`; `CREATE UNIQUE INDEX ux_port_macs_key ON port_macs (device_id, mac,
   (COALESCE(vlan, -1)))`; `CREATE INDEX ix_port_macs_mac ON port_macs (mac)`; `CREATE INDEX
   ix_port_macs_mac_prefix ON port_macs (mac varchar_pattern_ops)` (prefix search, any collation); `CREATE INDEX
   ix_port_macs_device_if ON port_macs (device_id, if_index) WHERE present`.
5. `CREATE TABLE arp_entries (…, PRIMARY KEY (source_device_id, ip))`; `ix_arp_entries_mac`, `ix_arp_entries_ip`.
6. `CREATE TABLE dns_cache (…)`.
7. `CREATE TABLE port_inventory_status (…)` with `port_inventory_status_detected_class_check` (the 8
   device classes).

`downgrade()` (reverse order): drop `port_inventory_status`, `dns_cache`, `arp_entries`, `port_macs`,
`switch_ports`; `ALTER TABLE devices DROP COLUMN arp_collect, DROP COLUMN port_inventory`;
`DELETE FROM sensors WHERE type = 'snmp_walk_count'` (channels/measurements cascade or are removed with them as
in 0003's downgrade); restore `sensors_type_check` with the 10 types. Settings rows
(`port_inventory_min`, `uplink_mac_threshold`, `port_mac_retention_days`, `reverse_dns`) are harmless and stay.
`snmp_custom` sensors with `config.percent_base` keep the key (ignored by 2b-1 code).

# **[2b-3]** Phase 2b-3 additions

Scope: feature toggles, SNMP trap and syslog reception (storage, parsing, attribution), event rules and
passive event sensors, port flapping, SIEM forwarding, LAG membership, Active Directory login, audit log,
device timeline, Microsoft Teams channel, API tokens. All schema changes are in **migration `0006`**
([§ Phase 2b-3 migration](#2b-3-phase-2b-3-migration-0006)). Everything is additive; `users.password_hash`
becomes nullable (LDAP users).

API shapes: [api.md § Phase 2b-3](api.md#2b-3-phase-2b-3-contract). Components, pipeline, flows:
[architecture.md § 12](architecture.md#12-phase-2b-3-traps-syslog-event-alerts-lag-active-directory-audit-timeline-teams-flapping-siem-forwarding-api-tokens).

## [2b-3] Enum changes

| Name | Values |
|---|---|
| `sensor_type` | … 2b-2 values, **[2b-3]** `trap`, `syslog`, `port_flap` (passive event sensors) |
| `notification_type` | `email`, `webhook`, `telegram`, **[2b-3]** `teams` |
| `event_kind` | … 2b-1 values, **[2b-3]** `notification_sent`, `port_change` |
| **[2b-3]** `user_source` | `local`, `ldap` |
| **[2b-3]** `event_rule_kind` | `trap`, `syslog` |
| **[2b-3]** `event_rule_action` | `warning`, `down`, `ignore` |
| **[2b-3]** `rule_scope` | `global`, `group`, `device` |
| **[2b-3]** `condition_source` | `rule`, `flap` |
| **[2b-3]** `trap_version` | `v1`, `v2c`, `v3` |
| **[2b-3]** `trap_pdu` | `trap`, `inform` |
| **[2b-3]** `syslog_format` | `rfc5424`, `rfc3164`, `raw` |
| **[2b-3]** `transport` | `udp`, `tcp` |
| **[2b-3]** `actor_type` | `user`, `token`, `system`, `anonymous` |
| **[2b-3]** `feature` | `traps`, `syslog`, `event_alerts`, `lag`, `ldap`, `audit`, `timeline`, `flapping`, `siem_forwarding` |

Syslog severity 0–7 (emerg, alert, crit, err, warning, notice, info, debug); facility 0–23 (RFC 5424 table).

## [2b-3] Feature toggles

Settings keys `feature_<name>_enabled` (bool), **default `false`** for every feature, also on upgraded
installs (no migration writes them). Turning one ON/OFF takes effect at runtime (architecture § 12.0).

| Key | ON | OFF means exactly |
|---|---|---|
| `feature_traps_enabled` | trap UDP socket open, decoding, storage | socket closed; `/api/traps*` 409; no trap rows written; v3 engine dropped |
| `feature_syslog_enabled` | syslog UDP+TCP sockets, storage | sockets and TCP connections closed; `/api/syslog*` 409 |
| (traps or syslog) | | `/api/ingest/*` and `/api/event-rules*` answer 409 only when **both** are OFF |
| `feature_event_alerts_enabled` | rules evaluated, `trap`/`syslog` event sensors created/updated, expiry loop | no evaluation; all `rule` conditions deleted, `trap`/`syslog` sensors set `up` ("Event alerts are turned off"); rule CRUD stays available (prepare before enabling); `/clear` and `/conditions` still work |
| `feature_flapping_enabled` | transition tracking, `port_flap` sensors, suppression | no tracking; `flap` conditions deleted (sensors `up`), suppression set emptied with re-arm |
| `feature_siem_forwarding_enabled` | forwarder tasks per enabled target | tasks stopped, queues discarded; `POST /api/ingest/forwarders/test` 409; targets stay in settings |
| `feature_lag_enabled` | walks #13/#14, LAG fields in ports/search | walks not sent; `lag`, `lag_members`, `vlans_from_lag`, `via_lag` returned as null/[]/false; stored columns untouched |
| `feature_ldap_enabled` | AD login | login is local-only (LDAP users get 401 `invalid_credentials`, audit reason `ldap_disabled`); `POST /api/ldap/test*` **still allowed** |
| `feature_audit_enabled` | `audit_log` written | nothing written; `/api/audit*` 409; retention still deletes old rows |
| `feature_timeline_enabled` | timeline endpoint; `port_change` events written | endpoint 409; no `port_change` events (the other event kinds, incl. `notification_sent`, are always written) |

Not toggles (owner decision): the Microsoft Teams channel type and API tokens are **always available**.
Rows `feature_teams_enabled` / `feature_api_tokens_enabled` left by earlier builds are ignored when reading
settings (no migration removes them); `PUT /api/settings` accepts and ignores those keys like any unknown key
(they are not stored and not returned).

## [2b-3] `traps` (a hypertable when Timescale is available)

| Column | Type | Constraints / meaning |
|---|---|---|
| id | bigint | GENERATED ALWAYS AS IDENTITY. PK `(id, time)` (a hypertable needs the time column in unique keys) |
| time | timestamptz | NOT NULL — receive time |
| source_ip | inet | NOT NULL — UDP peer (IPv4-mapped IPv6 normalised) |
| agent_addr | inet | NULL — v1 agent-addr |
| device_id | integer | NULL — attributed device. **No FK** (like `measurements`); device delete → background `UPDATE … SET device_id = NULL` |
| version | text | NOT NULL CHECK in trap_version |
| pdu | text | NOT NULL CHECK in trap_pdu |
| security_name | varchar(64) | NULL — v3 user name (the community is never stored) |
| trap_oid | varchar(255) | NOT NULL — snmpTrapOID (v1 converted, RFC 3584) |
| trap_name | varchar(128) | NULL — OID name (below) |
| uptime_ticks | bigint | NULL |
| varbinds | jsonb | NOT NULL DEFAULT `'[]'` — `[{"oid","name","type","value","display"}]`, ≤ 128, without sysUpTime.0 / snmpTrapOID.0 |
| summary | text | NOT NULL (≤ 1000 chars, architecture § 12.2.3) |
| matched_rule_ids | integer[] | NOT NULL DEFAULT '{}' — rules that cleared or raised a condition (or `ignore`d) for this record |

Indexes: `ix_traps_time (time DESC)`, `ix_traps_device_time (device_id, time DESC)`, `ix_traps_unknown
(source_ip, time DESC) WHERE device_id IS NULL`, `ix_traps_oid (trap_oid varchar_pattern_ops)`; trigram
`ix_traps_summary_trgm USING gin (summary gin_trgm_ops)` (bootstrap, see Text search). `type` in varbinds is the
SMI name: `Integer`, `OctetString`, `ObjectIdentifier`, `IpAddress`, `Counter32`, `Gauge32`, `TimeTicks`,
`Counter64`, `Opaque`, `Null`, `noSuchObject`, `noSuchInstance`, `endOfMibView`.

## [2b-3] `syslog_messages` (a hypertable when Timescale is available)

| Column | Type | Constraints / meaning |
|---|---|---|
| id | bigint | identity; PK `(id, time)` |
| time | timestamptz | NOT NULL — receive time |
| device_time | timestamptz | NULL — parsed device timestamp |
| source_ip | inet | NOT NULL |
| device_id | integer | NULL, no FK (as traps) |
| transport | text | NOT NULL CHECK in transport |
| format | text | NOT NULL CHECK in syslog_format |
| facility | smallint | NOT NULL CHECK 0–23 |
| severity | smallint | NOT NULL CHECK 0–7 |
| hostname | varchar(255) | NOT NULL DEFAULT '' |
| program | varchar(128) | NOT NULL DEFAULT '' — APP-NAME / TAG / Comware module |
| pid | varchar(128) | NOT NULL DEFAULT '' — PROCID / `[pid]` |
| msgid | varchar(64) | NOT NULL DEFAULT '' — MSGID / Comware digest / ArubaOS event id |
| structured_data | text | NULL — RFC 5424 SD text as received |
| message | text | NOT NULL — ≤ 8192 bytes UTF-8 |
| truncated | boolean | NOT NULL DEFAULT false |
| matched_rule_ids | integer[] | NOT NULL DEFAULT '{}' |

Indexes: `ix_syslog_time (time DESC)`, `ix_syslog_device_time (device_id, time DESC)`, `ix_syslog_unknown
(source_ip, time DESC) WHERE device_id IS NULL`; trigram `ix_syslog_message_trgm USING gin (message
gin_trgm_ops)` (bootstrap).

### Timescale / fallback
`netmon.db.timescale.bootstrap()` additionally runs, in Timescale mode:
`create_hypertable('traps', 'time', chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE,
migrate_data => TRUE, create_default_indexes => FALSE)` and the same for `syslog_messages` with `INTERVAL '1
day'`; retention policies `trap_retention_days` / `syslog_retention_days` (re-applied on `PUT /api/settings`
change). No compression. Fallback (or policy API missing): the hourly loop deletes `time < now() − N days` in
batches of 50 000 (same SQL pattern as measurements). The `measurements`-style BRIN index on `time` is added in
fallback mode only.

### Text search: trigram, not tsvector
Log lines are full of tokens that a text-search parser breaks or stems (`GigabitEthernet1/0/5`,
`%%10IFNET/4/PHY_UPDOWN`, IPs, MACs, `sshd[2211]`); operators expect grep-like substring search. **`pg_trgm`**
GIN indexes serve `ILIKE '%text%'` for any substring ≥ 3 characters, language-independent, and need no extra
column. `pg_trgm` is a contrib extension shipped with PostgreSQL packages (Docker `postgres`/`timescaledb`
images, Debian/Ubuntu, the EDB Windows installer) and is **trusted** since PG 13 (the DB owner may create it).
Bootstrap (`netmon.db.extensions.ensure_trgm()` after migrations, in a savepoint): `CREATE EXTENSION IF NOT
EXISTS pg_trgm`, then `CREATE INDEX IF NOT EXISTS` the two trigram indexes. On failure: WARNING, flag
`rt.trgm_enabled = false` (reported by `/api/health`); text search still works (sequential scan bounded by the
time range). Queries: `message ILIKE '%' || :q || '%'` with `\`, `%`, `_` escaped in `:q`.

## [2b-3] Syslog parsing (pure `netmon.ingest.syslog_parse.parse_syslog`)

Input: raw bytes (one datagram or TCP frame), receive time, time zone. Pre-processing: strip trailing `\n`,
`\r`, `\0`; decode UTF-8 with `errors="replace"`; replace NUL with U+FFFD; cut the message part to 8192 bytes.

1. **PRI:** `^<(\d{1,3})>` with value 0–191 → facility = v // 8, severity = v % 8. Missing/invalid → facility
   1, severity 5, `format = raw`, `message` = the whole line, other fields empty. Stop.
2. **RFC 5424:** after PRI `1 ` → `TIMESTAMP SP HOSTNAME SP APP-NAME SP PROCID SP MSGID SP SD [SP MSG]`; `-` =
   empty/null; TIMESTAMP RFC 3339 → `device_time`; SD = `-` or one or more `[...]` elements (respecting `\]`
   escapes) → `structured_data`; MSG with a leading UTF-8 BOM stripped. Malformed header → fall through to 3.
3. **RFC 3164 (lenient):** optional timestamp `Mmm dd hh:mm:ss` (day may be space-padded; optional `.fff`;
   optional ` yyyy` after the time (Comware) or `yyyy ` before the time); then optional HOSTNAME = next token
   unless that token ends with `:` or matches the TAG pattern followed by `:`; then:
   - **Comware:** `^%%(\d{2})?([A-Za-z0-9_-]+)/(\d)/([A-Za-z0-9_]+)(\([a-z]\))?:\s?(.*)$` → `program` = group 2,
     `msgid` = group 4, `message` = group 6 (the in-text severity is informational; PRI stays authoritative);
   - **ArubaOS-Switch:** `^(\d{5}) ([^\s:\[]{1,48}):\s?(.*)$` → `msgid` = event id, `program`, `message`
     (medium confidence; verified on the owner's switches);
   - **TAG:** `^([^\s:\[]{1,48})(\[([^\]]{1,128})\])?:\s?(.*)$` → `program`, `pid`, `message`;
   - else `program` = '', `message` = rest.
   No year → current year in `tz`, minus one if that is > 1 day in the future. No timestamp → `device_time`
   null. `format = rfc3164`.

**Test vectors** (tz `Asia/Jerusalem`, received `2026-11-08T10:00:05Z`):

| # | Input | facility / severity | Expected fields |
|---|---|---|---|
| S1 | `<165>1 2026-11-08T10:00:00.003Z sw1.mnidom.net evntslog 1234 ID47 [exampleSDID@32473 iut="3"] \xEF\xBB\xBFAn application event` | 20 / 5 | rfc5424, device_time `2026-11-08T10:00:00.003Z`, hostname `sw1.mnidom.net`, program `evntslog`, pid `1234`, msgid `ID47`, sd `[exampleSDID@32473 iut="3"]`, message `An application event` |
| S2 | `<34>1 - - - - - - plain` | 4 / 2 | rfc5424, all empty, device_time null, sd null, message `plain` |
| S3 | `<34>Oct 11 22:14:15 mymachine su: 'su root' failed for lonvick on /dev/pts/8` | 4 / 2 | rfc3164, hostname `mymachine`, program `su`, message `'su root' failed for lonvick on /dev/pts/8`, device_time 2026-10-11T19:14:15Z (Oct 11 2026 is before the receive date, so the current year; IDT +3) |
| S4 | `<30>Nov  8 09:05:01 gw-hq sshd[2211]: Accepted password for admin` | 3 / 6 | hostname `gw-hq`, program `sshd`, pid `2211`, device_time `2026-11-08T07:05:01Z` (IST +2) |
| S5 | `<188>Nov  8 12:00:00 2026 FF5945-IRF %%10IFNET/4/PHY_UPDOWN: Physical state on the interface Ten-GigabitEthernet1/0/49 changed to down.` | 23 / 4 | hostname `FF5945-IRF`, program `IFNET`, msgid `PHY_UPDOWN`, message `Physical state on the interface Ten-GigabitEthernet1/0/49 changed to down.` |
| S6 | `<190>Nov  8 12:00:00 10.0.0.2 00076 ports: port 5 is now on-line` | 23 / 6 | hostname `10.0.0.2`, msgid `00076`, program `ports`, message `port 5 is now on-line` |
| S7 | `<13>Nov  8 12:00:00 su: test` | 1 / 5 | hostname '', program `su`, message `test` |
| S8 | `<13>hello world` | 1 / 5 | rfc3164, device_time null, hostname '', program '', message `hello world` |
| S9 | `just text` | 1 / 5 | raw, message `just text` |
| S10 | `<999>x` | 1 / 5 | raw, message `<999>x` |
| S11 | `<14>Nov  8 12:00:00 host app: a\x00b` + 9000 × `x` | 1 / 6 | message has U+FFFD for NUL, ≤ 8192 bytes, truncated true |

**TCP framing vectors** (one connection): `27 <13>1 - h a - - - octet one` then `<13>lf one\n` then `<13>lf
two\r\n` → three records (`octet one`, `lf one`, `lf two`); `99999999 x` → connection closed (`too_large`).

## [2b-3] OID name table (`netmon.ingest.oid_names`, longest prefix)

| OID | Name |
|---|---|
| `1.3.6.1.6.3.1.1.5.1` … `.6` | `coldStart`, `warmStart`, `linkDown`, `linkUp`, `authenticationFailure`, `egpNeighborLoss` |
| `1.3.6.1.2.1.1.3.0` / `1.3.6.1.6.3.1.1.4.1.0` / `1.3.6.1.6.3.1.1.4.3.0` | `sysUpTime.0` / `snmpTrapOID.0` / `snmpTrapEnterprise.0` |
| `1.3.6.1.6.3.18.1.3.0` / `1.3.6.1.6.3.18.1.4.0` | `snmpTrapAddress.0` / `snmpTrapCommunity.0` |
| `1.3.6.1.2.1.1.1.0` / `1.3.6.1.2.1.1.5.0` | `sysDescr.0` / `sysName.0` |
| `1.3.6.1.2.1.2.2.1.1` / `.2` / `.3` / `.7` / `.8` | `ifIndex` / `ifDescr` / `ifType` / `ifAdminStatus` / `ifOperStatus` (status display `up(1)`, `down(2)`, `testing(3)`, `unknown(4)`, `dormant(5)`, `notPresent(6)`, `lowerLayerDown(7)`) |
| `1.3.6.1.2.1.31.1.1.1.1` / `.18` | `ifName` / `ifAlias` |
| `1.3.6.1.2.1.17.0.1` / `.2` | `newRoot` / `topologyChange` |
| `1.3.6.1.2.1.47.2.0.1` | `entConfigChange` |
| `1.0.8802.1.1.2.0.0.1` | `lldpRemTablesChange` |
| `1.3.6.1.2.1.105.0.1` / `.2` / `.3` | `pethPsePortOnOffNotification` / `pethMainPowerUsageOnNotification` / `pethMainPowerUsageOffNotification` |
| `1.3.6.1.4.1.8072.4.0.1` / `.2` / `.3` | `nsNotifyStart` / `nsNotifyShutdown` / `nsNotifyRestart` |
| `1.3.6.1.4.1.11` / `.25506` / `.14823` / `.47196` / `.2620` / `.9` / `.8072` | vendor roots `hp` / `h3c` / `aruba` / `arubaCx` / `checkpoint` / `cisco` / `netSnmp` (remainder appended: `h3c.2.6.2.0.1`) |

## [2b-3] Attribution, sources

`devices` gains:

| Column | Type | Constraints |
|---|---|---|
| source_addresses | inet[] | NOT NULL DEFAULT '{}' — ≤ 16 additional sender IPs (no prefixes), unique per device |
| event_sensor_optout | text[] | NOT NULL DEFAULT '{}' — subset of {`trap`,`syslog`,`port_flap`}: event sensors deleted by a user and not to be re-created |

Rules: architecture § 12.2.5. The `SourceMap` is in memory; an IP in another device's `source_addresses`
is allowed (precedence decides) but the API returns a warning list in the device response
(`source_address_conflicts`).

## [2b-3] Event rules

### `event_rules`
| Column | Type | Constraints |
|---|---|---|
| id | integer | PK |
| name | varchar(128) | NOT NULL |
| kind | text | NOT NULL CHECK in event_rule_kind |
| enabled | boolean | NOT NULL DEFAULT true |
| builtin_key | varchar(64) | NULL, UNIQUE — set on seeded rules |
| priority | integer | NOT NULL DEFAULT 100 (0–100000; lower first; ties by id) |
| scope | text | NOT NULL DEFAULT `global` CHECK in rule_scope |
| group_id | integer | NULL FK `groups.id` ON DELETE CASCADE — iff scope group (subgroups included) |
| device_id | integer | NULL FK `devices.id` ON DELETE CASCADE — iff scope device |
| match | jsonb | NOT NULL — `TrapMatch` / `SyslogMatch` by kind |
| action | text | NOT NULL CHECK in event_rule_action |
| key_varbind_oid | varchar(255) | NULL — trap rules only |
| key_regex | varchar(200) | NULL — syslog rules only; exactly one capture group |
| clear_match | jsonb | NULL — same shape as `match`; only with action warning/down |
| clear_after_min | integer | NULL CHECK 1–10080; only with action warning/down |
| last_matched_at | timestamptz | NULL (updated at most once per minute) |
| created_at, updated_at | timestamptz | |

Table CHECK: scope/group/device consistency as `maintenance_windows`. API validation (422): action `ignore`
has no key/clear; warning/down rules need `clear_match` or `clear_after_min` (a condition must be able to end;
manual clear exists but is not enough); regexes compile (Python `re`, `IGNORECASE`), ≤ 200 chars; OIDs
numeric dotted.

```json
// TrapMatch
{ "trap_oids": ["1.3.6.1.6.3.1.1.5.3"],                 // prefixes of trap_oid; [] = any trap
  "varbinds": [ { "oid": "1.3.6.1.2.1.2.2.1.7", "op": "ne", "value": "2" } ],   // all must hold, ≤ 8
  "ports": "important" }                                 // "any" | "important"
// SyslogMatch
{ "severity_max": 3,                                     // 0–7 or null (any); matches severity <= value
  "facilities": [], "programs": [],                      // [] = any; programs case-insensitive exact, ≤ 16
  "text": null, "text_regex": false }                    // contains (case-insensitive) or regex
```
Varbind ops on the **first** varbind whose OID equals or starts with `oid.`: `eq` / `ne` (raw `value` string
equality), `contains` (case-insensitive), `regex` (search), `exists`, `absent`. A missing varbind: `ne` and
`absent` true, everything else false.

### Rule evaluation (pure `netmon.ingest.rules.evaluate(record, rules, ctx) -> Outcome`)
Inputs: the record (device attributed, event sensor not paused, device enabled), the enabled rules of that kind
in scope for the device sorted by `(priority, id)`, `ctx` (important-port predicate, flapping ports).
1. **Key** of a rule for the record: trap — `value` of the first varbind matching `key_varbind_oid` (absent →
   `""`); syslog — group 1 of `key_regex` searched in `message` (no match → `""`); no key field → `""`.
   Keys are cut to 128 chars.
2. **Clears:** every rule with a `clear_match` that matches → clear `(rule, key)`.
3. **Raise:** the first rule whose `match` matches (incl. the port filter): `ignore` → stop; `warning`/`down` →
   if `(device, ifIndex key)` is currently flapping and the rule's `key_varbind_oid` is the ifIndex column
   (`1.3.6.1.2.1.2.2.1.1`) → no condition (suppressed); else upsert condition `(rule, key)`.
4. `matched_rule_ids` = clearing rules + the raising/ignoring rule (in that order, distinct).

**Important port** (`ports: "important"`, the ifIndex is the value of varbind `1.3.6.1.2.1.2.2.1.1`): no such
varbind → important; else important when an `snmp_interface` sensor of the device has `config.if_index` =
ifIndex, or the `switch_ports` row has kind `lag` or `lag_if_index` not null, or the port is an uplink per
data-model § Uplinks; a device with no `switch_ports` rows and no `snmp_interface` sensors → important.
**[1.3]** replaced by [§ 1.3 Important ports](#13-important-ports): the shared definition (uplink, LAG / member,
trunk while `event_linkdown_include_trunks`) plus ports whose `snmp_interface` sensor has impact **`critical`**;
a device with neither `switch_ports` rows nor critical interface sensors → every port important.

### Built-in rules (seeded by `0006`, **enabled = false**)
| builtin_key | name (en / he) | kind | priority | match | action | key | clear |
|---|---|---|---|---|---|---|---|
| `trap_link_down` | Link down / קישור נפל | trap | 1000 | `{"trap_oids":["1.3.6.1.6.3.1.1.5.3"],"varbinds":[{"oid":"1.3.6.1.2.1.2.2.1.7","op":"ne","value":"2"}],"ports":"important"}` | warning | `key_varbind_oid` `1.3.6.1.2.1.2.2.1.1` | `clear_match {"trap_oids":["1.3.6.1.6.3.1.1.5.4"],"varbinds":[],"ports":"any"}`, `clear_after_min` 1440 |
| `trap_auth_failure` | SNMP authentication failure / כשל אימות SNMP | trap | 1010 | `{"trap_oids":["1.3.6.1.6.3.1.1.5.5"],"varbinds":[],"ports":"any"}` | warning | — | 60 |
| `trap_restart` | Device restarted / ההתקן הופעל מחדש | trap | 1020 | `{"trap_oids":["1.3.6.1.6.3.1.1.5.1","1.3.6.1.6.3.1.1.5.2"],"varbinds":[],"ports":"any"}` | warning | — | 30 |
| `syslog_error` | Syslog error or worse / שגיאת Syslog ומעלה | syslog | 1030 | `{"severity_max":3,"facilities":[],"programs":[],"text":null,"text_regex":false}` | warning | — | 30 |

The name is stored in the default language at seed time; `POST /api/event-rules/{id}/reset` restores the row
above (name in the current `default_language`, `enabled` unchanged). The API returns `name` of a built-in
rule in the requesting user's language (he/en) while the stored name is one of the two default names.

### `event_conditions`
| Column | Type | Constraints |
|---|---|---|
| id | bigint | PK identity |
| sensor_id | integer | NOT NULL FK `sensors.id` ON DELETE CASCADE |
| source | text | NOT NULL CHECK in condition_source |
| rule_id | integer | NULL FK `event_rules.id` ON DELETE CASCADE — required iff source = rule |
| key | varchar(128) | NOT NULL DEFAULT '' (flap: the ifIndex as text) |
| status | text | NOT NULL CHECK in (`warning`,`down`) |
| first_at | timestamptz | NOT NULL |
| last_at | timestamptz | NOT NULL |
| count | integer | NOT NULL DEFAULT 1 (matches, or transitions for flap) |
| message | text | NOT NULL (≤ 500) |
| last_kind | text | NULL CHECK in (`trap`,`syslog`) |
| last_ref_id | bigint | NULL — id of the last trap/syslog row (no FK) |
| expires_at | timestamptz | NULL |

Unique index `ux_event_conditions_key (sensor_id, source, COALESCE(rule_id, 0), key)`; index `(expires_at)
WHERE expires_at IS NOT NULL`. At most 256 conditions per sensor (the oldest `last_at` is removed first).
Condition `expires_at` = `last_at + clear_after_min` (rule) or `last_at + flap_quiet_min` (flap).

### Event sensor types (`poller/sensor_types.py`)
`trap`, `syslog`, `port_flap`: `requires_snmp false`, **`passive true`** (new `SensorTypeDef` field, served by
`/api/sensor-types`), no config fields, one channel `active` (unit `#`, no thresholds, primary). The channel's
`last_value` = active conditions; its `status` = the sensor status. Default names: `SNMP traps`, `Syslog`,
`Port flapping`. Unique index `ux_sensors_event_type (device_id, type) WHERE type IN
('trap','syslog','port_flap')`. `alert_state` gains `flap_suppressed boolean NOT NULL DEFAULT false`
(architecture § 12.4).

## [2b-3] Port flapping
Settings `flap_threshold` (int, 3–50, default 5), `flap_window_min` (1–120, default 10), `flap_quiet_min`
(1–1440, default 15). Pure tracker vectors (threshold 5, window 10, quiet 15; times in minutes): transitions
at 0 d, 1 u, 2 d, 3 u → none; + 4 d → **start** (5 in 10 min); 4.5 d → duplicate (same direction < 60 s) ignored;
transitions until 9, then none → **end** at 24; 0 d, 3 u, 6 d, 9 u, 12 d → none (only 4 within any 10 min).

## [2b-3] SIEM forwarding target (settings `siem_targets`, list ≤ 4)
```json
{ "id": "a1b2c3", "name": "QRadar", "enabled": true, "host": "siem.mnidom.net", "port": 6514,
  "protocol": "tls", "tls_verify": true,
  "kinds": ["syslog", "trap"], "min_severity": 7, "sources": [], "include_unknown": false }
```
`id`: server-generated 6 hex chars (kept across edits; new entries without id get one). `protocol` `udp` |
`tcp` | `tls`; `port` 1–65535; `min_severity` 0–7 (forward records with severity ≤ value; traps: 5, or 4/2 when a
rule raised warning/down); `sources`: IPs/CIDRs (≤ 64; [] = all); `include_unknown`: forward unknown sources.
Rendering: architecture § 12.5. Example syslog S5 forwarded: `<188>1 2026-11-08T10:00:00Z FF5945-IRF IFNET -
PHY_UPDOWN [origin ip="10.0.0.12" software="NetworkMonitor" swVersion="1.1.0"] Physical state on the interface
Ten-GigabitEthernet1/0/49 changed to down.`; TCP/TLS frame = `<length> <message>`.

## [2b-3] LAG membership (`switch_ports` gains)

| Column | Type | Constraints |
|---|---|---|
| lag_if_index | integer | NULL — aggregator ifIndex of this physical port |
| lag_attached | boolean | NULL — true: attached (LAG-MIB); false: configured (ifStack) but LAG-MIB reports not attached; null: membership known from ifStack only |

`port_inventory_status.sources` gains key `lag` (`source_state`) and `lag_mib` (`"ieee8023"` \| `"if_stack"` \|
`"both"` \| null). Walks and `resolve_lag`: architecture § 12.6.

| # | Object | OID | Index | Confidence |
|---|---|---|---|---|
| 13 | dot3adAggPortAttachedAggID | `1.2.840.10006.300.43.1.2.1.1.13` | member ifIndex → aggregator ifIndex (0 = none) | high (MIB); vendor support medium (static ArubaOS trunks may be absent → #14) |
| 14 | ifStackStatus | `1.3.6.1.2.1.31.1.2.1.3` | higher.lower ifIndex, value 1 active | high |

**Pure vectors:** physical {1, 2, 3, 4}, lag {11}; #13 `{3: 11, 4: 11, 1: 0}`, #14 `{}` → 3 → (11, true), 4 →
(11, true); #13 `{3: 11, 4: 0}`, #14 `{(11,3): 1, (11,4): 1}` → 3 (11, true), 4 (11, false); #13 `{}`, #14
`{(11,4): 1, (0,11): 1, (4,0): 1}` → 4 (11, null); #13 `{3: 99}` (99 not a lag) → none; #14 `{(11,3): 2}` →
none; #14 `{(12,3):1, (11,3):1}` with lags {11, 12} → (11, null).
Inheritance vectors: member without bridge port, LAG untagged [1] tagged [10, 20] → member inherits
(`vlans_from_lag: true`); member with own untagged [5] → keeps [5]; member mac_count 0 + LAG 2 → 2; LAG uplink
by neighbour on member 3 → members 3 and 4 uplink.

## [2b-3] Users, Active Directory

`users` changes:

| Column | Type | Constraints |
|---|---|---|
| password_hash | text | now **NULL** allowed |
| source | text | NOT NULL DEFAULT `local` CHECK in user_source |
| external_id | varchar(64) | NULL — AD objectGUID (lower-case hex, 32 chars). Unique index WHERE NOT NULL |
| display_name | varchar(256) | NULL |
| ldap_dn | text | NULL |
| ldap_synced_at | timestamptz | NULL — last successful AD login |

CHECK `users_source_password_check`: `(source = 'local') = (password_hash IS NOT NULL)`. Username: local
users keep `^[A-Za-z0-9._-]{3,64}$`; LDAP users store lower(sAMAccountName) (1–64 chars, any printable except
`"/\[]:;|=,+*?<>@`), still unique on `lower(username)`. Migration `0006` replaces the 0001 inline check
`users_username_check` by `(source = 'local' AND <local pattern>) OR (source = 'ldap' AND <ldap rule>)`.

### LDAP settings (key `ldap`)
```json
{ "servers": ["dc1.mnidom.net", "dc2.mnidom.net:636"],   // ≤ 8; host | host:port | [v6]:port; [] = DNS SRV
  "security": "ldaps",                                    // "ldaps" (default port 636) | "starttls" (389)
  "tls_verify": true,
  "domain": "mnidom.net", "netbios_domain": "MNIDOM",
  "base_dn": "DC=mnidom,DC=net",                          // "" → derived from domain
  "bind_dn": "svc-netmon@mnidom.net",                     // DN or UPN
  "bind_password_enc": "v1:…",                            // stored only; API exposes bind_password (write-only)
  "admin_group_dn": "CN=NetMon-Admins,OU=Groups,DC=mnidom,DC=net",
  "viewer_group_dn": "CN=NetMon-Viewers,OU=Groups,DC=mnidom,DC=net",
  "timeout_s": 5 }                                        // 1–30
```
Default: servers [], security ldaps, tls_verify true, other strings "", timeout 5, no password. Validation when
`feature_ldap_enabled` is turned on or `ldap` is saved while it is on (422): `bind_dn`, a stored password,
servers or domain, base DN (given or derivable), at least one group DN. Group DNs and base DN must parse as DNs
(`ldap3.utils.dn.parse_dn`).

**Encryption** (`netmon.services.crypto`): key = HKDF-SHA256(`secret.key` bytes, salt none, info
`b"netmon/settings/v1"`, 32 bytes); value = `"v1:" + base64url(nonce12 ‖ AESGCM(ciphertext ‖ tag))`, AAD
`b"ldap.bind_password"`. Decryption failure (secret.key replaced) → treated as unset, `bind_password_unreadable:
true` in the API, one `system` event, LDAP logins → 503 `directory_unavailable`.

**Pure helpers** (`netmon.auth.ldap_pure`) with vectors: `parse_login("jdoe")` → sam `jdoe`;
`("MNIDOM\\jdoe")` → sam `jdoe`; `("mnidom\\jdoe")` → sam (case-insensitive); `("OTHER\\jdoe")` → not a
directory name; `("John.Doe@MNIDOM.NET")` → upn `John.Doe@MNIDOM.NET`, fallback sam `John.Doe`;
`("x@other.com")` → upn only; `escape("a*)(uid=*")` → `a\2a\29\28uid=\2a`; `escape("CN=A\\,B")` →
`CN=A\5c,B`; UAC 512 → ok, 514 → disabled, computed 0x10 → locked, 0x800000 → password_expired;
`accountExpires` 0 and 9223372036854775807 → never; AD message `…data 775,…` → `account_locked`.

## [2b-3] `audit_log`
| Column | Type | Constraints |
|---|---|---|
| id | bigint | PK identity |
| time | timestamptz | NOT NULL DEFAULT now() |
| actor_type | text | NOT NULL CHECK in actor_type (`anonymous` = failed login) |
| actor_user_id | integer | NULL (no FK: survives user deletion) |
| actor_name | varchar(256) | NOT NULL DEFAULT '' — username snapshot, or the name typed at a failed login (cut to 256) |
| token_id | integer | NULL — API token used |
| ip | varchar(45) | NULL — client IP (rightmost X-Forwarded-For when the proxy is trusted) |
| action | varchar(64) | NOT NULL — dotted, list below |
| entity_type | varchar(32) | NULL |
| entity_id | bigint | NULL |
| entity_name | varchar(256) | NULL |
| device_id | integer | NULL — the device concerned (devices, sensors, acks, event clears) for the timeline |
| source | text | NULL CHECK in (`local`,`ldap`,`token`) — logins |
| reason | varchar(64) | NULL — login failure reason codes (api.md) |
| changes | jsonb | NOT NULL DEFAULT '{}' — `{"field": [old, new]}`; secrets as `["***","***"]` when changed (never values); create = new values; delete = old values (secrets omitted) |

Indexes: `(time DESC)`, `(actor_user_id, time DESC)`, `(entity_type, entity_id, time DESC)`, `(device_id, time
DESC) WHERE device_id IS NOT NULL`, `(action, time DESC)`.
Trigger `audit_log_immutable` (plpgsql, migration `0006`): `BEFORE UPDATE` → raise; `BEFORE DELETE` → raise
unless `current_setting('netmon.audit_purge', true) = 'on'`. Retention (hourly loop): `SET LOCAL
netmon.audit_purge = 'on'; DELETE … WHERE time < now() − audit_retention_days` (batches of 50 000).

Actions: `auth.login`, `auth.login_failed`, `auth.logout`, `<entity>.create|update|delete` for `device`,
`group`, `sensor`, `alert_rule`, `notification_channel`, `maintenance_window`, `event_rule`, `topology_link`,
`discovery_profile`, `user`, `api_token` (create/revoke = `api_token.create|revoke`), `device.apply_template`,
`discovery.import`, `settings.update` (entity null, `changes` per key; the `ldap` object diffed per field),
`feature.enable|disable` (entity_name = feature), `sensor.acknowledge|unacknowledge|clear_events`,
`user.ldap_create`, `user.ldap_role_change`, `ingest.assign_source`.

## [2b-3] `api_tokens`
| Column | Type | Constraints |
|---|---|---|
| id | integer | PK |
| user_id | integer | NOT NULL FK `users.id` ON DELETE CASCADE |
| name | varchar(128) | NOT NULL (unique per user, case-insensitive) |
| prefix | varchar(12) | NOT NULL — `nmt_` + first 8 chars of the secret part (display) |
| token_hash | char(64) | NOT NULL UNIQUE — SHA-256 hex of the full token |
| role | text | NOT NULL CHECK in role |
| created_at | timestamptz | NOT NULL |
| expires_at | timestamptz | NOT NULL — 1–365 days after creation |
| last_used_at | timestamptz | NULL |
| last_used_ip | varchar(45) | NULL |
| revoked_at | timestamptz | NULL |

Expired/revoked tokens are deleted 30 days after expiry/revocation by the daily cleanup.

## [2b-3] Events additions
- `notification_sent`: one row per successful send of a stage (problem, escalation, reminder, recovery) per
  rule: `sensor_id`/`device_id`/`group_id`/names set, `old_status`/`new_status` = the notified transition,
  message `Sent DOWN to Ops mail, Ops Teams (rule "All down")`. Written always (needed by the timeline, cheap).
- `port_change`: `device_id` set, `sensor_id` = the port's linked interface sensor or NULL, `old_status` /
  `new_status` = `up`/`down`, message `Port 5 (GigabitEthernet1/0/5) down`; only while `timeline` is ON, ≤ 200
  per run, physical/lag ports, not on the first snapshot of a device.
- Index `ix_events_maintenance (time DESC) WHERE kind = 'maintenance'`.

## [2b-3] Settings keys
| Key | Type | Default | Limits / meaning |
|---|---|---|---|
| `feature_*_enabled` (11 keys) | bool | `false` | § Feature toggles |
| `trap_retention_days` | int | 30 | 1–3650 |
| `syslog_retention_days` | int | 30 | 1–3650 |
| `trap_community_check` | `"any"` \| `"known"` | `"any"` | |
| `trap_communities` | str[] | [] | ≤ 32, each 1–64; **secret list** (write-only) |
| `trap_v3_users` | object[] | [] | ≤ 32 `{username, security_level, auth_protocol, auth_key, priv_protocol, priv_key}` (same value sets as SnmpCredentials; keys secret) |
| `ingest_unknown_sources` | `"store"` \| `"drop"` | `"store"` | |
| `ingest_ignored_sources` | str[] | [] | ≤ 256 IPs or CIDRs |
| `trap_collapse_enabled` | bool | false | **[1.2.1]** keep only the newest traps of `trap_collapse_oids` per source IP and OID (pruned after each write and once when turned on; event rules still see every trap) |
| `trap_collapse_oids` | str[] | hh3cPeriodicalTrap, syslogMsgNotification | ≤ 64 numeric OIDs (leading dot stripped) |
| `trap_collapse_keep` | int | 10 | 1–1000 |
| `trap_names_version` | str | "" | internal: OID name table version the stored `traps.trap_name` values were last updated to (startup backfill) |
| `flap_threshold`, `flap_window_min`, `flap_quiet_min` | int | 5, 10, 15 | § Port flapping |
| `event_linkdown_scope` | `"important"` \| `"all"` | `"important"` | link-down / flapping alert scope (architecture § 12.3, § 12.4); other values → 422 |
| `siem_targets` | object[] | [] | § SIEM |
| `ldap` | object | above | § LDAP settings |
| `audit_retention_days` | int | 365 | 30–3650 |

Process env (not settings): `NETMON_TRAP_PORT`, `NETMON_SYSLOG_PORT`, `NETMON_SYSLOG_TCP_PORT`,
`NETMON_INGEST_HOST` (architecture § 4 / § 12.2.1).

## [2b-3] Phase 2b-3 migration (`0006`)
File `backend/netmon/migrations/versions/0006_phase2b3.py`, `revision = "0006"`, `down_revision = "0005"`.
Plain PostgreSQL DDL (+ one plpgsql trigger function); no Timescale or extension calls.

`upgrade()`:
1. `sensors_type_check` re-added with the 11 types of 0005 + `'trap','syslog','port_flap'`;
   `CREATE UNIQUE INDEX ux_sensors_event_type …`.
2. `notification_channels_type_check` + `'teams'`; `events_kind_check` + `'notification_sent','port_change'`;
   `CREATE INDEX ix_events_maintenance …`.
3. `users`: `ALTER COLUMN password_hash DROP NOT NULL`, add `source` (+ check), `external_id` (+ unique partial
   index `ux_users_external_id`), `display_name`, `ldap_dn`, `ldap_synced_at`, `users_source_password_check`.
4. `devices`: add `source_addresses inet[] NOT NULL DEFAULT '{}'`, `event_sensor_optout text[] NOT NULL DEFAULT
   '{}'`.
5. `switch_ports`: add `lag_if_index integer`, `lag_attached boolean`.
6. `alert_state`: add `flap_suppressed boolean NOT NULL DEFAULT false`.
7. `CREATE TABLE traps`, `syslog_messages` with their checks and btree indexes (trigram indexes are created by
   the bootstrap).
8. `CREATE TABLE event_rules` (+ checks), seed the 4 built-in rules (English names, `enabled = false`);
   `CREATE TABLE event_conditions` (+ indexes).
9. `CREATE TABLE audit_log` (+ indexes), `CREATE FUNCTION netmon_audit_immutable()` and the trigger.
10. `CREATE TABLE api_tokens`.

`downgrade()` (reverse): drop `api_tokens`, the audit trigger/function and `audit_log`, `event_conditions`,
`event_rules`, `syslog_messages`, `traps`; drop the new columns of `alert_state`, `switch_ports`, `devices`;
`DELETE FROM users WHERE source = 'ldap'` (their FKs are SET NULL/CASCADE as today), drop the user columns,
`SET NOT NULL` on `password_hash`; `DELETE FROM events WHERE kind IN ('notification_sent','port_change')`,
restore `events_kind_check`; `DELETE FROM notification_channels WHERE type = 'teams'` (rule links cascade),
restore its check; drop `ix_events_maintenance`, `ux_sensors_event_type`; `DELETE FROM sensors WHERE type IN
('trap','syslog','port_flap')`, restore `sensors_type_check`. Settings rows stay (harmless). A Timescale
hypertable is dropped by `DROP TABLE` like a plain table.

# **[3]** Phase 3 additions

Scope: network device config backup, NetFlow/IPFIX/sFlow collector, new-device alerts, SSO (Kerberos, OIDC),
reports, NOC kiosk screens, acknowledge from Telegram/Teams. All schema changes are in **migration `0007`**
([§ Phase 3 migration](#3-phase-3-migration-0007)). Everything is additive.

API shapes: [api.md § Phase 3](api.md#3-phase-3-contract). Components and flows:
[architecture.md § 13](architecture.md#13-phase-3-config-backup-flows-new-device-alerts-sso-reports-noc-mode-chat-acknowledge).

## [3] Enum changes

| Name | Values |
|---|---|
| `sensor_type` | … 2b-3 values, **[3]** `config_backup`, `new_mac` (passive event sensors) |
| `event_kind` | … 2b-3 values, **[3]** `config_change`, `new_device` |
| `user_source` | `local`, `ldap`, **[3]** `oidc` (Kerberos SSO users are AD users: `ldap`) |
| `condition_source` | `rule`, `flap`, **[3]** `backup_failed`, `host_key`, `config_changed`, `new_mac` |
| `event_rule_action` | `warning`, `down`, `ignore`, **[3]** `config_backup` |
| `actor_type` | unchanged (`user`, `token`, `system`, `anonymous`); kiosk requests are not audited |
| `feature` | … 11 values of 2b-3, **[3]** `config_backup`, `flows`, `new_device_alerts`, `sso_kerberos`, `sso_oidc`, `reports`, `noc`, `chat_ack` |
| **[3]** `backup_mode` | `auto`, `on`, `off` |
| **[3]** `backup_result` | `ok`, `unchanged`, `failed`, `cancelled` |
| **[3]** `backup_trigger` | `schedule`, `manual`, `event`, `retry` |
| **[3]** `backup_error` | `connect_timeout`, `connect_refused`, `auth_failed`, `host_key_changed`, `prompt_timeout`, `command_error`, `too_large`, `no_driver`, `no_credentials`, `cancelled`, `internal` |
| **[3]** `host_key_state` | `pinned`, `changed` |
| **[3]** `flow_protocol` | `netflow5`, `netflow9`, `ipfix`, `sflow5` |
| **[3]** `exporter_state` | `accepted`, `pending`, `ignored` |
| **[3]** `if_direction` | `in`, `out` |
| **[3]** `host_direction` | `src`, `dst` |
| **[3]** `known_mac_state` | `baseline`, `new`, `known`, `allowed` |
| **[3]** `report_schedule` | `weekly`, `monthly`, `none` |
| **[3]** `report_section` | `availability`, `top_alerts`, `flapping_ports`, `port_errors`, `config_changes`, `new_devices`, `top_talkers` |
| **[3]** `kiosk_widget` | `summary`, `down_devices`, `problems`, `recent_alerts`, `map`, `group_tiles` |
| **[3]** `chat_provider` | `telegram` |

## [3] Feature toggles

Keys `feature_<name>_enabled` (bool, **default `false`**, also on upgraded installs; no migration writes them).

| Key | ON | OFF means exactly |
|---|---|---|
| `feature_config_backup_enabled` | scheduler, manual/trigger queue, `config_backup` sensors, `config_backup` rule action | no SSH connection is opened; scheduler/queue stopped, running sessions cancelled; `config_backup` conditions deleted (sensors `up`, "Configuration backup is turned off"); `/api/config-backups*` and the device `…/config*` endpoints 409 (`POST /api/devices/{id}/ssh/test` stays allowed, so credentials can be prepared); versions, status and host keys kept; retention still runs; `config_backup` rule actions are skipped |
| `feature_flows_enabled` | UDP sockets 2055/4739/6343, worker, writer | sockets closed, worker stopped (current minute discarded); `/api/flows*` 409; data kept; retention and (fallback) hourly rollup still run |
| `feature_new_device_alerts_enabled` | detection after FDB writes, `new_mac` sensors | no detection; `new_mac` conditions deleted (sensors `up`); `/api/new-devices*` 409; `known_macs`, allow-list, learning end kept |
| `feature_sso_kerberos_enabled` | `GET /api/auth/sso/kerberos` negotiates; keytab file present | endpoint 409 (`feature_disabled`); keytab file deleted; settings (incl. the encrypted keytab) kept and editable (`/api/sso/kerberos*` admin endpoints stay available so the admin can prepare) |
| `feature_sso_oidc_enabled` | `/api/auth/oidc/start|callback` work | both 409 (callback: redirect to `/login?sso_error=feature_disabled`); settings editable, `POST /api/sso/oidc/test` still allowed; OIDC users get 401 at their next session check (`src = oidc` sessions are refused while OFF) |
| `feature_reports_enabled` | scheduler, sends, report pages | scheduler stopped; `/api/reports*` 409; definitions and runs kept; run retention still runs |
| `feature_noc_enabled` | kiosk API/WS answer | every kiosk request 404 (indistinguishable), kiosk WS closed (4403); `/api/kiosk-screens*` (admin management) 409; screens kept |
| `feature_chat_ack_enabled` | ack buttons/links added to messages, Telegram poller, ack page API, linking | no buttons/links in new messages; poller stopped; `/api/chat-ack/*` and `/api/chat-links*` 409; presses on old buttons are not received (no poller); links and identities kept |

## [3] SSH credentials (jsonb in `groups.ssh_credentials` / `devices.ssh_credentials`)

`groups` and `devices` gain `ssh_credentials jsonb NULL` (NULL = inherit; device → its group → parent groups →
none). Stored shape (secrets only encrypted, `"v1:…"` as § [2b-3] Encryption, AAD
`b"ssh.<field>"`, e.g. `b"ssh.password"`):

```json
{ "username": "netmon-backup", "port": 22,
  "password_enc": "v1:…",            // null when key auth only
  "private_key_enc": null,           // OpenSSH or PEM private key text (≤ 16 KiB), encrypted
  "passphrase_enc": null,            // for an encrypted private key
  "enable_password_enc": null,       // Cisco enable / other privilege step
  "legacy_algorithms": false }
```
Validation (422): `username` 1–64 printable; `port` 1–65535; at least one of password / private key on create;
private key must parse with asyncssh (`import_private_key`, with the passphrase) — the error text never includes
key material. API secret rules as `SnmpCredentials` (null + `*_set` in responses; omitted/null keep, `""` clears,
string replaces). Decryption failure → treated as unset, `credentials_unreadable: true`, one `system` event.

`devices` also gains:

| Column | Type | Constraints |
|---|---|---|
| config_backup | text | NOT NULL DEFAULT `auto` CHECK in backup_mode |
| config_backup_driver | varchar(64) | NULL — driver id override (validated against the loaded drivers) |
| newdev_excluded_ports | integer[] | NOT NULL DEFAULT '{}' — ifIndexes never treated as access ports (≤ 256) |

## [3] Config backup drivers (data files)

`backend/netmon/configbackup/drivers/<id>.yaml`, loaded at startup with `yaml.safe_load` (package data
`configbackup/drivers/*.yaml`), validated by the pure `validate_driver` (an invalid file is skipped with a load
error shown on the Config backups page, like the 2b-2 template library). Style rules as 2b-2 templates (quote
regexes with single quotes, quote OIDs).

| Key | Type | Meaning |
|---|---|---|
| `id` | str | `^[a-z0-9_]{2,64}$`, equals the file name |
| `name` | str | display name |
| `version` | int | bumped on change |
| `confidence` | `verified` \| `unverified` | |
| `priority` | int | match order (lower first, ties by id) |
| `match.templates` | str[] | 2b-2 template ids (`hpe_comware`, …) |
| `match.sys_object_id` | str[] | sysObjectID prefixes |
| `mode` | `shell` \| `exec` | |
| `prompt` | regex | matched against the last line of the cleaned buffer (multiline off) |
| `pre_prompt` | `[{pattern, send}]` | e.g. `{'pattern': 'Press any key to continue', 'send': '\n'}` (≤ 4) |
| `enable` | `{when: regex, command, password_prompt: regex}` \| null | |
| `session_setup` | str[] | session-only commands; each must be in the global allow-list below |
| `commands` | `[{command, fallback?}]` | backup commands (1–4) |
| `error_patterns` | regex[] | output lines that mean failure |
| `ignore_lines` | regex[] | removed before hashing (`^`-anchored, applied per line) |
| `secrets` | regex[] | per line; every match (`finditer`: a line may hold several secrets, e.g. SNMPv3 auth + priv keys) of every regex is taken on the **original** line and the union of all group-1 spans is replaced by `<secret removed>` (order-independent; a rule never matches inside an inserted mask). A rule must capture the secret, never a keyword: use a negative look-ahead where a keyword can follow (Comware `cipher authentication-mode …`) and no `^` anchor on rules that must hit several secrets on one line |
| `strip_ansi` | bool | default true |
| `legacy_algorithms` | bool | default false |
| `timeouts` | `{prompt_s, command_s}` | defaults 30, 90 |
| `notes` | str | |

**Allow-list of `session_setup` commands** (exact, case-insensitive): `screen-length disable`, `no page`, `terminal
length 0`, `terminal width 0`, `terminal pager 0`, `set clienv rows 0`. **Backup commands** must start with
`display `, `show `, `/export` or `clish -c "show ` and contain none of `;`, `|`, `&`, `>`, `<`, backtick, `$(`,
newline. Forbidden first words for any command (defence in depth): `save`, `write`, `commit`, `configure`,
`config`, `conf`, `copy`, `delete`, `erase`, `reload`, `reboot`, `reset`, `system-view`, `undo`, `set`, `add`,
`remove`, `/system`. Violations → the driver is rejected.

Example (`hpe_comware.yaml`):
```yaml
id: hpe_comware
name: 'HPE Comware 7'
version: 1
confidence: unverified
priority: 100
match:
  templates: ['hpe_comware']
  sys_object_id: ['1.3.6.1.4.1.25506.', '1.3.6.1.4.1.11.2.3.7.11.']
mode: shell
prompt: '^(<[^<>\r\n]{1,64}>|\[[^\[\]\r\n]{1,64}\])\s*$'
pre_prompt: []
enable: null
session_setup: ['screen-length disable']
commands:
  - command: 'display current-configuration'
error_patterns: ['^\s*% (Unrecognized|Incomplete|Too many|Wrong parameter)']
ignore_lines: []
secrets:
  - '\b(?:cipher|hash)\s+(?!authentication-mode\b|privacy-mode\b)(\S+)'
  - '^\s*snmp-agent community (?:(?:read|write)\s+)?(?:simple\s+|cipher\s+)?(\S+)'
  - '\b(?:authentication-mode|privacy-mode)\s+\S+\s+(?!cipher\b|simple\b)(\S+)'
  - '\b(?:key|password)\s+(?:\S+\s+)?(?:simple|cipher)\s+(\S+)'
notes: 'IRF: one config for the stack. Verify on FF 5945.'
```

The other six drivers (values; full files ship in the repo):

| id | match | prompt | setup | commands | ignore_lines | secrets (group 1 masked) |
|---|---|---|---|---|---|---|
| `hpe_arubaos_switch` | templates `hpe_arubaos_switch`; sysObjectID `1.3.6.1.4.1.11.2.3.7.11.` (after Comware by priority 110: Comware matches by template first) | `'^[\w.\-]{1,64}(\([\w\-]+\))?# ?$'` | `no page` | `show running-config` | `'^Running configuration:$'` | `'^\s*password (?:manager\|operator\|port-access) .*?(?:sha1\|sha256\|plaintext) "?((?<=")[^"]+\|[^"\s]+)'`, `'\bpassword\b.*?\b(?:sha1\|sha256\|plaintext) "?((?<=")[^"]+\|[^"\s]+)'`, `'^\s*snmp-server community "([^"]+)"'`, `'\bkey "?((?<=")[^"]+\|[^"\s]+)'`, `'\b(?:auth\|priv)\s+(?:md5\|sha\S*\|des\|3des\|aes\S*)\s+"?((?<=")[^"]+\|[^"\s]+)'` ; `pre_prompt` "Press any key to continue" → `\n` |
| `aruba_cx` | templates `aruba_cx`; sysObjectID `1.3.6.1.4.1.47196.` | `'^[\w.\-]{1,64}# ?$'` | `no page` | `show running-config` | `'^Current configuration:$'` | `'\bciphertext (\S+)'`, `'\bplaintext (\S+)'`, `'^\s*snmp-server community (\S+)'` |
| `checkpoint_gaia` | templates `checkpoint_gaia`; sysObjectID `1.3.6.1.4.1.2620.` | — (exec) | — | `show configuration` (fallback `clish -c "show configuration"`) | `'^# Exported by \S+ on '`, `'^# Configuration of \S+$'` (keeps hostname stable) | `'password-hash (\S+)'`, `'^set snmp community (\S+)'`, `'^set expert-password-hash (\S+)'`, `'\b(?:auth\|privacy)-pass-(?:phrase\|hash) (\S+)'`, `'(?<![\w-])secret (\S+)'`, `'^(?:add\|set) aaa tacacs-servers .*?\bkey (\S+)'` |
| `cisco_ios` | templates `cisco_ios`; sysObjectID `1.3.6.1.4.1.9.1.` | `'^[\w.\-:/]{1,64}[>#] ?$'`; enable when `'>\s*$'` | `terminal length 0`, `terminal width 0` | `show running-config` | `'^! (Last configuration change\|NVRAM config last updated) at'`, `'^Building configuration'`, `'^Current configuration : \d+ bytes'`, `'^ntp clock-period'` | `'^\s*enable (?:secret\|password)(?: \d)? (\S+)'`, `'^\s*username \S+ .*?(?:secret\|password)(?: \d)? (\S+)'`, `'^\s*snmp-server community (\S+)'`, `'^\s*snmp-server host \S+ (?:informs \|traps )?(?:version (?:1\|2c\|3 \S+) )?(\S+)'`, `'\bpre-shared-key(?: (?:local\|remote))?(?: \d)? (\S+)'`, `'\bkey(?:-string)?(?: \d+)?(?: md5)?(?: \d)? (\S+)'`, `'(?<![\w-])password(?: \d)? (\S+)'`, `'\bauth (?:md5\|sha\S*) (\S+)'`, `'\bpriv (?:des\S*\|3des\|aes(?: \d+)?) (\S+)'` |
| `fortigate` | templates `fortigate`; sysObjectID `1.3.6.1.4.1.12356.` | — (exec) | — | `show full-configuration` | `'^#conf_file_ver='`, `'^#buildno='` | `'\bset [\w-]+ ENC (\S+)'` (every `ENC` value is a secret). Not maskable per line: `config system snmp community` → `set name "<community>"` (context needed) |
| `mikrotik_routeros` | templates `mikrotik`; sysObjectID `1.3.6.1.4.1.14988.` | — (exec) | — | `/export` | `'^# \S+ \d\d:\d\d:\d\d by RouterOS'`, `'^# \d{4}-\d\d-\d\d \d\d:\d\d:\d\d by RouterOS'`, `'^# software id = '` | `'\b[\w-]*(?:password\|secret\|passphrase\|pre-shared-key\|private-key\|auth-key\|psk)=("[^"]*"\|\S+)'` (RouterOS 7 hides most secrets in `/export`; this covers v6 and `show-sensitive` exports) |

**`match_driver` vectors** (drivers above): device with template `hpe_comware` → `hpe_comware`; template
`hpe_arubaos_switch` → `hpe_arubaos_switch`; no template, sysObjectID `1.3.6.1.4.1.25506.11.1.240` →
`hpe_comware`; no template, `1.3.6.1.4.1.11.2.3.7.11.181` → `hpe_comware` (priority 100 < 110; the HP prefix is
shared — **documented ambiguity**: ArubaOS-Switch devices are identified by their template, which 2b-2 classifies
from sysDescr); sysObjectID `1.3.6.1.4.1.2620.1.6.123` → `checkpoint_gaia`; `1.3.6.1.4.1.8072.3.2.10` (net-snmp,
no template) → none; override `cisco_ios` on any device → `cisco_ios`; override with an unknown id → 422 at save.

## [3] `ssh_host_keys`
| Column | Type | Constraints |
|---|---|---|
| device_id | integer | PK, FK `devices.id` ON DELETE CASCADE |
| algorithm | varchar(64) | NOT NULL — e.g. `ssh-ed25519`, `rsa-sha2-512` key type `ssh-rsa` |
| fingerprint | varchar(64) | NOT NULL — `SHA256:` + base64 (OpenSSH style, no padding) |
| public_key | text | NOT NULL — OpenSSH one-line format |
| state | text | NOT NULL DEFAULT `pinned` CHECK in host_key_state |
| presented_algorithm | varchar(64) | NULL — key seen while `changed` |
| presented_fingerprint | varchar(64) | NULL |
| presented_public_key | text | NULL |
| pinned_at | timestamptz | NOT NULL |
| changed_at | timestamptz | NULL |
| accepted_by | varchar(64) | NULL — username of the last accept |

Decisions (pure `host_key_decision(row, presented) -> accept | pin | reject_changed`): no row → `pin` (insert);
row `pinned` and same fingerprint → `accept`; row `pinned`, different → `reject_changed` (state `changed`,
`presented_*` stored); row `changed` → `reject_changed` (presented updated) until accept. **Accept** (admin, with
the fingerprint shown in the UI as a guard: request carries `fingerprint`, must equal `presented_fingerprint`, else
409 `host_key_mismatch`) → the presented key becomes the pinned key, state `pinned`. **Reset** → row deleted
(next connection pins).

## [3] `config_backup_status`
| Column | Type | Constraints |
|---|---|---|
| device_id | integer | PK, FK `devices.id` ON DELETE CASCADE |
| driver | varchar(64) | NULL — driver used by the last attempt |
| last_attempt_at | timestamptz | NULL |
| last_result | text | NULL CHECK in backup_result |
| last_error | text | NULL CHECK in backup_error |
| last_message | varchar(500) | NULL — human detail (never secrets) |
| last_trigger | text | NULL CHECK in backup_trigger |
| last_duration_ms | integer | NULL |
| last_success_at | timestamptz | NULL — last `ok` or `unchanged` |
| last_change_at | timestamptz | NULL — last stored non-initial version |
| last_unchanged_at | timestamptz | NULL |
| consecutive_failures | integer | NOT NULL DEFAULT 0 |
| next_due_at | timestamptz | NULL — next scheduled/retry/debounced run |
| pending_trigger | boolean | NOT NULL DEFAULT false — "run again after the current/limit window" |

## [3] `config_versions`
| Column | Type | Constraints |
|---|---|---|
| id | bigint | PK identity |
| device_id | integer | NOT NULL FK `devices.id` ON DELETE CASCADE |
| collected_at | timestamptz | NOT NULL |
| trigger | text | NOT NULL CHECK in backup_trigger |
| driver | varchar(64) | NOT NULL |
| sha256 | char(64) | NOT NULL — of the normalized content |
| content | text | NOT NULL — normalized (masked when masking was on) |
| masked | boolean | NOT NULL |
| size_bytes | integer | NOT NULL |
| lines | integer | NOT NULL |
| added | integer | NOT NULL DEFAULT 0 — vs the previous version (0 for initial) |
| removed | integer | NOT NULL DEFAULT 0 |
| initial | boolean | NOT NULL — first version of the device (or first after all versions were deleted) |
| is_latest | boolean | NOT NULL |

Indexes: `ix_config_versions_device (device_id, collected_at DESC)`, unique partial `ux_config_versions_latest
(device_id) WHERE is_latest`; trigram (bootstrap, `rt.trgm_enabled`) `ix_config_versions_trgm USING gin (content
gin_trgm_ops) WHERE is_latest`. `version_no` in the API = row number per device by `collected_at` (computed).

**Retention selection** (pure `select_expired(versions, now, max_versions, retention_days, min_versions)`): sort
newest first; keep index 0 (latest) and indexes < `min_versions`; from the rest delete those with index ≥
`max_versions` or `collected_at < now − retention_days`. Vectors (max 5, days 30, min 2): 7 versions all fresh →
delete the 2 oldest; versions aged 0, 40, 50 d → keep 0 and 40 (min 2), delete 50; 1 version aged 400 d → keep.

**Normalisation vectors** (pure `normalize`): Cisco raw `show running-config\r\nBuilding configuration...\r\n\r\nCurrent
configuration : 1234 bytes\r\n!\r\n! Last configuration change at 10:00:00 UTC Sun Nov 8 2026\r\nhostname r1\r\nenable
secret 9 $9$abc\r\nr1#` → `!\nhostname r1\nenable secret 9 <secret removed>\n` (masking on); ArubaOS raw with
`\x1b[24;1H\x1b[2K` sequences and `-- MORE --, next page: Space` absent (paging off) → ANSI removed; Comware
`display current-configuration\n#\n version 7.1.070\n#\n snmp-agent community read cipher $c$3$xyz\n#\nreturn\n<sw1>`
→ `#\n version 7.1.070\n#\n snmp-agent community read cipher <secret removed>\n#\nreturn\n`; the same with masking
off keeps `$c$3$xyz`; two outputs equal except an ignored line → same sha256.

## [3] Event rule action `config_backup`
`event_rules.action` gains `config_backup`: valid for trap and syslog rules, no key, no clear fields (422
otherwise). Evaluation (pure `rules.backup_triggers(record, rules) -> bool`): any enabled `config_backup` rule in
scope whose `match` matches → the device is queued (architecture § 13.2.4). These rules are evaluated when
`config_backup` is ON, independent of `event_alerts`, and are **skipped** by the 2b-3 raise/clear evaluation
(`evaluate` ignores them). `matched_rule_ids` of the record includes them.

### Built-in config-change rules (seeded by `0007`, **enabled = false**)
| builtin_key | name (en / he) | kind | priority | match | action |
|---|---|---|---|---|---|
| `syslog_comware_cfg_changed` | Comware configuration changed / שינוי תצורה Comware | syslog | 2000 | `{"severity_max":null,"facilities":[],"programs":["CFGMAN"],"text":null,"text_regex":false}` (every message of the Comware configuration-management module, e.g. `%%10CFGMAN/5/CFGMAN_CFGCHANGED: …`; the debounce merges a save and a change into one backup) | config_backup |
| `syslog_arubaos_cfg_changed` | ArubaOS-Switch configuration changed / שינוי תצורה ArubaOS | syslog | 2010 | `{"severity_max":null,"facilities":[],"programs":["mgr"],"text":"(running\|startup) config(uration)? (file )?changed","text_regex":true}` | config_backup |
| `syslog_cisco_config_i` | Cisco IOS configured / שינוי תצורה Cisco | syslog | 2020 | `{"severity_max":null,"facilities":[],"programs":[],"text":"%SYS-5-CONFIG_I","text_regex":false}` | config_backup |

Confidence: Cisco high; Comware and ArubaOS **medium** — the owner sends real lines (architecture § 13.13) and the
patterns are adjusted before they are marked verified. Turning `config_backup` ON for the first time shows "Enable
the built-in config-change triggers?" (like 2b-3).

## [3] Flow collector

### `flow_exporters`
| Column | Type | Constraints |
|---|---|---|
| id | integer | PK |
| address | inet | NOT NULL UNIQUE — sFlow agent address / NetFlow-IPFIX UDP source |
| device_id | integer | NULL FK `devices.id` ON DELETE SET NULL |
| state | text | NOT NULL CHECK in exporter_state |
| protocols | text[] | NOT NULL DEFAULT '{}' — subset of flow_protocol seen |
| sampling_override | integer | NULL CHECK 1–1 000 000 |
| sampling_seen | integer | NULL — last effective rate |
| first_seen, last_seen | timestamptz | NOT NULL |
| records_total | bigint | NOT NULL DEFAULT 0 (updated at most once per minute) |

Auto state at first sight: attributed to a device → `accepted` (device_id set); else `pending`. A device's
source-address change re-attributes `pending` exporters on the next minute. ≤ 256 rows (more → not inserted,
`exporter_limit`).

### 1-minute tables (hypertables in Timescale mode, chunk 6 h; plain tables otherwise)
All have `time timestamptz NOT NULL` (minute start, UTC), `exporter_id integer NOT NULL` (no FK — like
`measurements`; exporter delete → background delete), `bytes bigint NOT NULL`, `packets bigint NOT NULL`, `flows
integer NOT NULL` (after sampling correction for bytes/packets; `flows` = records).

| Table | Extra columns | Key (unique index) | Other index |
|---|---|---|---|
| `flow_if_1m` | `if_index integer NOT NULL`, `direction text NOT NULL CHECK in if_direction` | `(exporter_id, if_index, direction, time)` | `(time DESC)` |
| `flow_app_1m` | `protocol smallint NOT NULL`, `port integer NOT NULL` (-1 = other) | `(exporter_id, protocol, port, time)` | `(time DESC)` |
| `flow_host_1m` | `ip inet NOT NULL`, `direction text NOT NULL CHECK in host_direction`, `other boolean NOT NULL DEFAULT false` (ip `0.0.0.0` when other) | `(exporter_id, ip, direction, other, time)` | `(time DESC)`, `(ip, time DESC)` |
| `flow_conv_1m` | `in_if integer`, `out_if integer`, `src_ip inet`, `dst_ip inet`, `protocol smallint`, `src_port integer`, `dst_port integer` (all NOT NULL; the non-service port is 0), `other boolean NOT NULL DEFAULT false` | none (insert only) | `(exporter_id, time DESC)` |

Hourly objects (bootstrap-created, architecture § 13.3.6): `flow_if_1h (time, exporter_id, if_index, direction,
bytes, packets, flows)`, `flow_app_1h (time, exporter_id, protocol, port, …)`, `flow_host_1h (time, exporter_id,
ip, direction, other, …)`. Fallback tables have PK `(exporter_id, <keys>, time)`. Timescale bootstrap SQL
(in a savepoint each; any failure → fallback tables for all three, `flows_rollup = "table"`):
```sql
SELECT create_hypertable('flow_if_1m','time', chunk_time_interval => INTERVAL '6 hours',
       if_not_exists => TRUE, migrate_data => TRUE, create_default_indexes => FALSE);   -- same for app/host/conv
CREATE MATERIALIZED VIEW IF NOT EXISTS flow_if_1h WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
  SELECT time_bucket(INTERVAL '1 hour', time) AS time, exporter_id, if_index, direction,
         sum(bytes)::bigint AS bytes, sum(packets)::bigint AS packets, sum(flows)::bigint AS flows
  FROM flow_if_1m GROUP BY 1, 2, 3, 4 WITH NO DATA;
SELECT add_continuous_aggregate_policy('flow_if_1h', start_offset => INTERVAL '6 hours',
       end_offset => INTERVAL '1 hour', schedule_interval => INTERVAL '15 minutes', if_not_exists => TRUE);
SELECT add_retention_policy('flow_if_1m', INTERVAL '<flow_minute_retention_hours> hours', if_not_exists => TRUE);
SELECT add_retention_policy('flow_if_1h', INTERVAL '<flow_rollup_retention_days> days', if_not_exists => TRUE);
-- flow_conv_1m: retention <flow_raw_retention_hours> hours; policies re-applied on settings change
```
Fallback rollup (hourly loop, last 3 complete hours, idempotent):
```sql
INSERT INTO flow_if_1h (time, exporter_id, if_index, direction, bytes, packets, flows)
SELECT date_bin(INTERVAL '1 hour', time, TIMESTAMPTZ '2000-01-01'), exporter_id, if_index, direction,
       sum(bytes), sum(packets), sum(flows)
FROM flow_if_1m WHERE time >= :from AND time < :to GROUP BY 1, 2, 3, 4
ON CONFLICT (exporter_id, if_index, direction, time)
DO UPDATE SET bytes = EXCLUDED.bytes, packets = EXCLUDED.packets, flows = EXCLUDED.flows;
```

### Services map (`backend/netmon/data/services.tsv`, pure `netmon.flows.services`)
UTF-8 TSV `protocol<TAB>port<TAB>name` (~250 rows, from the IANA service-name registry subset + common
enterprise ports: 22 ssh, 53 dns, 80 http, 123 ntp, 161/162 snmp, 389/636 ldap(s), 443 https, 445 smb, 514
syslog, 1433 mssql, 3389 rdp, 5985/5986 winrm, 8080 http-alt, 8443 https-alt, 2055/4739/6343 flows, …); protocol 6
tcp, 17 udp; ICMP (1) and ICMPv6 (58) → port 0 name `icmp`/`icmpv6`; GRE 47, ESP 50 → port 0 with protocol
names. Settings `flow_app_overrides` (≤ 200 `{protocol, port, name}`) win over the file.
`service_port(proto, sp, dp)`: proto without ports → 0; else the port present in the map (dp first, then sp);
else the lower of the two when it is < 1024; else the lower of the two ports when either is < 49152; else 0
(both ephemeral). Vectors: (6, 51000, 443) → 443; (6, 443, 51000) → 443; (17, 53000, 53) → 53; (6, 50000, 8443) →
8443; (6, 3000, 50000) → 3000; (17, 60000, 61000) → 0; (1, 0, 0) → 0.

### Decoder vectors (QA builds the datagrams with `tests/sim/flows/send_flows.py`; unit tests use bytes literals)
- v5: header count 2, sampling `0x4064` (mode 1, interval 100) → 2 records, rate 100; count 31 → `parse_error`.
- v9: template 256 (fields 8,12,7,11,4,1,2,10,14) then data → records; data before template → `no_template`; options
  template 257 with 34 = 512 → exporter rate 512; a data record whose length is shorter than the template →
  `parse_error` for that set only (other sets of the datagram still processed).
- IPFIX: template 300 with a variable-length field (65535) and an enterprise field (bit 15, PEN 9) → skipped
  correctly; withdrawal (field count 0) then data → `no_template`; 305/306 = 1/99 → rate 100.
- sFlow: expanded flow sample, sampling rate 4096, raw header (Ethernet + 802.1Q + IPv4 + TCP 51000→443),
  frame_length 1514 → 1 record bytes 1514 × 4096 = 6 201 344, packets 4096, ingress ifIndex from the sample;
  agent address 10.0.0.12 used as exporter even when the UDP source is 172.17.0.1.

### Aggregation vectors (pure `MinuteAggregator`, top_apps 2, top_hosts 1, top_conversations 1)
Records (bytes): A 10.0.0.1:51000→10.0.0.9:443 tcp 600 in_if 1 out_if 2; B 10.0.0.2:52000→10.0.0.9:443 tcp 300 in 1
out 2; C 10.0.0.1:53000→8.8.8.8:53 udp 100 in 1 out 3. → `flow_if_1m`: (1,in) 1000, (2,out) 900, (3,out) 100;
`flow_app_1m`: (6,443) 900, (17,53) 100; `flow_host_1m` src: 10.0.0.1 700 + other 300, dst: 10.0.0.9 900 + other
100; `flow_conv_1m`: (1,2,10.0.0.1,10.0.0.9,6,0,443) 600 + other 400. Sums per table = 1000.

## [3] New-device alerts

### `known_macs`
| Column | Type | Constraints |
|---|---|---|
| mac | varchar(17) | PK — `aa:bb:cc:dd:ee:ff` |
| state | text | NOT NULL CHECK in known_mac_state |
| vendor | varchar(64) | NULL — OUI snapshot |
| first_seen | timestamptz | NOT NULL |
| first_device_id | integer | NULL FK `devices.id` ON DELETE SET NULL |
| first_if_index | integer | NULL |
| first_port_name | varchar(255) | NULL — snapshot |
| first_vlan | integer | NULL |
| first_ip | varchar(45) | NULL — snapshot at detection |
| first_hostname | varchar(255) | NULL |
| last_seen | timestamptz | NOT NULL |
| last_device_id | integer | NULL FK `devices.id` ON DELETE SET NULL |
| last_if_index | integer | NULL |
| last_vlan | integer | NULL |
| detected_at | timestamptz | NULL — when it became `new` |
| ack_at | timestamptz | NULL |
| ack_by | varchar(64) | NULL |
| ack_comment | varchar(500) | NOT NULL DEFAULT '' |

Indexes: `(state, detected_at DESC)`, `(first_seen DESC)`, `(last_device_id)`. Not subject to retention (the
register is the baseline; "Forget" deletes rows; rows of MACs not seen for `newdev_forget_after_days` — default
**0 = never** — are deleted by the daily task).

### `mac_allowlist`
| Column | Type | Constraints |
|---|---|---|
| id | integer | PK |
| prefix | varchar(17) | NOT NULL UNIQUE — normalised lower-case hex with colons: 6 hex digits (OUI, `00:1b:21`), 7 (`00:1b:21:a`), 9 (`00:1b:21:aa:b`) or 12 (full MAC) |
| bits | smallint | NOT NULL CHECK in (24, 28, 36, 48) |
| comment | varchar(200) | NOT NULL DEFAULT '' |
| created_by | varchar(64) | NULL |
| created_at | timestamptz | NOT NULL |

≤ 5000 rows. Match = the MAC's first `bits` bits equal the prefix.

### Pure rules and vectors
`is_access_port(port, view)`: (kind `physical`, lag_if_index null, not uplink, mode ≠ `trunk`, if_index ∉
excluded). Vectors: physical access, 1 MAC → true; physical, `mac_count` 40 > threshold 25 → false (uplink by
mac_count); physical with LLDP neighbour switch → false; `Trk1` member → false; mode `trunk` → false; mode null
(no VLAN data) → true; kind `mgmt` → false; excluded ifIndex → false.

`classify_sightings(sightings, known, allowlist, ctx{first_read, learning, ignore_random})` → per MAC one of
`skip_random`, `update_known`, `baseline`, `allowed`, `new`; order exactly as architecture § 13.4 rules 2–7.
Vectors: `02:00:00:aa:00:02` ignore_random → `skip_random`; `01:00:5e:00:00:01` (multicast) → `skip_random`;
known MAC → `update_known`; unknown + first_read → `baseline`; unknown + learning → `baseline`; unknown + OUI
`3c:a8:2a` allow-listed → `allowed`; unknown otherwise → `new`; 25 `new` in one run → 20 events + 1 summary.

Settings: `newdev_learning_days` (0–60, default 7), `newdev_learning_until` (timestamptz, set by the first ON;
read-only in the API except through "Restart learning"), `newdev_scope_group_ids` (int[], ≤ 100, [] = all),
`newdev_ignore_random_macs` (bool, true), `newdev_condition_clear_min` (10–10080, 1440), `newdev_forget_after_days`
(0 or 30–3650, 0). Per-switch "first read since ON" is tracked in `port_inventory_status.newdev_baselined_at`
(timestamptz NULL, new column; cleared for all rows when the feature is turned ON).

## [3] Users, SSO

`users.source` gains `oidc` (check updated). `users_source_password_check` unchanged (`oidc` users have no
password). `external_id` holds the Entra `oid` for `oidc` users (GUID lower-case, 36 chars with dashes — column
widened to `varchar(64)` already). Sessions: JWT claim `am` (`password` | `kerberos` | `oidc` | absent = password).

### Kerberos settings (key `kerberos`)
```json
{ "realms": ["MNIDOM.NET"],            // accepted client realms (upper-case), 1–8
  "auto_login": true,
  "keytab_enc": "v1:…",                // stored only; AAD b"kerberos.keytab"; API: keytab_set, keytab_entries
  "keytab_uploaded_at": "2026-11-08T10:00:00Z" }
```
Default: realms derived from `ldap.domain` upper-cased when empty, auto_login true, no keytab. Turning the feature
ON requires a keytab with ≥ 1 entry and `feature_ldap_enabled` (422 `details` with the missing parts).

**Keytab parser vectors** (pure `parse_keytab(bytes) -> [{principal, kvno, enctype}]`): file version `0x0502`,
entries `int32 size` (negative = deleted, skipped), principal components with realm, name type, timestamp, kvno8
(and the optional 32-bit kvno at the end of the entry when present), enctype, key. Version `0x0501` → 422
`invalid_keytab`; truncated → 422; enctype 23 only → warning `rc4_only`; no `HTTP/` principal → warning
`no_http_principal`.

**Principal → account** (pure `kerberos_pure.split_principal`): `jdoe@MNIDOM.NET` → (`jdoe`, `MNIDOM.NET`);
`JDoe@mnidom.net` → realm upper-cased; `HTTP/x@MNIDOM.NET` (service principal) → refused `sso_failed`;
`a@b@C` → refused. sAMAccountName = the name part (AD Kerberos principal names equal sAMAccountName).

**NTLM detection vectors** (`token_mech`): `NTLMSSP\0…` → `ntlm`; SPNEGO NegTokenInit (`0x60 … 1.3.6.1.5.5.2`) with
mechTypes [`1.2.840.48018.1.2.2` (MS KRB5), `1.2.840.113554.1.2.2`] → `kerberos`; mechTypes [`1.3.6.1.4.1.311.2.2.10`
(NTLM)] only → `ntlm`; NegTokenInit with Kerberos mechTypes but an NTLMSSP mechToken → `ntlm`; garbage → `invalid`.

### OIDC settings (key `oidc`)
```json
{ "tenant_id": "00000000-0000-0000-0000-000000000000",
  "client_id": "11111111-1111-1111-1111-111111111111",
  "client_secret_enc": "v1:…",           // AAD b"oidc.client_secret"; API: client_secret (write-only), client_secret_set
  "client_secret_expires_at": null,      // optional date entered by the admin (reminder only)
  "role_source": "app_roles",            // "app_roles" | "groups"
  "admin_values": ["NetMon.Admin"],      // app role values, or group object ids when role_source = groups (≤ 16)
  "viewer_values": ["NetMon.Viewer"],
  "button_label": null }                 // optional, else "Sign in with Microsoft"
```
Turning `sso_oidc` ON validates tenant/client ids (GUIDs), a stored secret, and ≥ 1 admin or viewer value (422).
Process env `NETMON_OIDC_AUTHORITY` (default `https://login.microsoftonline.com`, tests only).

**id_token validation vectors** (pure `oidc.validate_claims(claims, cfg, nonce, now)`): valid → ok; `iss` other
tenant → `sso_failed`; `aud` other client → `sso_failed`; `exp` = now − 301 s → `sso_failed`; `exp` = now − 299 s →
ok (leeway); `nonce` mismatch → `sso_failed`; `roles` [`NetMon.Viewer`] → viewer; [`NetMon.Admin`,
`NetMon.Viewer`] → admin; [] → `not_in_group`; role_source groups with `_claim_names.groups` → `oidc_group_overage`.

## [3] Port flapping episodes (for reports)
### `port_flap_episodes`
| Column | Type | Constraints |
|---|---|---|
| id | bigint | PK identity |
| device_id | integer | NOT NULL FK `devices.id` ON DELETE CASCADE |
| if_index | integer | NOT NULL |
| port_name | varchar(255) | NOT NULL DEFAULT '' |
| started_at | timestamptz | NOT NULL |
| ended_at | timestamptz | NULL |
| transitions | integer | NOT NULL |

Written by the 2b-3 flapping tracker (insert at start, update at end and on each transition, at most once per 10 s)
while `flapping` is ON. Index `(started_at DESC)`, `(device_id, started_at DESC)`. Retention with
`event_retention_days`.

## [3] Reports

### `reports`
| Column | Type | Constraints |
|---|---|---|
| id | integer | PK |
| name | varchar(128) | NOT NULL UNIQUE (case-insensitive) |
| enabled | boolean | NOT NULL DEFAULT true |
| language | text | NOT NULL CHECK in language |
| scope_group_ids | integer[] | NOT NULL DEFAULT '{}' — recursive; with `scope_device_ids` both empty = everything |
| scope_device_ids | integer[] | NOT NULL DEFAULT '{}' |
| sections | text[] | NOT NULL — ordered, distinct, ⊆ report_section, ≥ 1 |
| schedule | text | NOT NULL CHECK in report_schedule |
| weekday | smallint | NULL — 1 (Mon)–7, iff weekly |
| month_day | smallint | NULL — 1–28, iff monthly |
| send_time | varchar(5) | NULL — `HH:MM`, iff weekly/monthly |
| email_to | text[] | NOT NULL DEFAULT '{}' — ≤ 50 addresses |
| channel_ids | integer[] | NOT NULL DEFAULT '{}' — notification channels (email/teams/telegram/webhook) |
| next_run_at | timestamptz | NULL |
| last_run_id | bigint | NULL |
| created_at, updated_at | timestamptz | |

### `report_runs`
| Column | Type | Constraints |
|---|---|---|
| id | bigint | PK identity |
| report_id | integer | NULL FK `reports.id` ON DELETE SET NULL (on-demand runs of unsaved definitions: NULL) |
| report_name | varchar(128) | NOT NULL — snapshot |
| trigger | text | NOT NULL CHECK in (`schedule`,`manual`) |
| period_start, period_end | timestamptz | NOT NULL |
| started_at, finished_at | timestamptz | `finished_at` NULL while running |
| status | text | NOT NULL CHECK in (`running`,`ok`,`partial`,`failed`) — partial = some deliveries failed |
| language | text | NOT NULL |
| data | jsonb | NOT NULL — `ReportData` (api.md) |
| html | text | NULL — the rendered email/page HTML (≤ 2 MB) |
| deliveries | jsonb | NOT NULL DEFAULT '[]' — `[{"target": "email:ops@mnidom.net" \| "channel:4", "ok": true, "error": null, "at": "…"}]` |
| created_by | varchar(64) | NULL |

Index `(report_id, started_at DESC)`, `(started_at DESC)`. Retention `report_retention_days` (30–3650, 400).

### Report vectors (pure `availability`, period 2026-11-02T00:00 → 2026-11-09T00:00 = 168 h)
1. Last change before the period → `up`; changes: down at +10 h, up at +12 h → measured 168 h, downtime 2 h,
   availability 98.81 %, outages 1, longest 2 h.
2. As 1 plus a maintenance window +11 h → +13 h covering the device → measured 166 h, downtime 1 h → 99.40 %.
3. `down` from before the period until +1 h, then `unreachable` +1 h → +5 h, then `up` → measured 164 h, downtime 1 h
   → 99.39 %, outages 1 (the outage started before the period: counted, longest = 1 h within the period).
4. Sensor created at +100 h (first event `unknown`→`up`), never down → measured 68 h, 100.00 %.
5. Paused the whole period → measured 0 → `null` ("n/a").
6. `warning` for 20 h, otherwise up → 100.00 %.
Group: devices of vectors 1 (168 h measured, 98.81) and 4 (68 h, 100) → (166 + 68) / (168 + 68) = 99.15 %.
Schedule vectors (tz `Asia/Jerusalem`): weekly Monday 07:47 created Sunday 2026-11-08 12:00 → next 2026-11-09T05:47Z
(IST +2), period 2026-11-02T00:00+02 → 2026-11-09T00:00+02; monthly day 1 07:47 on 2026-03-31 → next
2026-04-01T04:47Z (IDT +3), period March (2026-03-01T00:00+02 → 2026-04-01T00:00+03, 743 h).

## [3] NOC kiosk screens

### `kiosk_screens`
| Column | Type | Constraints |
|---|---|---|
| id | integer | PK |
| name | varchar(128) | NOT NULL UNIQUE |
| enabled | boolean | NOT NULL DEFAULT true |
| token_hash | char(64) | NOT NULL UNIQUE — SHA-256 hex of the token |
| token_prefix | varchar(12) | NOT NULL — `nmk_` + 8 chars (display) |
| allowed_sources | text[] | NOT NULL — 1–64 IPs/CIDRs (IPv4/IPv6), validated |
| scope_group_id | integer | NULL FK `groups.id` ON DELETE SET NULL (NULL = everything) |
| widgets | text[] | NOT NULL — ordered, distinct, ⊆ kiosk_widget, ≥ 1 |
| language | text | NOT NULL CHECK in language |
| show_addresses | boolean | NOT NULL DEFAULT false |
| refresh_s | integer | NOT NULL DEFAULT 30 CHECK 10–300 |
| last_seen_at | timestamptz | NULL (updated at most once per minute) |
| last_seen_ip | varchar(45) | NULL |
| created_at, updated_at | timestamptz | |

Token `nmk_` + `secrets.token_urlsafe(32)` (43 chars). Guard vectors (pure `kiosk.guard(ip, sources)`):
`10.1.2.3` in [`10.1.2.0/24`] → ok; `10.1.3.1` → deny; `::ffff:10.1.2.3` normalised → ok; [] → deny (cannot be
saved anyway).

## [3] Chat acknowledge

### `chat_identities`
| Column | Type | Constraints |
|---|---|---|
| id | integer | PK |
| user_id | integer | NOT NULL FK `users.id` ON DELETE CASCADE |
| provider | text | NOT NULL CHECK in chat_provider |
| external_id | varchar(64) | NOT NULL — Telegram `from.id` as text |
| display | varchar(128) | NOT NULL DEFAULT '' — `@username` or first/last name |
| linked_at | timestamptz | NOT NULL |
| last_used_at | timestamptz | NULL |

Unique `(provider, external_id)` (one NetworkMonitor user per Telegram account); a user may link several.

### `chat_link_codes`
| Column | Type | Constraints |
|---|---|---|
| code_hash | char(64) | PK — SHA-256 of the normalised code (upper-case, no `NM-`) |
| user_id | integer | NOT NULL FK `users.id` ON DELETE CASCADE |
| provider | text | NOT NULL |
| expires_at | timestamptz | NOT NULL — created + 10 min |
| used_at | timestamptz | NULL |

At most 3 unexpired codes per user (oldest replaced); rows deleted 1 day after expiry.

### `telegram_poll_state`
| Column | Type | Constraints |
|---|---|---|
| bot_key | char(64) | PK — SHA-256 of the bot token |
| bot_username | varchar(64) | NULL — from `getMe` |
| next_offset | bigint | NOT NULL DEFAULT 0 |
| state | text | NOT NULL DEFAULT `idle` — `idle` \| `polling` \| `error` |
| last_error | varchar(500) | NULL |
| updated_at | timestamptz | NOT NULL |

### Channel config additions (`notification_channels.config`)
- `telegram`: `ack_buttons` (bool, default false), `ack_link` (bool, default false; URL button, used only when
  `ack_buttons` is false).
- `teams`: `ack_link` (bool, default true).
- `email`: `ack_link` (bool, default false).
Buttons/links are added only when `chat_ack` is ON (link: and `public_base_url` is set), for stages problem,
escalation and reminder.

### Ack token vectors (pure `chatack.tokens`, key = 32 bytes `0x01…0x20` in tests)
`encode(sensor_id=7, event_id=991, exp_min=29_400_000)` → `a1.` + 38 base64url chars (length 41); `decode` of it →
(7, 991, 29 400 000); one flipped character → `invalid`; `exp_min` < now → `expired`; prefix `a2.` → `invalid`;
length ≠ 41 → `invalid`.

## [3] Events additions
- `config_change`: `device_id` set, `sensor_id` = the device's `config_backup` sensor or NULL, `old_status` /
  `new_status` NULL, message `Configuration changed (+12 −3 lines), version 37` (en, built from fields by the UI).
- `new_device`: `device_id` = the switch, `sensor_id` = its `new_mac` sensor or NULL, message as architecture §
  13.4; summary rows `+N more new devices on …`.
- Timeline kinds `config` (from `config_change`) and `new_mac` (from `new_device`) read these via `ix (device_id,
  time DESC)`.

## [3] Settings keys
| Key | Type | Default | Limits / meaning |
|---|---|---|---|
| `feature_*_enabled` (8 new keys) | bool | `false` | § [3] Feature toggles |
| `config_backup_time` | `HH:MM` | `"02:47"` | local time |
| `config_backup_window_min` | int | 60 | 0–720 (0 = all at once, still capped by concurrency) |
| `config_backup_concurrency` | int | 4 | 1–16 |
| `config_backup_timeout_s` | int | 120 | 30–600 |
| `config_backup_mask_secrets` | bool | true | |
| `config_backup_max_versions` | int | 100 | 2–1000 |
| `config_backup_min_versions` | int | 10 | 1–100, ≤ max |
| `config_backup_retention_days` | int | 365 | 7–3650 |
| `config_backup_fail_threshold` | int | 2 | 1–10 |
| `config_change_alert` | bool | false | condition `config_changed` on changes |
| `config_change_clear_min` | int | 60 | 5–10080 |
| `flow_max_records_per_s` | int | 10000 | 1000–50000 |
| `flow_top_conversations` | int | 500 | 50–5000 |
| `flow_top_hosts` | int | 200 | 20–2000 |
| `flow_top_apps` | int | 100 | 10–1000 |
| `flow_raw_retention_hours` | int | 24 | 1–168 |
| `flow_minute_retention_hours` | int | 48 | 24–336 (must exceed the cagg start_offset 6 h; ≥ 24 enforced) |
| `flow_rollup_retention_days` | int | 30 | 7–400 |
| `flow_app_overrides` | object[] | [] | ≤ 200 `{protocol, port, name}` |
| `newdev_*` | | | § [3] New-device alerts |
| `kerberos` | object | above | |
| `oidc` | object | above | |
| `report_retention_days` | int | 400 | 30–3650 |
| `chat_ack_ttl_h` | int | 168 | 1–720 |
| `telegram_poll_timeout_s` | int | 50 | 10–50 |

Process env: `NETMON_NETFLOW_PORT` (2055), `NETMON_IPFIX_PORT` (4739), `NETMON_SFLOW_PORT` (6343),
`NETMON_FLOW_HOST` (`0.0.0.0`), `NETMON_OIDC_AUTHORITY` (tests).

## [3] Phase 3 migration (`0007`)
File `backend/netmon/migrations/versions/0007_phase3.py`, `revision = "0007"`, `down_revision = "0006"`. Plain
PostgreSQL DDL only (hypertables, continuous aggregates, hourly fallback tables and trigram indexes are created by
the bootstrap).

`upgrade()`:
1. `sensors_type_check` re-added with the 2b-3 types + `'config_backup','new_mac'`; `ux_sensors_event_type`
   recreated with the five passive types; `devices.event_sensor_optout` values check widened accordingly (API
   validation).
2. `events_kind_check` + `'config_change','new_device'`.
3. `users_source_check` + `'oidc'` (and `users_username_check` gains `source = 'oidc'`: 1–64 characters, no
   control characters or white space); `audit_log_source_check` + `'oidc'` (OIDC sign-ins).
4. `event_conditions` source check + `'backup_failed','host_key','config_changed','new_mac'`; `event_rules`
   action check + `'config_backup'` (and the table CHECK: `config_backup` rows have no key/clear fields); seed the
   3 built-in config-change rules (English names, `enabled = false`).
5. `groups.ssh_credentials jsonb NULL`; `devices.ssh_credentials jsonb NULL`, `config_backup text NOT NULL
   DEFAULT 'auto'` (+ check), `config_backup_driver varchar(64)`, `newdev_excluded_ports integer[] NOT NULL DEFAULT
   '{}'`.
6. `port_inventory_status.newdev_baselined_at timestamptz NULL`.
7. `CREATE TABLE ssh_host_keys`, `config_backup_status`, `config_versions` (+ indexes).
8. `CREATE TABLE flow_exporters`, `flow_if_1m`, `flow_app_1m`, `flow_host_1m`, `flow_conv_1m` (+ btree indexes).
9. `CREATE TABLE known_macs`, `mac_allowlist`.
10. `CREATE TABLE port_flap_episodes`.
11. `CREATE TABLE reports`, `report_runs`.
12. `CREATE TABLE kiosk_screens`.
13. `CREATE TABLE chat_identities`, `chat_link_codes`, `telegram_poll_state`.

`downgrade()` (reverse): `DROP MATERIALIZED VIEW IF EXISTS flow_if_1h, flow_app_1h, flow_host_1h CASCADE` and
`DROP TABLE IF EXISTS` the same names (whichever the bootstrap created); drop the tables of steps 13 → 7; drop the
columns of steps 6 and 5; `DELETE FROM event_rules WHERE action = 'config_backup'`, restore the action and
condition-source checks after `DELETE FROM event_conditions WHERE source IN (…new…)`; `UPDATE audit_log SET source = NULL WHERE source = 'oidc'` (with the
append-only trigger `audit_log_immutable` disabled for that statement), restore the audit source check; `DELETE FROM
users WHERE source = 'oidc'`, restore the user checks; `DELETE FROM events WHERE kind IN ('config_change','new_device')`,
restore the kind check; `DELETE FROM sensors WHERE type IN ('config_backup','new_mac')`, restore the type check
and `ux_sensors_event_type`. Settings rows stay (harmless).

# **[3b]** Phase 3b additions

Scope: the "Ask the network" assistant (Amazon Bedrock Converse with read-only tools) and 10 AI features. All
schema changes are in **migration `0009`** ([§ Phase 3b migration](#3b-phase-3b-migration-0009)). Everything is
additive. API shapes: [api.md § Phase 3b](api.md#3b-phase-3b-contract). Components and flows:
[architecture.md § 14](architecture.md#14-phase-3b-ask-the-network-assistant-and-ai-features-amazon-bedrock).

## [3b] Enum changes

| Name | Values |
|---|---|
| `feature` | … 19 values of 2b-3 + 3, **[3b]** `assistant`, `ai_explain_alert`, `ai_correlation`, `ai_alert_notes`, `ai_config_diff`, `ai_syslog_digest`, `ai_rule_drafts`, `ai_template_drafts`, `ai_knowledge`, `ai_chat_ask`, `ai_report_summary` (28 toggles in total; Teams and API tokens are not toggles) |
| **[3b]** `assistant_purpose` | `ask`, `explain`, `incident`, `alert_note`, `diff_explain`, `digest`, `rule_draft`, `template_draft`, `chat_ask`, `report_summary`, `test`, `title` (the neutral audit title of a question, light model) |
| **[3b]** `assistant_channel` | `web`, `telegram` |
| **[3b]** `conversation_kind` | `chat`, `explain`, `rule_draft`, `template_draft` |
| **[3b]** `message_role` | `user`, `assistant` |
| **[3b]** `message_status` | `running`, `complete`, `cancelled`, `error`, `limit_reached` |
| **[3b]** `artifact_kind` | `explain`, `incident_summary`, `alert_note`, `diff_explain`, `digest`, `rule_draft`, `template_draft`, `report_summary` |
| **[3b]** `artifact_status` | `ok`, `failed`, `skipped`, `running` |
| **[3b]** `incident_state` | `open`, `resolved`, `stale` |
| **[3b]** `walk_status` | `running`, `ok`, `partial`, `failed`, `cancelled` |
| **[3b]** `auth_mode` | `roles_anywhere`, `access_key`, `default_chain` |
| **[3b]** `proxy_mode` | `env`, `direct`, `custom` |

## [3b] Feature toggles

Keys `feature_<name>_enabled` (bool, **default `false`**, also on upgraded installs; no migration writes them).
`feature_assistant_enabled` and `feature_ai_knowledge_enabled` can be turned ON only through their configure-first
endpoints with a valid `test_id` (architecture § 14.2.6); `PUT /api/settings` setting them to `true` →
`422 assistant_test_required`; `false` is accepted there. `feature_assistant_enabled` ON requires
`feature_audit_enabled` ON, and `audit` cannot be turned OFF while `assistant` is ON (`422 feature_dependency`).

| Key | ON | OFF means exactly |
|---|---|---|
| `feature_assistant_enabled` | credentials provider, agent executor, `/assistant`, conversations, usage | running questions cancelled; credentials dropped from memory; `/api/assistant/*` 409 except `GET/PUT /api/assistant/settings`, `POST /api/assistant/test`, `POST|DELETE /api/assistant/cert`, `POST /api/assistant/models`, `GET /api/assistant/usage` (prepare, review cost); every AI feature idles; conversations, usage, artifacts kept; retention still runs |
| `feature_ai_explain_alert_enabled` | *Explain* buttons, `POST /api/assistant/explain` | endpoint 409, buttons hidden; stored explanations kept (readable in conversations) |
| `feature_ai_correlation_enabled` | correlation listener, incident API, WS `incident.update` | listener stopped (open incidents set `stale`); `/api/incidents*` 409; incidents kept |
| `feature_ai_alert_notes_enabled` | note worker, `alert_rules.ai_note` honoured | no note jobs (queue dropped); `ai_note` flags kept and ignored; alerts unchanged |
| `feature_ai_config_diff_enabled` | `POST /api/assistant/config-diff/explain`, risk flags in the diff view | endpoint 409; flags not shown; stored explanations kept |
| `feature_ai_syslog_digest_enabled` | digest scheduler, digest API, signature register updates | scheduler stopped; `/api/assistant/digests*` 409; digests and signatures kept |
| `feature_ai_rule_drafts_enabled` | `POST /api/assistant/rule-drafts` | 409; the rule forms hide *Describe* |
| `feature_ai_template_drafts_enabled` | walks and drafts | running walks cancelled; `/api/assistant/template-drafts*` 409; `POST /api/device-templates/user` stays available (normal admin feature) |
| `feature_ai_knowledge_enabled` | `search_knowledge` offered | tool not offered; `GET/PUT /api/assistant/knowledge` and `POST /api/assistant/knowledge/test` stay available |
| `feature_ai_chat_ask_enabled` | `/ask` handled by the phase-3 poller for channels with `ai_ask` | `/ask` answered "Asking is turned off" (only while the poller runs for `chat_ack`), no question started |
| `feature_ai_report_summary_enabled` | `reports.ai_summary` honoured | reports render without the summary (`ai_summary_status = "disabled"`) |

## [3b] Settings

### Assistant connection and data sharing (key `assistant`, object)
```json
{ "auth_mode": "roles_anywhere",                  // roles_anywhere | access_key | default_chain
  "region": "us-east-1",                          // required; [a-z]{2}(-gov)?-[a-z]+-\d; default us-east-1
                                                   //   (owner: us-east-1 + us. profile recommended;
                                                   //   il-central-1 + global. profile supported)
  "model_id": "",                                  // required to enable, 1–256 chars, [A-Za-z0-9.:/_-];
                                                   //   no default (never hard-coded); global. → warning
  "model_id_light": null,                          // optional, same syntax
  "endpoint_url": null,                            // null = https://bedrock-runtime.<region>.amazonaws.com
  "control_endpoint_url": null,                    // optional, for List models
  "proxy_mode": "env", "proxy_url": null,          // custom: http(s)://host:port, no userinfo
  "prompt_caching": true,
  "access_key_id": null,                           // AKIA/ASIA + 16 [A-Z0-9]
  "secret_access_key_enc": null,                   // AAD b"assistant.secret_access_key"; API: secret_access_key (write-only), _set, _unreadable
  "session_token_enc": null,                       // optional session token of temporary credentials (ASIA… ids, e.g.
                                                   //   IAM Identity Center / STS), ≤ 4096 base64 chars, AAD
                                                   //   b"assistant.session_token"; API: session_token (write-only, "" clears),
                                                   //   session_token_set, session_token_unreadable; sent as X-Amz-Security-Token
  "access_key_created_at": null,                   // optional date, reminder only
  "ra_region": null,                               // null = region
  "ra_endpoint_url": null,
  "ra_trust_anchor_arn": null,                     // arn:aws[a-z-]*:rolesanywhere:<region>:<12 digits>:trust-anchor/<uuid>
  "ra_profile_arn": null,                          // …:profile/<uuid>
  "ra_role_arn": null,                             // arn:aws[a-z-]*:iam::<12 digits>:role/<path/name>
  "ra_session_seconds": 3600,                      // 900–43200
  "ra_cert_pem": null, "ra_chain_pem": null,       // public; stored as uploaded (normalised PEM)
  "ra_key_enc": null,                              // PKCS#8 PEM, AAD b"assistant.ra_key"; never returned
  "ra_cert_uploaded_at": null,
  "ra_csr_pem": null, "ra_csr_subject": null,      // [3b owner] Generate CSR: pending CSR (public)
  "ra_csr_key_enc": null,                          // its key, PKCS#8 PEM, AAD b"assistant.ra_csr_key"; never returned
  "ra_csr_created_at": null,
  "last_test": null,                               // {"ok", "at", "by", "error_code"} of the last Test
  "send_config_excerpts": false,
  "redact_internal_ips": false,
  "internal_cidrs": [],                            // ≤ 64 extra CIDRs treated as internal
  "chat_redact_ips": true,
  "prices": { "currency": "USD", "input_per_mtok": null, "output_per_mtok": null,
              "cache_read_per_mtok": null, "cache_write_per_mtok": null },  // numbers ≥ 0, ≤ 1000
                                                   // currency label, default USD (the cost cap is in it);
                                                   // one price set for every model
  "chat_models": [] }                              // [3b owner] [{"id": <model id, MODEL syntax>, "label": str|null
                                                   //   (≤ 60)}], ≤ 8, unique ids; models an admin may pick in the
                                                   //   chat composer. Allowed list = model_id first (always, the
                                                   //   default) + these; [] = model_id + model_id_light (if set).
                                                   //   Not a connection field (no test_id). Telegram /ask and the
                                                   //   AI features keep model_id / model_id_light
```
Validation per auth mode when enabling/testing: `roles_anywhere` needs the three ARNs + a certificate and key
(uploaded or host files); `access_key` needs both key fields, and an `ASIA…` (temporary) key id also needs
`session_token_enc` (otherwise the Test fails at the credentials step with `aws_session_token_required`; a set
token adds the warning `temporary_credentials`); `default_chain` needs nothing else. ARNs' account
ids must be equal (warning, not error). A connection-field change while ON needs a `test_id`
(architecture § 14.2.6). **Connection fields** (hashed for `test_id`): `auth_mode`, `region`, `model_id`,
`model_id_light`, `endpoint_url`, `control_endpoint_url`, `proxy_mode`, `proxy_url`, `prompt_caching`,
`access_key_id`, SHA-256 of the decrypted secret key, SHA-256 of the decrypted session token, every `ra_*` field (cert/chain/key by SHA-256).

### Knowledge Base (key `assistant_kb`, object)
```json
{ "knowledge_base_id": "ABCDEFGHIJ",   // [0-9A-Z]{10}
  "region": null,                      // null = assistant.region
  "endpoint_url": null,                // null = https://bedrock-agent-runtime.<region>.amazonaws.com
  "results": 5,                        // 1–10
  "min_score": 0.0,                    // 0–1
  "description": "IT runbooks and network diagrams" }   // ≤ 300 chars, shown to the model
```
KB connection fields for its `test_id`: all of the above except `description`, plus the assistant connection hash.

### Scalar keys
| Key | Type | Default | Limits / meaning |
|---|---|---|---|
| `feature_*_enabled` (11 new keys) | bool | `false` | § [3b] Feature toggles |
| `assistant_daily_questions_per_user` | int | 50 | 1–1000 |
| `assistant_monthly_token_cap` | int | 5000000 | 100000–1000000000 (input + output + 10 % of cache reads + cache writes) |
| `assistant_monthly_cost_cap` | number \| null | null | ≥ 0, USD (the prices' currency); admin setting, effective only with prices; questions stop at it, background work at `assistant_background_stop_pct` |
| `assistant_background_stop_pct` | int | 80 | 10–100 |
| `assistant_max_model_calls` | int | 8 | 2–15 |
| `assistant_max_tool_calls` | int | 12 | 1–30 |
| `assistant_max_output_tokens` | int | 2000 | 256–8192 |
| `assistant_question_timeout_s` | int | 120 | 30–300 |
| `assistant_max_concurrent` | int | 4 | 1–16 |
| `assistant_context_budget_tokens` | int | 40000 | 8000–150000 |
| `assistant_retention_days` | int | 90 | 1–730 |
| `assistant_artifact_retention_days` | int | 180 | 7–730 |
| `correlation_window_min` | int | 5 | 1–30 |
| `correlation_min_alerts` | int | 3 | 2–50 |
| `correlation_attach_to_alert` | bool | false | |
| `correlation_daily_summaries` | int | 50 | 0–500 |
| `alert_note_timeout_s` | int | 45 | 10–120 |
| `alert_notes_daily_cap` | int | 100 | 0–5000 |
| `config_diff_auto` | bool | false | explain the latest change automatically when the diff view opens |
| `config_diff_daily_cap` | int | 50 | 0–500 |
| `digest_time` | `HH:MM` | `"07:17"` | local time |
| `digest_language` | `"en"` \| `"he"` \| null | null | null = `default_language` |
| `digest_email_to` | string[] | [] | ≤ 20 addresses |
| `digest_channel_ids` | int[] | [] | Teams/Telegram/email channel ids, ≤ 10 |
| `chat_ask_per_user_hour` | int | 10 | 1–60 |

## [3b] `assistant_conversations`
| Column | Type | Constraints |
|---|---|---|
| id | bigint | PK identity |
| user_id | integer | NOT NULL FK `users.id` ON DELETE CASCADE |
| kind | text | NOT NULL DEFAULT 'chat' CHECK in conversation_kind |
| channel | text | NOT NULL DEFAULT 'web' CHECK in assistant_channel |
| title | varchar(200) | NOT NULL — first question cut to 80 chars (rename ≤ 200) |
| language | varchar(2) | NOT NULL DEFAULT 'en' — of the last question |
| ref_sensor_id | integer | NULL FK `sensors.id` ON DELETE SET NULL — `explain` |
| ref_event_id | bigint | NULL — `explain` (no FK: events may be retention-deleted) |
| redaction_map | jsonb | NOT NULL DEFAULT '{}' — `{"IP-1": "10.0.0.9", …}` (never sent to the model) |
| known_refs | text[] | NOT NULL DEFAULT '{}' — refs issued by tools in this conversation (≤ 5000, oldest dropped) |
| message_count | integer | NOT NULL DEFAULT 0 |
| created_at, updated_at, last_message_at | timestamptz | NOT NULL DEFAULT now() |

Indexes: `(user_id, last_message_at DESC)`, `(last_message_at)` (retention), `(ref_sensor_id, ref_event_id) WHERE kind = 'explain'`.

## [3b] `assistant_messages`
| Column | Type | Constraints |
|---|---|---|
| id | bigint | PK identity |
| conversation_id | bigint | NOT NULL FK ON DELETE CASCADE |
| seq | integer | NOT NULL — 1, 2, … within the conversation; UNIQUE (conversation_id, seq) |
| role | text | NOT NULL CHECK in message_role |
| purpose | text | NOT NULL CHECK in assistant_purpose |
| content | text | NOT NULL DEFAULT '' — user: the question as typed (≤ 2000); assistant: final answer Markdown with validated `nm:` links, de-tokenised |
| notes | jsonb | NOT NULL DEFAULT '[]' — texts of intermediate rounds `[{"round": 1, "text": "…"}]` |
| sources | jsonb | NOT NULL DEFAULT '[]' — `[{"ref": "device/12", "label": "core-sw"}]`; KB sources `{"ref": "kb/1", "label": "…", "url": "…"}` |
| converse_turns | jsonb | NULL — assistant only: the Converse messages of this exchange as sent (toolUse + toolResult blocks, redacted, capped); ≤ 512 KiB |
| status | text | NOT NULL DEFAULT 'complete' CHECK in message_status |
| error_code | varchar(64) | NULL |
| notices | text[] | NOT NULL DEFAULT '{}' — `limit_reached`, `truncated_context`, `kb_unavailable`, `usage_estimated` (no Bedrock usage metadata: tokens estimated), … |
| stop_reason | varchar(32) | NULL — last Bedrock `stopReason` |
| model_id | varchar(256) | NULL |
| model_calls, tool_calls | integer | NOT NULL DEFAULT 0 |
| input_tokens, output_tokens, cache_read_tokens, cache_write_tokens | integer | NOT NULL DEFAULT 0 |
| cost_estimate | numeric(12,6) | NULL — null without prices |
| duration_ms | integer | NULL |
| feedback | smallint | NULL CHECK in (-1, 1) |
| feedback_comment | varchar(1000) | NULL |
| created_at | timestamptz | NOT NULL DEFAULT now() |
| finished_at | timestamptz | NULL |

Index: `(conversation_id, seq)` (unique). At startup, rows still `running` become `error` with `error_code =
'interrupted'`.

## [3b] `assistant_tool_calls`
| Column | Type | Constraints |
|---|---|---|
| id | bigint | PK identity |
| message_id | bigint | NOT NULL FK `assistant_messages.id` ON DELETE CASCADE |
| seq | integer | NOT NULL |
| round | smallint | NOT NULL — model call number |
| tool_use_id | varchar(64) | NOT NULL — Bedrock `toolUseId` |
| tool | varchar(64) | NOT NULL |
| arguments | jsonb | NOT NULL — as validated (placeholders translated back are **not** stored; the model's form is stored) |
| ok | boolean | NOT NULL |
| error_code | varchar(64) | NULL |
| rows | integer | NULL |
| result_bytes | integer | NOT NULL DEFAULT 0 |
| truncated | boolean | NOT NULL DEFAULT false |
| duration_ms | integer | NOT NULL |
| started_at | timestamptz | NOT NULL |

Index `(message_id, seq)`.

## [3b] `assistant_usage`
| Column | Type | Constraints |
|---|---|---|
| day | date | NOT NULL — local date (`settings.timezone`) |
| user_id | integer | NOT NULL — 0 = system (background purposes); no FK (survives user deletion) |
| purpose | text | NOT NULL CHECK in assistant_purpose |
| model_id | text | NOT NULL DEFAULT `''` — **[3b owner]** the model / inference profile of the counted calls (≤ 256 chars); `''` = no model call (the `questions` counter, errors before a call) |
| questions, model_calls, tool_calls, errors, throttles | integer | NOT NULL DEFAULT 0 |
| input_tokens, output_tokens, cache_read_tokens, cache_write_tokens | bigint | NOT NULL DEFAULT 0 |
| cost_estimate | numeric(14,6) | NOT NULL DEFAULT 0 — at the prices valid when counted |

PK `(day, user_id, purpose, model_id)`. Upsert (`ON CONFLICT … DO UPDATE SET x = x + EXCLUDED.x`) after every model call (tokens)
and at question start (`questions`). Monthly totals: `SUM` over the month's days. Retention 730 days.
**Cap arithmetic** (pure `usage.cap_tokens(row) = input + output + cache_write + ceil(cache_read / 10)`).

## [3b] `assistant_artifacts`
| Column | Type | Constraints |
|---|---|---|
| id | bigint | PK identity |
| kind | text | NOT NULL CHECK in artifact_kind |
| key | varchar(200) | NOT NULL — `explain:<sensor>:<event>`, `incident:<id>:<n>`, `note:<event_id>`, `diff:<device>:<from>:<to>`, `digest:<yyyy-mm-dd>`, `draft:<uuid>`, `report:<run_id>` |
| status | text | NOT NULL CHECK in artifact_status |
| skip_reason | varchar(64) | NULL — `cap`, `background_stop`, `timeout`, `queue_full`, `unsupported_channel`, `feature_off`, `unverified_numbers`, error codes |
| content | text | NOT NULL DEFAULT '' — Markdown / plain text |
| data | jsonb | NOT NULL DEFAULT '{}' — kind-specific (diff: `{"flags": [...], "risks": [...]}`; digest: `{"stats": …}`; drafts: `{"draft": …, "validation": …}`) |
| conversation_id | bigint | NULL FK ON DELETE SET NULL |
| created_by | integer | NULL (user id; null = system) |
| model_id | varchar(256) | NULL |
| input_tokens, output_tokens | integer | NOT NULL DEFAULT 0 |
| cost_estimate | numeric(12,6) | NULL |
| created_at | timestamptz | NOT NULL DEFAULT now() |

UNIQUE `(kind, key)` (a refresh replaces the row). Index `(created_at)` for retention (`assistant_artifact_retention_days`; digests 400 days).

## [3b] `assistant_incidents` and `assistant_incident_members`
`assistant_incidents`:

| Column | Type | Constraints |
|---|---|---|
| id | bigint | PK identity |
| state | text | NOT NULL CHECK in incident_state |
| opened_at | timestamptz | NOT NULL |
| last_change_at | timestamptz | NOT NULL |
| resolved_at | timestamptz | NULL |
| title | varchar(200) | NOT NULL — deterministic title (en; the UI builds he from `title_fields`) |
| title_fields | jsonb | NOT NULL — `{"root": "core-sw", "root_status": "down", "count": 14, "unit": "devices", "status": "unreachable"}` |
| root_device_id | integer | NULL FK `devices.id` ON DELETE SET NULL |
| root_candidates | integer[] | NOT NULL DEFAULT '{}' |
| ai_title | varchar(200) | NULL |
| ai_summary | text | NULL |
| ai_root_guess | jsonb | NULL — `{"device_id": 3, "confidence": "high"}` |
| ai_summaries | smallint | NOT NULL DEFAULT 0 — ≤ 5 |
| member_count | integer | NOT NULL DEFAULT 0 |
| note_sent | boolean | NOT NULL DEFAULT false — attach-to-alert done |

Indexes `(state, last_change_at DESC)`, `(opened_at DESC)`.

`assistant_incident_members`: `incident_id` bigint FK ON DELETE CASCADE, `event_id` bigint NOT NULL (the
`status_change` event), `sensor_id` integer NULL, `device_id` integer NULL, `status` text, `time` timestamptz,
`recovered_at` timestamptz NULL; PK `(incident_id, event_id)`; index `(sensor_id, time DESC)`. Retention with
artifacts (resolved incidents older than `assistant_artifact_retention_days`).

## [3b] `assistant_note_targets`
Where a problem notification was delivered, so the note can follow it (written by the dispatcher only for rules with
`ai_note` while `ai_alert_notes` is ON):

| Column | Type | Constraints |
|---|---|---|
| id | bigint | PK identity |
| event_id | bigint | NOT NULL |
| rule_id | integer | NULL FK `alert_rules.id` ON DELETE SET NULL |
| channel_id | integer | NOT NULL FK `notification_channels.id` ON DELETE CASCADE |
| channel_type | text | NOT NULL — `telegram`, `teams`, `email` |
| ref | jsonb | NOT NULL — telegram `{"chat_id": "…", "message_id": 123, "text": "<sent html>"}`; email `{"message_id": "<…@netmon>", "subject": "…", "to": [...]}`; teams `{}` |
| sent_at | timestamptz | NOT NULL |
| note_status | text | NULL — `sent`, `edited`, `replied`, `skipped:<reason>` |

Index `(event_id)`; rows older than 2 days deleted hourly.

## [3b] `assistant_syslog_signatures`
| Column | Type | Constraints |
|---|---|---|
| signature | char(16) | PK — hex of SHA-1 of the normalised text |
| sample | varchar(300) | NOT NULL — normalised text (placeholders) |
| first_seen, last_seen | date | NOT NULL |
| days_seen | integer | NOT NULL DEFAULT 1 |
| count | bigint | NOT NULL DEFAULT 0 |

Updated by the digest job; rows with `last_seen` older than 30 days deleted.

## [3b] `assistant_snmp_walks`
| Column | Type | Constraints |
|---|---|---|
| id | bigint | PK identity |
| device_id | integer | NOT NULL FK ON DELETE CASCADE |
| requested_by | integer | NULL |
| status | text | NOT NULL CHECK in walk_status |
| subtrees | text[] | NOT NULL |
| varbinds | jsonb | NOT NULL DEFAULT '[]' — `[["1.3.6.1.2.1.1.1.0", "OctetString", "…≤ 256 chars"]]`, ≤ 5000 |
| counts | jsonb | NOT NULL DEFAULT '{}' — per subtree |
| error | varchar(300) | NULL |
| started_at | timestamptz | NOT NULL |
| finished_at | timestamptz | NULL |

Retention 7 days.

## [3b] Column additions
- `alert_rules.ai_note boolean NOT NULL DEFAULT false`.
- `reports.ai_summary boolean NOT NULL DEFAULT false`; `report_runs.ai_summary_status text NULL` CHECK in
  (`ok`, `failed`, `skipped_cap`, `timeout`, `unverified_numbers`, `disabled`); `ReportData` (json in
  `report_runs.data`) gains `ai_summary: string | null`.
- `notification_channels.config`: Telegram gains `ai_ask: bool` (default false); email gains `ai_note_email: bool`
  (default true).
- `audit_log.source` CHECK gains `telegram` (for `assistant.question` from `/ask`).

## [3b] Pure vectors

### Signing vectors (`netmon.assistant.sigv4`)
1. **AWS `get-vanilla`:** `GET /`, headers `Host: example.amazonaws.com`, `X-Amz-Date: 20150830T123600Z`, empty body,
   key `AKIDEXAMPLE` / `wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY`, region `us-east-1`, service `service` → signature
   `5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31`.
2. **Bedrock Converse:** model id `eu.anthropic.claude-test-v1:0` → request path
   `/model/eu.anthropic.claude-test-v1%3A0/converse` (model id percent-encoded with no safe characters); canonical
   URI **`/model/eu.anthropic.claude-test-v1%253A0/converse`** (each segment encoded again); body
   `{"messages":[{"role":"user","content":[{"text":"ping"}]}],"inferenceConfig":{"maxTokens":5}}` (exact bytes,
   SHA-256 `6df0851f0b28f3a611972fc70e4b1125520ef1133a54940f39609fb54f780a69`); headers `Host:
   bedrock-runtime.eu-central-1.amazonaws.com`, `Content-Type: application/json`, `X-Amz-Date: 20261109T120000Z`,
   `X-Amz-Security-Token: FQoGZXIvYXdzEXAMPLETOKEN`; signed headers `content-type;host;x-amz-date;x-amz-security-token`;
   same key as vector 1, region `eu-central-1`, service **`bedrock`** → canonical request SHA-256
   `52ce1d34b2b4abf31938695167c6d8c532ce88217496117380e25836634cf708`, signature
   `2584eff908463e2cb9f972d9e432585ee8fb20adc50f7e26ffa5a24b2065a3e8`.
3. **Roles Anywhere CreateSession:** `POST /sessions`, body
   `{"durationSeconds":3600,"profileArn":"arn:aws:rolesanywhere:eu-central-1:111122223333:profile/p-1","roleArn":"arn:aws:iam::111122223333:role/netmon-bedrock","trustAnchorArn":"arn:aws:rolesanywhere:eu-central-1:111122223333:trust-anchor/ta-1"}`
   (SHA-256 `b156a6b2d763ba2b892b764f394878e1e0283bcd6625c4f5016b0d177b9996c2`), headers `Host:
   rolesanywhere.eu-central-1.amazonaws.com`, `Content-Type: application/json`, `X-Amz-Date: 20261109T120000Z`,
   `X-Amz-X509: MIIBTESTCERT` (placeholder) → signed headers `content-type;host;x-amz-date;x-amz-x509`, string to sign
   ```
   AWS4-X509-RSA-SHA256
   20261109T120000Z
   20261109/eu-central-1/rolesanywhere/aws4_request
   5fb1c33641630cafe7c989350f015c1e6226cfc92d1ccf977798482982629f68
   ```
   The test signs it with a generated RSA key (PKCS#1 v1.5, SHA-256) and verifies with the public key; ECDSA variant:
   algorithm `AWS4-X509-ECDSA-SHA256`, DER signature verified. `Credential=` uses the leaf serial in **decimal**
   (serial `0x1234` → `4660`). With a chain, `x-amz-x509-chain` is signed too (signed headers
   `content-type;host;x-amz-date;x-amz-x509;x-amz-x509-chain`).

### Event-stream vector (`netmon.assistant.eventstream`)
Hex of one message (159 bytes), headers `:event-type` = `contentBlockDelta`, `:content-type` = `application/json`,
`:message-type` = `event` (header value type 7 = string), payload
`{"contentBlockIndex":0,"delta":{"text":"Hi"},"p":"abcd"}`:
```
0000009f00000057c37babff0b3a6576656e742d74797065070011636f6e74656e74426c6f636b44656c74610d3a636f6e74656e742d74
7970650700106170706c69636174696f6e2f6a736f6e0d3a6d6573736167652d747970650700056576656e747b22636f6e74656e744
26c6f636b496e646578223a302c2264656c7461223a7b2274657874223a224869227d2c2270223a2261626364227df2ace5ff
```
(prelude total length 159, headers length 87, prelude CRC32 `c37babff`, message CRC32 `f2ace5ff`). Feeding it in
1-byte chunks yields exactly one message; flipping any prelude byte → `bad_prelude_crc`; flipping a payload byte →
`bad_message_crc`; total length > 1 MiB → `frame_too_large`. Exception frames: `:message-type` = `exception`,
`:exception-type` = `throttlingException`.

### Redaction vectors (`redact.apply`)
| Input | Output |
|---|---|
| ` snmp-agent community read cipher $c$3$abcd==` | ` snmp-agent community read cipher <secret removed>` |
| `password=Hunter2 user=jdoe` | `password=<secret removed> user=jdoe` |
| `username admin secret 9 $9$xyz` | `username admin secret <secret removed>` |
| `-----BEGIN RSA PRIVATE KEY-----…-----END RSA PRIVATE KEY-----` | `<pem removed>` |
| `Link down on Gi1/0/1 from 10.0.0.9` (redaction on) | `Link down on Gi1/0/1 from IP-1`; again `10.0.0.9` in the same conversation → `IP-1`; `10.0.0.10` → `IP-2` |
| `8.8.8.8 unreachable` (redaction on) | unchanged (public) |
| tool argument `{"ip": "IP-2"}` | translated to `{"ip": "10.0.0.10"}` before the tool runs; unknown placeholder → `invalid_arguments` |
| `abc\x1b[31mred\x00` | `abc[31mred` (control characters removed) |

### Language vectors (`detect_language`)
`"Which devices are down?"` → `en`; `"אילו מכשירים למטה?"` → `he`; `"מה קרה ל-core-sw אתמול?"` → `he` (Hebrew
letters 11 of 17 ≥ 30 %); `"10.0.0.1?"` → user's UI language.

### Correlation vectors (`correlate.cluster`, window 5 min, min 3)
Graph: `core` parent of `a`, `b`, `c` (2b-1 dependencies); `x` unrelated; topology link `core`–`dist`.
- `core` down 12:00:00, `a` `b` `c` unreachable 12:00:30 → one incident, root `core`, title `core down — 3 devices
  unreachable`.
- add `x` down 12:01 → not a member (no edge).
- `dist` down 12:03 → member (topology neighbour of `core`), title unchanged root.
- `a` warning 12:10 alone after the incident's last change 12:03 → not a member (window passed) and no new incident (1 < 3).
- two problems only (`core`, `a`) → no incident.

### Diff risk-flag vectors (`risk_flags`, data file `risk_rules.yaml`)
| Driver | Diff line | Flag |
|---|---|---|
| `hpe_comware` | `+ snmp-agent community write cipher <secret removed>` | `snmp_community_added` high |
| `hpe_comware` | `- ssh server acl 2001` | `mgmt_acl_removed` high |
| `cisco_ios` | `+ access-list 10 permit any` | `acl_permit_any` high |
| `hpe_arubaos_switch` | `- logging 10.0.0.50` | `syslog_host_removed` medium |
| `cisco_ios` | `+ username tmp privilege 15 secret <secret removed>` | `privileged_user_added` high |
| any | `+ ntp server 1.2.3.4` | `ntp_changed` info |
| any | `+ description uplink` | none |

### Digest vectors (`digest.signature`, `digest.compare`)
`"Interface GigabitEthernet1/0/12 changed state to down"` and `"Interface GigabitEthernet1/0/7 changed state to
down"` → same signature (normalised `interface gigabitethernet<n>/<n>/<n> changed state to down`);
`"Login failed for user 'bob' from 10.1.1.5"` → `login failed for user <str> from <ip>`. Compare: baseline mean 10,
day 35 → unusual (×3.5, +25); baseline 10, day 25 → not (×2.5); baseline 0, day 20 for severity ≤ 3 → unusual.

### Pricing vector (`pricing.estimate`)
Prices input 3, output 15, cache read empty (→ 0.3), cache write empty (→ 3.75) per million; usage input 6000, output
600, cache read 6000, cache write 0 → 0.018 + 0.009 + 0.0018 = **0.0288**. Cap tokens: 6000 + 600 + 0 + 600 = 7200.

### Context trim vectors (`context.trim`, budget 1000 estimated tokens)
Conversation of 6 exchanges each with a 1500-char tool result: step 1 stubs results of exchanges 1–5 (oldest first)
until under budget; still over → drops exchanges 2.. keeping 1 and the last 4; the current exchange is never dropped.

## [3b] Phase 3b migration (`0009`)
File `backend/netmon/migrations/versions/0009_phase3b.py`, `revision = "0009"`, `down_revision = "0008"`. Plain
PostgreSQL DDL only.

`upgrade()`:
1. `CREATE TABLE assistant_conversations`, `assistant_messages`, `assistant_tool_calls` (+ indexes).
2. `CREATE TABLE assistant_usage` (with `model_id`, PK `(day, user_id, purpose, model_id)`; **[3b owner]** edited
   in place while phase 3b is unreleased — a database that already ran the earlier `0008` needs, once:
   `ALTER TABLE assistant_usage ADD COLUMN IF NOT EXISTS model_id text NOT NULL DEFAULT ''; ALTER TABLE
   assistant_usage DROP CONSTRAINT assistant_usage_pkey; ALTER TABLE assistant_usage ADD PRIMARY KEY (day, user_id,
   purpose, model_id);`).
3. `CREATE TABLE assistant_artifacts`, `assistant_incidents`, `assistant_incident_members`.
4. `CREATE TABLE assistant_note_targets`, `assistant_syslog_signatures`, `assistant_snmp_walks`.
5. `ALTER TABLE alert_rules ADD ai_note boolean NOT NULL DEFAULT false`; `ALTER TABLE reports ADD ai_summary boolean
   NOT NULL DEFAULT false`; `ALTER TABLE report_runs ADD ai_summary_status text NULL` (+ check).
6. `audit_log_source_check` + `'telegram'`.

Implementation additions (compatible with the tables above): index `ix_assistant_snmp_walks_device (device_id,
started_at DESC)`; `assistant_incidents.ai_summaries` CHECK 0–5; `assistant_incident_members.status` / `time`
nullable as listed. Owner decision (3b): no `assistant_audit_question_text` key — the question text is never
written to the audit log (architecture § 14.7).

`downgrade()` (reverse): `UPDATE audit_log SET source = NULL WHERE source = 'telegram'` (with the append-only
trigger disabled for that statement, as in `0007`), restore the check; drop the columns of step 5; drop the tables of
steps 4 → 1. Settings rows stay (harmless).

---

# **[1.3]** Release 1.3 additions

Core-behaviour improvements (no feature toggle). API: [api.md § 1.3](api.md#13-release-13-contract). Behaviour:
[architecture.md § 14](architecture.md#14-release-13-sensor-impact-important-ports-interface-selection-connected-hosts-lag-degraded-irf).

## [1.3] Sensor impact
`sensors.impact` (`critical` default | `warning` | `none`) decides what a sensor contributes to its **device**
status (pure `netmon.services.status.impact_status`):

| sensor status | critical | warning | none |
|---|---|---|---|
| down | down | **warning** | **up** |
| unreachable | unreachable | **warning** | **up** |
| warning | warning | warning | **up** |
| up / unknown / paused | unchanged | unchanged | unchanged |

The device rollup ([§ Status rollup](#status-rollup-computed-on-read-not-stored)) then runs unchanged on these
contributions. Decision for "all sensors `none`": `paused` and `unknown` are passed through for every impact, so a
device whose sensors are all `none` is `paused` when they are all paused, `unknown` while one is unknown, else `up`
— never a problem status. The sensor's own status, its events, alerts, notifications, acknowledge and maintenance
are unchanged by impact. `status_counts` still count raw sensor statuses.

Every consumer goes through `Inventory` (`load_inventory` → `compute`): device and group status in the API, tree,
dashboard `device_counts`, kiosk, topology/ports neighbour status, the `device.status` / `group.status` WS
rollup. Not affected: the dependency cascade ("parent down" tests the parent's sensors, reachability, not the
rolled-up status) and report availability (computed from the ping / system sensor's own status history).

Defaults: every new sensor `critical`, except a new `snmp_interface` sensor without an explicit impact
(`POST /api/sensors`, template apply): important port → `critical`, other port → `warning`, device without port
inventory → `critical`. IRF and all other template sensors: `critical`.

**Upgrade backfill** (`netmon.services.impact.backfill`, spawned at startup, guarded by the internal settings key
`impact_backfill_version` = `"1"`): `snmp_interface` sensors with impact `critical` of devices that have
`switch_ports` rows → `warning` when their ifIndex is not important; important ports keep `critical`; devices
without port inventory are left as they are. Runs once (a failure is logged and retried at the next start).

## [1.3] Important ports
One definition, pure `netmon.ports.importance.port_reasons(rows, neighbors_of, threshold, trunk_min)`; reasons in
this order:

| reason | when |
|---|---|
| `uplink` | the 2b-2 rule (§ Uplinks: a switch/router/AP device neighbour, or `mac_count` > `uplink_mac_threshold`); a LAG uses its own and all members' neighbours; a member also inherits its LAG's uplink state |
| `lag` | `kind = 'lag'` or `lag_if_index` not null (stored LAG data, whether or not the `lag` feature is ON) |
| `trunk` | `mode = 'trunk'` and ≥ `trunk_min_tagged_vlans` tagged VLANs; a member without its own VLANs uses its LAG's (`member_vlans`). One tagged VLAN (hybrid access + voice VLAN) is not a trunk at the default 2 |

Used by: ports API (`important`, `important_reasons`), apply selection and `/apply-template/interfaces`, the
new-interface-sensor impact and the backfill, and the ingest `PortInfo` (link-down / flapping scope
`important`), where trunks count only while `event_linkdown_include_trunks` is true and ports with a
**critical** interface sensor are added.

## [1.3] Settings keys
| Key | Type | Default | Limits / meaning |
|---|---|---|---|
| `trunk_min_tagged_vlans` | int | 2 | 1–100: tagged VLANs a trunk needs |
| `event_linkdown_include_trunks` | bool | true | trunks are important for link-down / flapping alerts under scope `important` (scope `all` unchanged) |
| `impact_backfill_version` | str | "" | internal (not in the API): the impact backfill that has run |

A change of the first two reloads the ingest configuration and drops the cached important-port sets.

## [1.3] LAG degraded condition
`condition_source` gains **`lag_degraded`**: on the device's `trap` event sensor, `rule_id` NULL, `key` = the LAG
ifIndex, status `warning`, message `LAG <name> degraded (<up> of <n> members up)`, `expires_at` = last refresh
+ the built-in link-down rule's `clear_after_min` (1440). Raised while 0 < up < n; removed when all members are up
or all are down (fully down = the existing link-down alert). Active while `event_alerts` is ON and the built-in
`trap_link_down` rule is enabled and in scope for the device; disabling / editing that rule or turning
`event_alerts` OFF deletes these conditions. Members = physical `switch_ports` rows whose `lag_if_index` is a
`lag` row of the device; member state = stored `oper_status`, overridden by linkDown/linkUp traps until the next
successful port inventory run (`reconcile`). `rules.Rule` gains `builtin_key`.

## [1.3] Template options
| Entry | Key | Meaning |
|---|---|---|
| `sensor` | `expect_from_probe: {channel, bound}` (only when set) | needs `probe: true`; at apply time the probe's value of `channel` becomes the channel threshold `bound` (`warning_min` / `warning_max` / `down_min` / `down_max`). A probe without that value → skipped `probe_failed` "No value". |
| `snmp_table` | `when` (only when set; same shape as sensor `when`) | evaluated before the walk; a failing condition → one skip `not_applicable` `Condition not met: <oid>` |

## [1.3] IRF sensors (`hpe_comware` v4)
Before the `snmp_interfaces` entry; every IRF entry has `when [{oid: hh3cStackMemberNum.0, not_in: [0, 1]}]` (only
IRF stacks of ≥ 2 members), `confidence: low`, notes `verified: false` until checked on the owner's FF 5945
(`deploy/tools/irf_dump.py`). HH3C-STACK-MIB, `hh3cStack` = `1.3.6.1.4.1.25506.2.91`:

| # | Entry | OID | Details |
|---|---|---|---|
| 1 | `IRF members` (`snmp_custom` gauge, unit `#`, probe, interval 120) | hh3cStackMemberNum `…2.91.1.2.0` | `expect_from_probe {channel: value, bound: down_min}`: down when fewer members than at apply time (re-apply after adding a member) |
| 2 | `IRF role {label}` (`snmp_table`, enum, max 16, interval 120) | hh3cStackBoardRole `…2.91.3.1.1` (INDEX entPhysicalIndex), label entPhysicalName | slave(1) → up "standby", master(2) → up "master", loading(3) / other(4) → warning; default warning. A member that leaves loses its row → check fails → down |
| 3 | `IRF port {index}` (`snmp_table`, enum, max 16, interval 120) | hh3cStackPortStatus `…2.91.4.1.3` (INDEX memberID.portIndex) | up(1) → up, down(2) / silent(3) / disabled(4) → warning; rows `disabled` at apply time are not created (IRF port without links) |
| 4 | `IRF links up` (`snmp_walk_count`, probe, interval 120) | the same column, `count_values [1]` | channel `matched` `down_min 1`: every IRF port down → down |

Enumerations from H3C's HH3C-STACK-MIB reference (mibs.observium.org object pages); table layout from the
Observium MIB browser. Impact: `critical` (default).

## [1.3] Migration (`0008`)
File `backend/netmon/migrations/versions/0008_v13.py`, `revision = "0008"`, `down_revision = "0007"`. Plain DDL.

`upgrade()`: `ALTER TABLE sensors ADD COLUMN impact text NOT NULL DEFAULT 'critical'` + `sensors_impact_check`;
`event_conditions_source_check` re-added with `'lag_degraded'`.
`downgrade()`: `DELETE FROM event_conditions WHERE source = 'lag_degraded'`, restore the source check, drop the
impact check and column, delete the settings row `impact_backfill_version` (the backfill runs again after a
re-upgrade).
