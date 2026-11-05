# NetworkMonitor — Data Model (Phase 1 + Phase 2a + Phase 2b-1)

Sections and rows marked **[2a]** belong to the Phase 2a contract (auto-discovery, device templates,
`snmp_custom` / `snmp_disk` sensors, Telegram channel). Everything else is the Phase 1 contract and is unchanged.
Phase 2a schema changes are all in **migration `0003`** (see [§ Phase 2a migration](#phase-2a-migration-0003)).
Sections marked **[2b-1]** (dependencies, `unreachable`, maintenance, acknowledge, escalation, fingerprinting,
HPE switch templates, topology) are in [§ Phase 2b-1](#2b-1-phase-2b-1-additions), migration `0004`.

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
`unknown` (2) > `up` (1) > `paused` (0).

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
  restart, which is acceptable for the MVP.
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
| `snmp_table` | `walk_oid`, `label_oid` (\| null), `label2_oid` (\| null), `percent_of_column` (\| null), `name` (en/he, with `{label}` and `{index}` placeholders), `config` (snmp_custom config without `oid`/`percent_of_oid`/`label`), `channels`, `max` | Walks `walk_oid`. For each row suffix `S` (sorted, first `max`): creates an `snmp_custom` sensor with `oid = walk_oid + "." + S`, `percent_of_oid = percent_of_column + "." + S` (when set), `label` = text of `label_oid.S` (+ `" (" + label2_oid.S + ")"` when label2 is set and non-empty), falling back to `S`. Name = `name` with `{label}`/`{index}` substituted, cut to 128 chars. |

### Idempotency ("apply" adds only what is missing)
A template entry is skipped with reason `exists` when the device already has a sensor with the same identity:

| type | identity |
|---|---|
| `ping`, `snmp_system`, `snmp_cpu`, `snmp_memory` | the type |
| `snmp_interface` | `config.if_index` |
| `snmp_disk` | `config.storage_descr` |
| `snmp_custom` | `config.oid` |
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
(`group_id` change): compute the effective parent of every device; following parents from any device must not
return to it. Otherwise `400 dependency_cycle`, `details: [{"device_ids": [3, 7, 3]}]` (the cycle path). The
check is in `netmon.services.dependencies.find_cycle(inventory) -> list[int] | None` (pure).

### Status evaluation with a dependency (`netmon.poller.processor.persist`)
After `evaluate()`: if the evaluated status is `down` (check failed **or** a down threshold) **and** the
device has an effective parent that is down → the sensor's status is **`unreachable`** instead, and
`status_message = "Unreachable (parent <parent device name> down): <original message>"`. The channels keep
their evaluated values/statuses. `warning`/`up` are never changed. The parent state is read in the same
transaction (one query; only on a failing check).

**Cascade** (`netmon.services.dependencies.on_status_change`, called from `events.published` for
`status_change` events): when a sensor of device P changes status, for every device C whose effective parent
is P (and whose dependency sensor is this sensor, or "whole device" mode):
- P **became down** → each sensor of C with status `down` becomes `unreachable` (via `set_sensor_status`-like
  code, writes a `status_change` event `down → unreachable` with the message above, keeps `last_check_at`).
- P **is no longer down** → each sensor of C with status `unreachable` is queued for an immediate re-check
  (`scheduler.run_soon(sensor_ids)`, random jitter 0–5 s each; does not move the regular schedule). The status
  stays `unreachable` until that check runs; then it becomes whatever the check says. A child that is
  genuinely down after the parent recovered goes `unreachable → down` and **alerts** like any new problem.
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
`zoneinfo` on `PUT /api/settings` and `POST /api/setup` (422). The setup wizard sends the browser zone
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
maintenance to the alert engine. API: `in_maintenance(device_id) -> bool`, `active_window_ids() -> list[int]`,
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
| problem_since | timestamptz | NOT NULL. Start of the current problem (or of the recovery for `up`). |
| due_at | timestamptz | NULL. When the problem (or recovery, for status `up`) notification is due. NULL = nothing pending. |
| primary_sent_at | timestamptz | NULL |
| escalation_due_at | timestamptz | NULL |
| escalated_at | timestamptz | NULL |
| next_reminder_at | timestamptz | NULL |
| reminders_sent | integer | NOT NULL DEFAULT 0 |
| maintenance_suppressed | boolean | NOT NULL DEFAULT false |
| notified_channel_ids | integer[] | NOT NULL DEFAULT '{}'. Every channel that successfully received a problem/escalation/reminder for this problem. |
| updated_at | timestamptz | NOT NULL DEFAULT now() |
| | | PK `(rule_id, sensor_id)`; index on `sensor_id` |

A rule R **matches** status S for a sensor when R is enabled, `R.sensor_id` is NULL or the sensor, and S ∈
`R.on_statuses`. **Still active** for a row = the sensor's current status ∈ {down, warning} ∩ `R.on_statuses`.

**On a `status_change` event E (sensor X, new status S, time t):**

| S | Action per rule |
|---|---|
| `down` / `warning` | For each rule R matching S: if there is no row, or the row status ∉ {down, warning} → **new problem**: upsert `status=S, event_id=E, problem_since=t, due_at=t+R.delay_s, primary_sent_at=NULL, escalation_due_at = t+R.escalation_after_s` (NULL when no escalation), `escalated_at=NULL, next_reminder_at=NULL, reminders_sent=0, maintenance_suppressed=false`, **keeping** `notified_channel_ids` (an unsent recovery). If the row is already `down`/`warning` → **change within the problem**: `status=S, event_id=E, due_at=t+R.delay_s` (Phase 1 behaviour: every down↔warning transition notifies), rest unchanged. Rows of rules that do **not** match S: `due_at=NULL` (a pending delayed notification is dropped, as in Phase 1). |
| `up` | For each row of X: `notified_channel_ids` empty → delete. Else if R is enabled and `up` ∈ `R.on_statuses` → `status=up, event_id=E, problem_since=t, due_at=t+R.delay_s`, escalation/reminder cleared. Else delete (no recovery wanted). |
| `unreachable` / `paused` / `unknown` | For each row: `notified_channel_ids` empty → delete; else `status=suspended`, `due_at`/`escalation_due_at`/`next_reminder_at` = NULL (a later `up` sends the recovery; a later `down` is a new problem). |

**Alert loop** (`AlertEngine` task): wakes at the earliest of `due_at`, `escalation_due_at`,
`next_reminder_at` (one query), on any `notify()` from the event path, on `rt.clock` changes, and at least every
30 s. It locks due rows (`FOR UPDATE SKIP LOCKED`) and, per row, using `now = rt.clock.now()`:

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

**Leaving maintenance** (from the tracker): for rows of the device's sensors with `maintenance_suppressed`:
still active and not acknowledged → `due_at = now` (sent once by step 1, payload `after_maintenance: true`),
`escalation_due_at = now + R.escalation_after_s` if R has escalation and `escalated_at IS NULL`;
`maintenance_suppressed=false` in every case.

**Acknowledge** sets `due_at`/`escalation_due_at`/`next_reminder_at` = NULL on the sensor's down/warning rows
(defensive; steps 1/3/4 also check). Unacknowledge re-arms nothing (escalation does not restart; reminders
resume: `next_reminder_at = now + repeat_every_s`).

**De-duplication:** one send per `(sensor_id, event_id, stage, channel_id)` within the process (bounded
in-memory set, as in Phase 1). A send failure writes a `notification` event; there is no retry.

**Restart:** rows are persisted; overdue rows are processed right after startup (validated against the current
state). The Phase 1 sentence "Pending timers are lost on restart" no longer applies.

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

**Model** (first match over T, then N; stored ≤ 64 chars, else null): `\b(TL-[A-Z]{2,5}\d{2,5}[A-Z]{0,3})\b`,
`\b(J[L]?\d{3,4}A)\b` (HPE part numbers), `OfficeConnect\s+(?:Switch\s+)?(\d{4}S?)`, `\b((?:DS|RS)\d{3,4}\+?)\b`,
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
| `filters` | `{"oid": str, "in": int[] \| null, "not_in": int[] \| null}[]` | `[]` | Each filter GETs `oid + "." + S` for the row suffix S; the row is kept only when the value (integer) is in `in` (when set) and not in `not_in` (when set). A missing/non-integer value drops the row. All filters must pass. |

`config` of an `snmp_table` entry may now contain every `snmp_custom` key except `oid`/`percent_of_oid`/`label`
(so also `value_map` / `value_map_default` / `interval_s` stays on the entry level as `interval_s: int | null`,
new, default null = settings default).

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
  **ok**: `missed_refreshes += 1`; `>= 3` → `stale = true`. Stale links are never deleted automatically.
- **Promotion:** a neighbour that now matches a device (rules above) has its links re-pointed to the device
  (duplicates merged, keeping the oldest `first_seen`), its layout row renamed when the device has none, and is
  deleted. Neighbours with no links left are deleted at the end.
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
7. `CREATE TABLE alert_state` (+ `ix_alert_state_sensor_id`); copy
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
