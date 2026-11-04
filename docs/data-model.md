# NetworkMonitor — Data Model (Phase 1)

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
| `sensor_type` | `ping`, `snmp_system`, `snmp_cpu`, `snmp_memory`, `snmp_interface`, `http`, `tcp_port`, `dns` |
| `snmp_version` | `v1`, `v2c`, `v3` |
| `notification_type` | `email`, `webhook` |
| `event_kind` | `status_change`, `notification`, `system` |

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
| unit | varchar(16) | NOT NULL (copied from the definition) |
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
- **Secret fields** (`community`, `v3_auth_key`, `v3_priv_key`, and `settings.smtp.password`) are write-only in the API (see
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
