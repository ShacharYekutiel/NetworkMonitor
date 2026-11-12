# NetworkMonitor — API Contract (Phase 1 + Phase 2a + Phase 2b-1 + Phase 2b-2 + Phase 2b-3 + Phase 3 + Phase 3b)

Items marked **[2a]** are the Phase 2a contract (discovery, device templates, `snmp_custom`/`snmp_disk`,
Telegram). The Phase 2a endpoints are specified in [§ Phase 2a](#2a-phase-2a-endpoints).
Items marked **[2b-1]** are specified in [§ Phase 2b-1](#2b-1-phase-2b-1-contract).
Items marked **[2b-2]** are specified in [§ Phase 2b-2](#2b-2-phase-2b-2-contract).
Items marked **[2b-3]** are specified in [§ Phase 2b-3](#2b-3-phase-2b-3-contract).
Items marked **[3]** are specified in [§ Phase 3](#3-phase-3-contract).
Items marked **[3b]** are specified in [§ Phase 3b](#3b-phase-3b-contract).
Items marked **[1.3]** (release 1.3: sensor impact, important ports, interface selection on template apply,
connected hosts / CSV on ports, LAG degraded, IRF) are specified in [§ Release 1.3](#13-release-13-contract).
Items marked **[1.3.1]** (IRF ports important, named value maps) are specified in
[§ Release 1.3.1](#131-release-131-contract).

This file is the contract between the backend (`backend/netmon/schemas`) and the frontend
(`frontend/src/api/types.ts`). **Field names are exact.** For the table semantics see
[data-model.md](data-model.md).

## Conventions

- **Base path:** `/api`. The WebSocket is at `/ws`.
- **Bodies:** requests and responses are `application/json; charset=utf-8`. A mutating request with any other
  content type gets `415 unsupported_media_type`, except bodiless POSTs.
- **Timestamps:** ISO-8601 UTC with a `Z` suffix, e.g. `"2026-11-04T12:00:00Z"`. Request query timestamps are
  ISO-8601.
- **IDs:** integers. **Nulls:** a field is always present, and its value is `null` when there is no value.
  The API never omits a key.
- **Partial updates:** `PATCH` takes a subset of the create fields. `PUT` is used only for `/settings` (a
  partial merge, see below).
- **Auth roles:**
  - `public`: no auth needed.
  - `viewer`: any logged-in user.
  - `admin`: the admin role only. A viewer calling an admin endpoint gets `403 forbidden`.

### Pagination
Collection endpoints return an envelope:

```json
{ "items": [ ... ], "total": 137, "page": 1, "page_size": 100 }
```

Query parameters:

- `page`: ≥ 1, default 1.
- `page_size`: 1–1000, default 100.

Sort orders are fixed per endpoint and stated below.

### Error format
Every error response, including 404 on an unknown `/api` path, 405 and 422, has this shape:

```json
{ "error": { "code": "validation_error", "message": "Human readable (English)", "details": [ { "loc": ["body","interval_s"], "msg": "must be >= 10" } ] } }
```

`details` is `null` unless stated otherwise. The frontend translates by `code`, and shows `message` only as a
fallback.

| HTTP | code | When |
|---|---|---|
| 400 | `bad_request` | Malformed request |
| 400 | `group_cycle` | `parent_id` would create a cycle |
| 400 | `invalid_sensor_type` | The type is not supported, or the type changed on PATCH |
| 401 | `unauthenticated` | No cookie, or the token is invalid or expired |
| 401 | `invalid_credentials` | Login failed |
| 403 | `forbidden` | Role too low, or a cross-origin mutating request |
| 403 | `setup_required` | Any endpoint except `/api/setup/*`, `/api/health`, `/api/settings/public` and `/api/auth/*` while there are no users (this check runs before routing, so it also answers unknown `/api` paths) |
| 404 | `not_found` | |
| 409 | `conflict` | A unique name already exists (`details`: `[{"field":"name"}]`) |
| 409 | `setup_already_done` | `POST /api/setup` when users exist |
| 405 | `method_not_allowed` | The path exists but not with this method |
| 409 | `sensor_paused` | `POST /api/sensors/{id}/run` on a sensor that is paused (or whose device is disabled) |
| 409 | `last_admin` | You may not delete, demote or deactivate the last active admin, or yourself |
| 415 | `unsupported_media_type` | |
| 422 | `validation_error` | Pydantic validation. `details` is a list of `{loc, msg}`. |
| 429 | `rate_limited` | Login throttling: 10 failures per 5 minutes per (IP, username) |
| 502 | `snmp_error` | An SNMP enumeration or test against the device failed (`message` gives the reason) |
| 502 | `smtp_error` / `webhook_error` | A test notification failed |
| 502 | **[2a]** `telegram_error` | A Telegram test send failed. `message` = Telegram's `description` (e.g. `Bad Request: chat not found`), or the transport error. The bot token never appears in it. |
| 409 | **[2a]** `scan_running` | A discovery scan is already running (`details`: `[{"scan_id": 12}]`), or the action needs a finished scan |
| 409 | **[2a]** `scan_not_running` | Cancel on a scan that is not running |
| 422 | **[2a]** `too_many_hosts` | Discovery targets expand to more than 4096 hosts (`details`: `[{"count": 65534, "max": 4096}]`) |
| 500 | `internal_error` | |

### Secret fields
`snmp_credentials.community`, `snmp_credentials.v3_auth_key`, `snmp_credentials.v3_priv_key`,
`smtp.password`, **[2a]** telegram `config.bot_token`, and discovery `options.extra_communities` /
`options.extra_v3[].v3_auth_key` / `options.extra_v3[].v3_priv_key` are **write-only** (for every role, admins
included).

- **Responses:** the field is always `null`, and a sibling boolean reports whether a value is stored:
  - `community_set`
  - `v3_auth_key_set`
  - `v3_priv_key_set`
  - `password_set`
  - **[2a]** `bot_token_set`
  - **[2a]** discovery options: see [Scan options in the API](#scan-options-in-the-api).
- **Requests:**
  - omitted, or `null` → keep the stored value;
  - `""` → clear it;
  - any other string → replace it.
- The `*_set` keys are ignored on input.
- A new credentials object (create, or switching from inherit) without `community` gets the default
  `"public"`.
- **HTTP sensor headers:** `config.headers` of `http` sensors is returned with real values to admins only.
  Viewers get the header names with every value replaced by `"********"`.

## Auth flow

- **Mechanism:** a JWT (HS256, signed with `secret.key`) in an **httpOnly cookie** named `netmon_session`.
  - Cookie attributes: `HttpOnly; SameSite=Strict; Path=/; Secure` (when https, see `NETMON_COOKIE_SECURE`).
  - `Max-Age`: 43200 (12 h).
  - No bearer tokens in Phase 1. The frontend never sees the token, and its `fetch` calls use
    `credentials: "same-origin"`.
- **JWT claims:** `sub` (the user id, as a string), `role`, `tv` (`token_version`), `iat`, `exp`.
- **A token is valid only if all of these hold:**
  - the signature is OK and it has not expired;
  - the user exists and `is_active`;
  - `tv == users.token_version`.

  The role is always re-read from the DB.
- **Sliding renewal:** when less than 6 h of validity remain, any authenticated response re-sets the cookie with
  a fresh 12 h token.
- **CSRF:** SameSite=Strict, plus the JSON-only content type. On top of that, for `POST/PUT/PATCH/DELETE`, if an
  `Origin` header is present and its host:port differs from the request `Host` (or `X-Forwarded-Host` when the
  proxy is trusted), the API returns `403 forbidden`.
- **WebSocket:** the browser sends the same cookie on `/ws`.
- **SPA boot sequence:**
  1. `GET /api/setup/status`. If `setup_required` → wizard.
  2. Otherwise `GET /api/auth/me`. A 401 → the login page.
  3. Otherwise the app.

  On any 401 later on, the SPA redirects to login.

## Common object shapes

```ts
type Status = "up" | "warning" | "down" | "paused" | "unknown";
type StatusCounts = { up: number; warning: number; down: number; paused: number; unknown: number; total: number };

type User = { id: number; username: string; email: string | null; role: "admin" | "viewer";
  language: "en" | "he"; is_active: boolean; last_login_at: string | null; created_at: string };

type SnmpCredentials = { version: "v1" | "v2c" | "v3"; port: number; timeout_s: number; retries: number;
  community: null; community_set: boolean;               // write-only (see Secret fields)
  v3_username: string | null;
  v3_security_level: "noAuthNoPriv" | "authNoPriv" | "authPriv" | null;
  v3_auth_protocol: "MD5" | "SHA" | "SHA224" | "SHA256" | "SHA384" | "SHA512" | null;
  v3_auth_key: null; v3_auth_key_set: boolean;            // write-only (see Secret fields)
  v3_priv_protocol: "DES" | "AES128" | "AES192" | "AES256" | null;
  v3_priv_key: null; v3_priv_key_set: boolean };

type Group = { id: number; parent_id: number | null; name: string; description: string;
  snmp_credentials: SnmpCredentials | null;               // null = inherit
  sort_order: number; status: Status; status_counts: StatusCounts;  // recursive sensor counts
  created_at: string; updated_at: string };

type Device = { id: number; group_id: number; name: string; host: string; description: string;
  enabled: boolean; snmp_credentials: SnmpCredentials | null;      // null = inherit
  effective_snmp_source: "device" | "group" | "default";
  effective_snmp_group_id: number | null;                 // set when source = "group"
  sort_order: number; status: Status; status_counts: StatusCounts;
  created_at: string; updated_at: string };

type Channel = { key: string; unit: string; warning_min: number | null; warning_max: number | null;
  down_min: number | null; down_max: number | null; last_value: number | null;
  status: "up" | "warning" | "down" | "unknown" };

type Sensor = { id: number; device_id: number; device_name: string; group_id: number;
  name: string; type: SensorType; config: Record<string, unknown>; interval_s: number;
  enabled: boolean; status: Status; status_message: string;
  last_check_at: string | null; last_status_change_at: string | null;
  primary_channel: string; channels: Channel[]; sort_order: number;
  created_at: string; updated_at: string };

type SensorType = "ping" | "snmp_system" | "snmp_cpu" | "snmp_memory" | "snmp_interface" | "http" | "tcp_port" | "dns"
  | "snmp_custom" | "snmp_disk";                         // [2a]

type Event = { id: number; time: string; kind: "status_change" | "notification" | "system" | "discovery"; // [2a] discovery
  sensor_id: number | null; device_id: number | null; group_id: number | null;
  sensor_name: string | null; device_name: string | null;
  old_status: Status | null; new_status: Status | null; message: string };

type CheckResult = { ok: boolean;          // false = the check failed (timeout, refused, ...)
  status: Status; message: string; duration_ms: number; checked_at: string;
  channels: { key: string; unit: string; value: number | null; status: "up" | "warning" | "down" | "unknown" }[] };
```

`channels` are always ordered as in the type definition ([data-model.md](data-model.md)). The frontend
translates channel names with the i18n key `channel.<key>`.

---

## Endpoints

### Health

**`GET /api/health`** (public). Always 200 while the process is alive. `status` is `"degraded"` when the DB is
unreachable.

```json
{ "status": "ok", "version": "0.1.0", "database": "ok", "timescale": true, "poller": "running",
  "sensors_scheduled": 42, "uptime_s": 3600 }
```

- `database`: `"ok"` | `"error"`.
- `poller`: `"running"` | `"stopped"`.
- **[2b-1]** `test_mode`: `true` only when `NETMON_TEST_MODE=1` (the test clock is enabled), else `false`.

### Setup

**`GET /api/setup/status`** (public) → `200 {"setup_required": true}`.

**`POST /api/setup`** (public, only when there are no users, else `409 setup_already_done`)

Request:

```json
{ "username": "admin", "password": "S3cret-pass", "email": null, "language": "he" }
```

- `password`: 8–128 characters.
- `language` (optional, default `"en"`): it also becomes `settings.default_language`.

The endpoint creates the admin, the group `{"name":"Default","parent_id":null}` and the default settings, then
sets the session cookie. Response `201`: `{"user": User}`.

### Auth

| Method & path | Role | Request | Response |
|---|---|---|---|
| `POST /api/auth/login` | public | `{"username":"admin","password":"..."}` | `200 {"user": User}` + Set-Cookie. Errors: `401 invalid_credentials`, `429 rate_limited`. |
| `POST /api/auth/logout` | viewer | — | `204`. Clears the cookie (Max-Age=0). |
| `GET /api/auth/me` | viewer | — | `200 {"user": User}` |
| `PATCH /api/auth/me` | viewer | `{"email": "...", "language": "he"}` (any subset) | `200 {"user": User}` |
| `POST /api/auth/change-password` | viewer | `{"current_password":"...","new_password":"..."}` | `204`. Increments `token_version` and re-issues the cookie. Wrong current password: `401 invalid_credentials`. |

### Users (admin)

| Method & path | Request | Response |
|---|---|---|
| `GET /api/users` | — | Page of `User`, sorted by `username` |
| `POST /api/users` | `{"username","password","email"?,"role","language"?}` | `201 User` |
| `GET /api/users/{id}` | — | `User` |
| `PATCH /api/users/{id}` | subset of `{"email","role","language","is_active","password"}` | `User`. A role/active/password change increments `token_version`. |
| `DELETE /api/users/{id}` | — | `204`. `409 last_admin` when deleting yourself or the last admin. |

### Tree (viewer)

**`GET /api/tree`**: the full hierarchy for the side tree, in a single call. It is not paginated.

- Groups, devices and sensors are each sorted by `sort_order, name`.
- `primary_value` is the `last_value` of the primary channel.

```json
{ "groups": [
  { "id": 1, "name": "Default", "parent_id": null, "status": "down",
    "status_counts": {"up":5,"warning":0,"down":1,"paused":0,"unknown":0,"total":6},
    "children": [ /* same shape, recursive */ ],
    "devices": [
      { "id": 3, "name": "core-sw", "host": "10.0.0.2", "enabled": true, "status": "down",
        "status_counts": {"up":2,"warning":0,"down":1,"paused":0,"unknown":0,"total":3},
        "sensors": [
          { "id": 7, "name": "Ping", "type": "ping", "enabled": true, "status": "up",
            "status_message": "", "primary_channel": "latency_ms", "primary_value": 1.8, "primary_unit": "ms",
            "last_check_at": "2026-11-04T12:00:00Z" } ] } ] } ] }
```

The top-level `groups` holds the root groups (`parent_id = null`).

### Groups

| Method & path | Role | Request | Response |
|---|---|---|---|
| `GET /api/groups` | viewer | Query: `parent_id` (int, or `root`). Omitted = all groups. | Page of `Group`, sorted by `sort_order, name` |
| `POST /api/groups` | admin | `{"name":"Branch TLV","parent_id":1,"description":"","snmp_credentials":null,"sort_order":0}` (only `name` is required) | `201 Group` |
| `GET /api/groups/{id}` | viewer | — | `Group` |
| `PATCH /api/groups/{id}` | admin | Subset of the create fields. `"snmp_credentials": null` = inherit. | `Group`. Can return `400 group_cycle`. |
| `DELETE /api/groups/{id}` | admin | — | `204`. **Cascades** to child groups, devices and sensors. The UI must confirm first. |

### Devices

| Method & path | Role | Request | Response |
|---|---|---|---|
| `GET /api/devices` | viewer | Query: `group_id`, `status`, `q` (an ILIKE match on name or host) | Page of `Device`, sorted by `name` |
| `POST /api/devices` | admin | See below | `201 Device` |
| `GET /api/devices/{id}` | viewer | — | `Device` |
| `PATCH /api/devices/{id}` | admin | Subset of the create fields. `"enabled": false` pauses all of its sensors. | `Device` |
| `DELETE /api/devices/{id}` | admin | — | `204` (cascades to its sensors) |
| `GET /api/devices/{id}/snmp/interfaces` | admin | — | See below. A timeout or auth failure → `502 snmp_error`. |
| **[1.3]** `GET /api/devices/{id}/ports.csv` | viewer | — | Ports × connected MACs as CSV ([§ 1.3](#13-release-13-contract)) |
| **[1.3]** `GET /api/devices/{id}/apply-template/interfaces` | admin | Query `template_id` | Interface choices of the apply dialog ([§ 1.3](#13-release-13-contract)) |

Create request:

```json
{ "group_id": 1, "name": "core-sw", "host": "10.0.0.2", "description": "", "enabled": true,
  "snmp_credentials": { "version": "v2c", "port": 161, "timeout_s": 2, "retries": 1, "community": "private",
    "v3_username": null, "v3_security_level": null, "v3_auth_protocol": null, "v3_auth_key": null,
    "v3_priv_protocol": null, "v3_priv_key": null },
  "sort_order": 0,
  "auto_sensors": ["ping"] }
```

- `snmp_credentials`: `null` = inherit. When an object is given, every field is optional, and the defaults are
  the ones in [data-model.md](data-model.md).
- `auto_sensors` (optional, create only): sensor types to create right away with default config. Only types
  that need no required config are allowed: `ping`, `snmp_system`, `snmp_cpu`, `snmp_memory`.

**SNMP interface enumeration** walks `ifTable`/`ifXTable` using the effective credentials. Response:

```json
{ "items": [
  { "if_index": 1, "name": "Gi0/1", "descr": "GigabitEthernet0/1", "alias": "uplink", "type": 6,
    "speed_bps": 1000000000, "admin_status": "up", "oper_status": "up", "mac": "00:11:22:33:44:55",
    "existing_sensor_id": null } ] }
```

- `admin_status` and `oper_status` are IF-MIB names: `up`, `down`, `testing`, `unknown`, `dormant`,
  `notPresent`, `lowerLayerDown`.
- `existing_sensor_id` is the id of an `snmp_interface` sensor that already exists on this device with that
  `if_index`, or null.
- To add interfaces, the frontend issues one `POST /api/sensors` per selected interface, with
  `type: "snmp_interface"` and `config: {"if_index": n, "if_name": name}`.

### Sensor types (viewer)

**`GET /api/sensor-types`**: static definitions, the same as [data-model.md](data-model.md).

```json
{ "items": [
  { "type": "ping", "requires_snmp": false, "primary_channel": "latency_ms",
    "config_fields": [
      { "key": "count", "type": "int", "required": false, "default": 4, "min": 1, "max": 20, "options": null },
      { "key": "timeout_ms", "type": "int", "required": false, "default": 1000, "min": 100, "max": 10000, "options": null },
      { "key": "packet_size", "type": "int", "required": false, "default": 56, "min": 16, "max": 1472, "options": null } ],
    "channels": [
      { "key": "latency_ms", "unit": "ms", "warning_min": null, "warning_max": 100, "down_min": null, "down_max": 500 },
      { "key": "packet_loss_pct", "unit": "%", "warning_min": null, "warning_max": 10, "down_min": null, "down_max": 50 } ] } ] }
```

- `config_fields[].type` is one of `int`, `str`, `bool`, `enum` (with `options: [..]`), `int_list`,
  `str_nullable`, `int_nullable`, `headers` (an object of string to string), **[2a]** `float` (`min`/`max` may
  be fractional).
- **[2a]** The list also contains `snmp_custom` and `snmp_disk` ([data-model.md](data-model.md)). For
  `snmp_custom` the listed channel `value` has `unit: ""`; the real unit is the sensor's `config.unit`, and
  `Sensor.channels[].unit` already carries it.
- The frontend translates field labels with the i18n key `sensorConfig.<type>.<key>`.

### Sensors

| Method & path | Role | Request | Response |
|---|---|---|---|
| `GET /api/sensors` | viewer | Query: `device_id`, `group_id` (recursive), `status`, `type`, `q` (name) | Page of `Sensor`, sorted by `device_name, sort_order, name`. A `status` filter returns the "worst first" order: severity desc, then `last_status_change_at` asc. |
| `POST /api/sensors` | admin | See below | `201 Sensor` |
| `GET /api/sensors/{id}` | viewer | — | `Sensor` |
| `PATCH /api/sensors/{id}` | admin | Subset of `{name, config, interval_s, enabled, sort_order, channels}` (**[1.3]** + `impact`). `type` cannot change. `config` is merged over the stored config. | `Sensor` |
| **[1.3]** `POST /api/sensors/bulk-impact` | admin | `{"sensor_ids": [7, 8], "impact": "warning"}` | `{"updated": n}` ([§ 1.3](#13-release-13-contract)) |
| `DELETE /api/sensors/{id}` | admin | — | `204` |
| `POST /api/sensors/{id}/run` | admin | — (no body) | `200 CheckResult`. Runs the check now and persists it (measurements, status, events, WS) like a scheduled run. Does not move the schedule. A paused sensor (or disabled device) → `409 sensor_paused`. |
| `POST /api/sensors/test` | admin | `{"device_id":3,"type":"http","config":{...}}`. You may send `"host"` instead of `device_id`; a test against an unsaved device then uses `"snmp_credentials"` from the body. | `200 CheckResult`. Persists **nothing**. |
| `GET /api/sensors/{id}/history` | viewer | Query: `range` = `1h`\|`24h`\|`7d`\|`30d` (default `24h`), **or** `from` + `to` (ISO). Optional `channels=latency_ms,packet_loss_pct`. | See below |

Create request:

```json
{ "device_id": 3, "name": "Web", "type": "http",
  "config": { "url": "https://{host}/", "keyword": "Welcome" },
  "interval_s": 60, "enabled": true, "sort_order": 0,
  "channels": [ { "key": "response_time_ms", "warning_max": 800, "down_max": 3000 } ] }
```

- `interval_s` defaults to `settings.default_interval_s`.
- `name` defaults to the localized type name, sent by the frontend. If it is missing (and for `auto_sensors`),
  the backend uses an English default: `Ping`, `System`, `CPU Load`, `Memory`, `HTTP`, `DNS`, `TCP <port>`, and
  `"<if_name>"` (or `Interface`) for interfaces.

**Channels in requests** hold threshold overrides only:

- Each entry is `{key, warning_min?, warning_max?, down_min?, down_max?}`.
- An unknown key → `422`.
- A threshold key that is present (including `null`) overwrites the stored value. A key that is absent keeps it.

**History**

- The bucket size is chosen from the span:

  | span ≤ | bucket |
  |---|---|
  | 1 h | 60 s |
  | 24 h | 300 s |
  | 7 d | 1800 s |
  | 30 d | 7200 s |
  | longer | `span / 400`, rounded up to a whole minute |

- Buckets with no data are **omitted**. The frontend draws gaps where `t` jumps by more than 2 × `bucket_s`.

```json
{ "sensor_id": 7, "from": "2026-11-03T12:00:00Z", "to": "2026-11-04T12:00:00Z", "bucket_s": 300,
  "channels": [
    { "key": "latency_ms", "unit": "ms",
      "points": [ { "t": "2026-11-03T12:00:00Z", "avg": 1.9, "min": 1.2, "max": 4.1 } ] } ] }
```

`t` is the bucket start.

**Sensor events:** `GET /api/sensors/{id}/events` returns the same as `/api/events?sensor_id={id}`.

### Events (viewer)

**`GET /api/events`**

- Query parameters:
  - `sensor_id`, `device_id`, `group_id` (recursive);
  - `kind`;
  - `status` (matches `new_status`);
  - `since`, `until` (ISO).
- Response: a page of `Event`, sorted by `time DESC, id DESC`.

### Dashboard (viewer)

**`GET /api/dashboard`**

```json
{ "sensor_counts": {"up":40,"warning":1,"down":2,"paused":3,"unknown":0,"total":46},
  "device_counts": {"up":10,"warning":1,"down":1,"paused":0,"unknown":0,"total":12},
  "group_count": 4,
  "recent_events": [ /* Event × up to 20, kind=status_change, newest first */ ],
  "problem_sensors": [ /* Sensor × up to 20, status down then warning, longest-in-state first */ ] }
```

### Notification channels (admin)

| Method & path | Request | Response |
|---|---|---|
| `GET /api/notification-channels` | — | Page of `NotificationChannel`, sorted by name |
| `POST /api/notification-channels` | `{"name":"Ops mail","type":"email","enabled":true,"config":{"to":["ops@example.com"]}}` | `201 NotificationChannel` |
| `GET /api/notification-channels/{id}` | — | `NotificationChannel` |
| `PATCH /api/notification-channels/{id}` | Subset (`type` cannot change). `config` is replaced as a whole. | `NotificationChannel` |
| `DELETE /api/notification-channels/{id}` | — | `204` |
| `POST /api/notification-channels/{id}/test` | — | `200 {"ok": true, "message": "sent"}`. On failure: `502 smtp_error` / `webhook_error` / **[2a]** `telegram_error`. |

The `NotificationChannel` shape:

```json
{ "id": 1, "name": "Ops hook", "type": "webhook", "enabled": true,
  "config": { "url": "https://hooks.example.com/x", "headers": {}, "verify_tls": true, "timeout_s": 10 },
  "created_at": "...", "updated_at": "..." }
```

#### Webhook payload
The alert engine sends a `POST` with `Content-Type: application/json` and `User-Agent: NetworkMonitor/<version>`.
A 2xx response counts as success. Any other response, or a timeout, writes a `notification` event, with no retry
in Phase 1.

```json
{ "event": "sensor.status_changed", "event_id": 991, "time": "2026-11-04T12:00:00Z",
  "old_status": "up", "new_status": "down", "message": "Host unreachable",
  "sensor": { "id": 7, "name": "Ping", "type": "ping" },
  "device": { "id": 3, "name": "core-sw", "host": "10.0.0.2" },
  "group":  { "id": 1, "name": "Default" },
  "rule":   { "id": 2, "name": "All down" },
  "url": "http://netmon.local:8080/sensors/7" }
```

- `url` is null when `public_base_url` is unset.
- A test send uses `"event": "test"`, with `old_status`, `new_status` and `rule` set to null and `sensor` /
  `device` / `group` set to null.

**Email:** the subject is `[NetworkMonitor] DOWN: core-sw / Ping`, in `settings.default_language`. The email has
a plain-text body and an HTML body with the same fields as the webhook payload.

### Alert rules (admin)

| Method & path | Request | Response |
|---|---|---|
| `GET /api/alert-rules` | Query: `sensor_id` (int, or `global`) | Page of `AlertRule`, sorted by name |
| `POST /api/alert-rules` | `{"name":"All down","sensor_id":null,"on_statuses":["down","up"],"delay_s":120,"enabled":true,"channel_ids":[1,2]}` | `201 AlertRule` |
| `GET /api/alert-rules/{id}` | — | `AlertRule` |
| `PATCH /api/alert-rules/{id}` | Subset. `channel_ids` replaces the whole set. | `AlertRule` |
| `DELETE /api/alert-rules/{id}` | — | `204` |

The `AlertRule` shape:

```json
{ "id": 2, "name": "All down", "sensor_id": null, "sensor_name": null, "on_statuses": ["down","up"],
  "delay_s": 120, "enabled": true, "channel_ids": [1,2], "created_at": "...", "updated_at": "..." }
```

`channel_ids` must be non-empty, and each id must exist (otherwise `422`).

### Settings

| Method & path | Role | Request | Response |
|---|---|---|---|
| `GET /api/settings` | admin | — | `Settings` |
| `PUT /api/settings` | admin | Any subset of the top-level keys. `smtp` is merged field by field, and the secret rules apply to `password`. | `Settings`. A change to `retention_days` re-applies the Timescale retention policy. |
| `POST /api/settings/smtp/test` | admin | `{"to": "me@example.com"}` | `200 {"ok": true, "message": "sent"}`, or `502 smtp_error` |
| `GET /api/settings/public` | public | — | `{"default_language": "he"}`, used by the login and setup pages |

The `Settings` shape:

```json
{ "smtp": { "host": "smtp.example.com", "port": 587, "security": "starttls", "username": "netmon",
            "password": null, "password_set": true, "from_address": "netmon@example.com", "from_name": "NetworkMonitor" },
  "retention_days": 90, "event_retention_days": 365, "default_interval_s": 60,
  "default_language": "en", "public_base_url": null }
```

---

## WebSocket `/ws`

- **Connecting:** the client connects to `ws(s)://<host>/ws`, and the cookie authenticates it.
  - With no valid session, the server accepts and then closes immediately with code **4401**.
  - During setup, it closes with **4403**.
  - An open socket is closed with **4401** when its user's session is revoked (deactivated, deleted, role or
    password changed by an admin). After the user's own password change (the browser already holds the new
    cookie), it is closed with **4000**: the client just reconnects.
  - If an `Origin` header is present and does not match `Host` (same rule as the CSRF check), it closes
    with **1008**.
- **Who receives what:** every authenticated client receives every message (no subscriptions in Phase 1). Both
  viewers and admins receive messages. **[2a] Exception:** `discovery.*` messages go only to sockets whose user
  had role `admin` at connect time (a role change closes the socket with 4401, so this stays correct).
- **Frames:** text frames carrying JSON `{"type": string, "ts": ISO, "data": object}`.

**Server → client messages:**

| type | data | When |
|---|---|---|
| `hello` | `{"user": User, "version": "0.1.0", "timescale": true}` | Right after connect |
| `sensor.update` | `{"id":7,"device_id":3,"status":"up","status_message":"","last_check_at":"...","status_changed":false,"channels":[{"key":"latency_ms","value":1.8,"status":"up"}]}` | After every check, scheduled or run-now |
| `event.created` | `Event` | After an event is written |
| `device.status` | `{"id":3,"status":"down","status_counts":StatusCounts}` | The device's rollup status or counts changed |
| `group.status` | `{"id":1,"status":"down","status_counts":StatusCounts}` | Sent for every ancestor group whose rollup changed |
| `config.changed` | `{"entity":"group"\|"device"\|"sensor"\|"alert_rule"\|"notification_channel"\|"discovery_profile","id":7,"action":"created"\|"updated"\|"deleted"}` | After any successful mutation through the API. The client should invalidate its queries (e.g. `/api/tree`). **[2a]** adds `discovery_profile`. |
| **[2a]** `discovery.progress` | see [Discovery WS messages](#discovery-websocket-messages) | **Admins only** |
| **[2a]** `discovery.result` | see below | **Admins only** |
| **[2a]** `discovery.finished` | see below | **Admins only** |
| `pong` | `{}` | Reply to the client's `ping` |

**Client → server:** `{"type":"ping"}`. Anything else is ignored.

**Keepalive and reconnect:**

- The server sends WS ping frames every 30 s.
- On close the client reconnects with backoff (1 s, 2 s, 5 s, then 10 s max), except after 4401, which sends it
  to login.
- After a reconnect the client refetches `/api/tree` and `/api/dashboard`.

---

## [2a] Phase 2a endpoints

All shapes follow the Phase 1 conventions (always-present keys, `null` for no value, error envelope, pagination
envelope where stated). New TypeScript types:

```ts
type DeviceClass = "router" | "switch" | "access_point" | "printer" | "nas" | "server" | "workstation" | "unknown";
type ScanStatus = "running" | "completed" | "cancelled" | "failed";
type L10n = { en: string; he: string };

type ScanOptions = {
  ping: boolean; ping_timeout_ms: number;
  snmp: boolean; snmp_port: number | null; snmp_timeout_s: number;
  extra_communities: null; extra_communities_count: number;        // write-only list
  extra_v3: ExtraV3Out[]; extra_v3_count: number;
  tcp: boolean; tcp_ports: number[]; tcp_timeout_ms: number;
  dns: boolean };
type ExtraV3Out = { v3_username: string;
  v3_security_level: "noAuthNoPriv" | "authNoPriv" | "authPriv";
  v3_auth_protocol: "MD5" | "SHA" | "SHA224" | "SHA256" | "SHA384" | "SHA512" | null;
  v3_auth_key: null; v3_auth_key_set: boolean;
  v3_priv_protocol: "DES" | "AES128" | "AES192" | "AES256" | null;
  v3_priv_key: null; v3_priv_key_set: boolean };

type DiscoveryProfile = { id: number; name: string; targets: string[]; group_id: number; options: ScanOptions;
  schedule_enabled: boolean; schedule_every_hours: number;
  notify_new_hosts: boolean; notify_channel_ids: number[];
  last_run_at: string | null; next_run_at: string | null; last_scan_id: number | null;
  created_at: string; updated_at: string };

type DiscoveryScan = { id: number; profile_id: number | null; profile_name: string | null;
  trigger: "manual" | "scheduled"; status: ScanStatus;
  group_id: number | null; targets: string[]; options: ScanOptions;   // secrets removed
  total_hosts: number; scanned_hosts: number; found_hosts: number; new_hosts: number;
  error: string | null; created_by: string | null;                  // username
  started_at: string; finished_at: string | null };

type DiscoveryResult = { id: number; scan_id: number; ip: string; hostname: string | null; mac: string | null;
  ping_ok: boolean; ping_rtt_ms: number | null;
  snmp_ok: boolean; snmp_version: "v1" | "v2c" | "v3" | null;
  snmp_credential: "group" | "extra" | null;                       // which credential answered
  sys_name: string | null; sys_descr: string | null; sys_object_id: string | null;
  open_ports: number[]; device_class: DeviceClass; vendor: string | null;
  suggested_template: string; existing_device_id: number | null; is_new: boolean;
  imported_device_id: number | null };

type DeviceTemplate = { id: string; name: L10n; description: L10n; requires_snmp: boolean; priority: number;
  match: { device_classes: DeviceClass[] | null; vendors: string[] | null; requires_snmp: boolean;
           any_open_ports: number[] | null }[];
  entries: TemplateEntry[] };
type TemplateEntry =
  | { kind: "sensor"; type: SensorType; name: L10n; config: Record<string, unknown>;
      channels: ChannelOverride[]; interval_s: number | null; probe: boolean }
  | { kind: "snmp_interfaces"; oper_up_only: boolean; exclude_if_types: number[];
      include_name_regex: string | null; exclude_name_regex: string | null; max: number; channels: ChannelOverride[] }
  | { kind: "snmp_table"; walk_oid: string; label_oid: string | null; label2_oid: string | null;
      percent_of_column: string | null; name: L10n; config: Record<string, unknown>;
      channels: ChannelOverride[]; max: number };
type ChannelOverride = { key: string; warning_min?: number | null; warning_max?: number | null;
  down_min?: number | null; down_max?: number | null };

type ApplyTemplateResult = { device_id: number; template_id: string;
  created: { sensor_id: number; name: string; type: SensorType }[];
  skipped: { name: string; type: SensorType;
             reason: "exists" | "probe_failed" | "snmp_error" | "max_reached" | "no_rows"; message: string }[] };

type TelegramChannelConfig = { bot_token: null; bot_token_set: boolean; chat_id: string;
  parse_mode: "HTML" | null; disable_notification: boolean };
```

`NotificationChannel.type` becomes `"email" | "webhook" | "telegram"`.

### Sensors: new types

`POST /api/sensors` examples:

```json
{ "device_id": 3, "name": "Clients HomeWiFi (na)", "type": "snmp_custom",
  "config": { "oid": "1.3.6.1.4.1.41112.1.6.1.2.1.8.2", "value_type": "gauge", "unit": "clients",
              "multiplier": 1, "label": "HomeWiFi (na)", "percent_of_oid": null, "expected": null, "match": "equals" },
  "channels": [ { "key": "value", "warning_max": 40 } ] }
```

```json
{ "device_id": 5, "type": "snmp_disk", "config": { "storage_descr": "/" } }
```

- Default names when `name` is missing: `snmp_custom` → `label`, else `SNMP <oid>` (cut to 128);
  `snmp_disk` → `Disk <storage_descr>`.
- `POST /api/sensors/test` works for both types (it is what the form's "Test" button uses).

**`GET /api/devices/{id}/snmp/storage`** (admin): walks `hrStorageTable` with the effective credentials, for
the `snmp_disk` picker. Timeout/auth failure → `502 snmp_error`.

```json
{ "items": [
  { "index": 31, "descr": "/", "type": "fixed_disk", "size_bytes": 115964116992, "used_bytes": 6512238592,
    "existing_sensor_id": null } ] }
```

- `type`: `ram` (.2), `virtual_memory` (.3), `fixed_disk` (.4), `removable_disk` (.5), `network_disk` (.10),
  `flash` (.9), `other` (anything else). Sorted by `index`. The UI preselects `fixed_disk` rows and hides
  `ram`/`virtual_memory`.
- `size_bytes` / `used_bytes`: `number | null` — null when the agent does not report `hrStorageSize` /
  `hrStorageUsed` / `hrStorageAllocationUnits` for the row. The UI shows "—" and no usage bar then.
- `existing_sensor_id`: an `snmp_disk` sensor of this device whose `config.storage_descr` equals `descr`, or null.

### Notification channels: `telegram`

Create:

```json
{ "name": "Ops Telegram", "type": "telegram", "enabled": true,
  "config": { "bot_token": "123456789:AAEhBP0av28rmzBYNkKz3x2ZxM9qO9vLdIo", "chat_id": "-1001234567890",
              "parse_mode": "HTML", "disable_notification": false } }
```

Response (`NotificationChannel`):

```json
{ "id": 4, "name": "Ops Telegram", "type": "telegram", "enabled": true,
  "config": { "bot_token": null, "bot_token_set": true, "chat_id": "-1001234567890",
              "parse_mode": "HTML", "disable_notification": false },
  "created_at": "...", "updated_at": "..." }
```

- Create without `bot_token` (or with `""`) → `422` (loc `["body","config","bot_token"]`).
- **PATCH:** for `telegram`, `config` is still replaced as a whole **except `bot_token`**, which follows the
  secret rules: omitted or `null` → keep, a new string → replace. `""` → 422 (a telegram channel always needs a
  token). `bot_token_set` on input is ignored.
- `chat_id` must be a string (a JSON number → 422, so negative ids never lose precision).

#### Telegram messages [2a]
- Request: `POST {NETMON_TELEGRAM_API_BASE}/bot{bot_token}/sendMessage` with JSON
  `{"chat_id": "...", "text": "...", "parse_mode": "HTML", "disable_web_page_preview": true, "disable_notification": false}`
  (`parse_mode` key omitted when null). Timeout 10 s. `NETMON_TELEGRAM_API_BASE` defaults to
  `https://api.telegram.org`.
- Success: HTTP 200 and body `{"ok": true, ...}`.
- Failure → `TelegramError(message)`:
  - Telegram answered `{"ok": false, "error_code": 400, "description": "Bad Request: chat not found"}` (any
    status) → message = `description` (e.g. `Unauthorized`, `Forbidden: bot was blocked by the user`,
    `Too Many Requests: retry after 5`).
  - Non-JSON response → `Telegram returned HTTP <status>`.
  - Timeout → `Telegram request timed out`. Other transport errors → their text.
  - Every message is passed through a scrubber that replaces the bot token with `***` (httpx errors include
    the URL). The `httpx`/`httpcore` loggers must not log request URLs at INFO (set them to WARNING), so the
    token never reaches the log.
- Test endpoint: `502 telegram_error` with that message. Alert sends: a `notification` event as for other channels.
- **Text** (language = `settings.default_language`, same labels as the email templates):
  - `parse_mode = "HTML"`: first line `<b>{subject}</b>`, then one line per row `<b>{label}:</b> {value}`
    (values HTML-escaped with `html.escape(v, quote=False)`), then `<a href="{url}">{link label}</a>` when
    `url` is set. Test: `<b>[NetworkMonitor] Test notification</b>` + the test body line.
  - `parse_mode = null`: `{subject}\n\n` + `text_body(...)` of the email template.
  - Truncated to 4096 characters (Telegram's limit) on a line boundary, with `…` appended.

### Discovery notification payload [2a]
When a scheduled scan of a profile with `notify_new_hosts = true` finishes (`completed`), has `new_hosts > 0`
and is not the profile's baseline run, the backend sends one notification per channel in
`notify_channel_ids` (enabled channels only). Failures write a `notification` event.
The `discovery` event itself is written for **every** completed, non-baseline scheduled scan with
`new_hosts > 0`, also when `notify_new_hosts = false`; only the notifications are opt-in. Webhook body:

```json
{ "event": "discovery.new_hosts", "event_id": 1203, "time": "2026-11-04T03:00:12Z",
  "old_status": null, "new_status": null,
  "message": "Discovery \"LAN\": 2 new hosts",
  "sensor": null, "device": null, "group": { "id": 1, "name": "Default" }, "rule": null,
  "profile": { "id": 2, "name": "LAN" }, "scan_id": 57,
  "hosts": [ { "ip": "10.0.0.5", "hostname": "brn3c2af4.lan", "device_class": "printer", "vendor": "brother" },
             { "ip": "10.0.0.9", "hostname": null, "device_class": "unknown", "vendor": null } ],
  "url": "http://netmon.local:8080/discovery/scans/57" }
```

- `event_id` is the id of the `discovery` event written for this scan. `hosts` lists at most 50 hosts (by IP);
  `message` always has the full count. `url` is null without `public_base_url`.
- For `status_change` payloads the new keys `profile`, `scan_id`, `hosts` are **not** added (Phase 1 payload
  unchanged).
- Email/Telegram subject: en `[NetworkMonitor] New hosts discovered: LAN (2)`, he
  `[NetworkMonitor] התגלו מארחים חדשים: LAN (2)`. Body: one line per host
  `10.0.0.5 — brn3c2af4.lan — printer — brother` (missing parts omitted), then `+N more` / `ועוד N` when
  truncated, then the link. Class names in he: router נתב/חומת אש, switch מתג, access_point נקודת גישה,
  printer מדפסת, nas NAS, server שרת, workstation תחנת עבודה, unknown לא ידוע.

### Device templates [2a]

| Method & path | Role | Response |
|---|---|---|
| `GET /api/device-templates` | viewer | `{"items": DeviceTemplate[]}`, sorted by `priority` desc. Not paginated. |
| `GET /api/device-templates/{id}` | viewer | `DeviceTemplate`. Unknown id → `404 not_found`. |
| `POST /api/devices/{id}/apply-template` | admin | Body `{"template_id": "opnsense"}`. `200 ApplyTemplateResult`. Unknown template → `422` (loc `["body","template_id"]`). |

Example item:

```json
{ "id": "unifi_ap", "name": { "en": "UniFi access point", "he": "נקודת גישה UniFi" },
  "description": { "en": "Ping, system, CPU, memory, uplink port and clients per SSID/radio",
                   "he": "פינג, מערכת, מעבד, זיכרון, פורט Uplink ולקוחות לכל SSID/רדיו" },
  "requires_snmp": true, "priority": 80,
  "match": [ { "device_classes": ["access_point"], "vendors": ["ubiquiti"], "requires_snmp": true, "any_open_ports": null } ],
  "entries": [
    { "kind": "sensor", "type": "ping", "name": { "en": "Ping", "he": "פינג" }, "config": {}, "channels": [],
      "interval_s": null, "probe": false },
    { "kind": "snmp_interfaces", "oper_up_only": true, "exclude_if_types": [24], "include_name_regex": "^eth0$",
      "exclude_name_regex": null, "max": 1, "channels": [] },
    { "kind": "snmp_table", "walk_oid": "1.3.6.1.4.1.41112.1.6.1.2.1.8", "label_oid": "1.3.6.1.4.1.41112.1.6.1.2.1.6",
      "label2_oid": "1.3.6.1.4.1.41112.1.6.1.2.1.9", "percent_of_column": null,
      "name": { "en": "Clients {label}", "he": "לקוחות {label}" },
      "config": { "value_type": "gauge", "unit": "clients" }, "channels": [], "max": 16 } ] }
```

Apply semantics ([data-model.md § Device templates](data-model.md#device-templates-built-in-code)):

- Entries are processed in order. Probes run with the device's effective SNMP credentials, through the
  poller semaphore, up to 4 at a time per device. The whole call has a 120 s budget; entries not reached in
  time are skipped with reason `probe_failed`, message `Timed out`.
- Created sensors get `interval_s` = the entry's value or `settings.default_interval_s`, are scheduled
  (`scheduler.upsert`), and use names in the caller's language.
- Works on disabled devices too (sensors are created, and stay `paused`); probes are still attempted.
- **Serialized per device:** apply-template and discovery import take a per-device lock, so two concurrent
  applies on one device never create duplicates (the later one reports `exists` for what the first created).
- One `config.changed {"entity":"device","id":<id>,"action":"updated"}` is broadcast (no per-sensor messages).
- Example response:

```json
{ "device_id": 9, "template_id": "opnsense",
  "created": [ { "sensor_id": 101, "name": "CPU Load", "type": "snmp_cpu" },
               { "sensor_id": 102, "name": "igb0", "type": "snmp_interface" } ],
  "skipped": [ { "name": "Ping", "type": "ping", "reason": "exists", "message": "" },
               { "name": "Interfaces", "type": "snmp_interface", "reason": "max_reached", "message": "3 more interfaces not added (max 16)" } ] }
```

### Discovery [2a] (all endpoints: admin)

#### Scan options in the API
- Responses (`ScanOptions`): `extra_communities` is always `null` with `extra_communities_count`;
  each `extra_v3` entry has its keys `null` with `*_set` booleans; `extra_v3_count` = its length.
- Requests: every option key is optional (defaults in [data-model.md § Scan options](data-model.md#scan-options)).
  `extra_communities`: a list of strings. `extra_v3`: a list of objects with the real keys.
  `extra_communities_count`, `extra_v3_count` and `*_set` are ignored on input.
- **Profile PATCH** of `options` is a field-by-field merge over the stored options. For the two secret lists:
  key omitted or `null` → keep the stored list; a list (also `[]`) → replace the whole list (v3 keys must be
  given again).

#### Profiles

| Method & path | Request | Response |
|---|---|---|
| `GET /api/discovery/profiles` | — | Page of `DiscoveryProfile`, sorted by name |
| `POST /api/discovery/profiles` | see below | `201 DiscoveryProfile`. Duplicate name → `409 conflict`. |
| `GET /api/discovery/profiles/{id}` | — | `DiscoveryProfile` |
| `PATCH /api/discovery/profiles/{id}` | subset of the create fields | `DiscoveryProfile` |
| `DELETE /api/discovery/profiles/{id}` | — | `204`. Its scans stay (`profile_id` → null). A running scan of it keeps running. |
| `POST /api/discovery/profiles/{id}/run` | — (no body) | `202 DiscoveryScan` (`trigger: "manual"`). `409 scan_running` if any scan runs. |

Create request:

```json
{ "name": "LAN", "targets": ["192.168.1.0/24", "192.168.10.1-30"], "group_id": 1,
  "options": { "snmp_port": null, "extra_communities": ["monitor"], "tcp_ports": [22, 80, 443, 9100] },
  "schedule_enabled": true, "schedule_every_hours": 24,
  "notify_new_hosts": true, "notify_channel_ids": [4] }
```

- Only `name`, `targets`, `group_id` are required. `group_id` must exist (422).
- Targets are validated and the host count checked (`422 validation_error` / `422 too_many_hosts`) on create
  and on PATCH of `targets`.
- `notify_channel_ids`: each id must exist (422). `notify_new_hosts: true` with an empty list is allowed
  (no notifications are sent). The `discovery` event does not depend on these fields: it is written for every
  completed, non-baseline scheduled scan with new hosts (see below).
- Profile mutations broadcast `config.changed` with `entity: "discovery_profile"`.

#### Scans

| Method & path | Request | Response |
|---|---|---|
| `POST /api/discovery/scans` | `{"targets": [...], "group_id": 1, "options": {...}}` (`options` optional) | `202 DiscoveryScan` with `status: "running"`, `profile_id: null`. `409 scan_running` (details `[{"scan_id": n}]`), `422 too_many_hosts`, `422 validation_error`. |
| `GET /api/discovery/scans` | Query: `profile_id` | Page of `DiscoveryScan`, sorted by `id` desc |
| `GET /api/discovery/scans/{id}` | — | `DiscoveryScan` (use it for polling, e.g. every 2 s, when the WS is down) |
| `POST /api/discovery/scans/{id}/cancel` | — (no body) | `200 DiscoveryScan` with `status: "cancelled"` (and `finished_at` set), always within **5 s**: if in-flight probes have not stopped by then, the server records the scan as `cancelled` anyway and lets the task wind down in the background (results it still writes stay). `409 scan_not_running` otherwise. |
| `DELETE /api/discovery/scans/{id}` | — | `204` (results cascade). `409 scan_running` while it runs. |
| `GET /api/discovery/scans/{id}/results` | Query: `only_new` (bool), `hide_existing` (bool), `device_class` (csv), `q` (case-insensitive **literal** substring of ip text / hostname / sys_name: `%`, `_` and `\` in `q` are escaped, so they match themselves) | Page of `DiscoveryResult`, sorted by `ip` ascending (inet order). Allowed while running (partial). |
| `POST /api/discovery/scans/{id}/import` | see below | `200` import result. `409 scan_running` while the scan runs. |

Scan example response:

```json
{ "id": 57, "profile_id": null, "profile_name": null, "trigger": "manual", "status": "running",
  "group_id": 1, "targets": ["192.168.1.0/24"],
  "options": { "ping": true, "ping_timeout_ms": 1000, "snmp": true, "snmp_port": null, "snmp_timeout_s": 1,
               "extra_communities": null, "extra_communities_count": 1, "extra_v3": [], "extra_v3_count": 0,
               "tcp": true, "tcp_ports": [22, 23, 53, 80, 443, 445, 631, 3389, 8080, 8443, 9100],
               "tcp_timeout_ms": 1000, "dns": true },
  "total_hosts": 254, "scanned_hosts": 31, "found_hosts": 6, "new_hosts": 2,
  "error": null, "created_by": "admin", "started_at": "2026-11-04T12:00:00Z", "finished_at": null }
```

Result example:

```json
{ "id": 812, "scan_id": 57, "ip": "192.168.1.1", "hostname": "OPNsense.localdomain", "mac": null,
  "ping_ok": true, "ping_rtt_ms": 0.42, "snmp_ok": true, "snmp_version": "v2c", "snmp_credential": "group",
  "sys_name": "OPNsense.localdomain",
  "sys_descr": "FreeBSD OPNsense.localdomain 14.1-RELEASE-p5 FreeBSD 14.1-RELEASE-p5 stable/24.7-n267758-... amd64",
  "sys_object_id": "1.3.6.1.4.1.8072.3.2.8", "open_ports": [22, 53, 80, 443],
  "device_class": "router", "vendor": "opnsense", "suggested_template": "opnsense",
  "existing_device_id": null, "is_new": true, "imported_device_id": null }
```

`snmp_credential`: `"group"` when credential index 0 answered, `"extra"` for any extra community / v3 entry,
null when SNMP did not answer.

#### Import

Request:

```json
{ "group_id": 1,
  "items": [ { "result_id": 812, "template_id": "opnsense", "name": null },
             { "result_id": 815, "template_id": "unifi_ap", "name": "AP Office" } ] }
```

- `group_id` optional → the scan's `group_id` (422 when both are null or the group does not exist).
- `items`: 1–256 entries, unique `result_id`. `template_id` must be a known template (whole request → 422).
  `name` optional (1–128 chars).
- **Time budget:** the whole import has a **90 s** budget. Each item's template apply gets
  `min(120 s, time left)`; items not started before the budget runs out are not attempted and return
  `errors[]` entries with code `internal_error`, message `Import time budget exceeded; retry the remaining hosts`
  (`device_id: null`). The response is still `200`.
- For each item (processed with concurrency 8; the order of `created`/`errors` follows `items`):
  - The result must belong to this scan, else item error `not_found`.
  - If `existing_device_id` (computed now) is not null → item error `device_exists` (with `device_id`).
  - Create the device: `host` = the IP text, `name` = `name` or `sys_name` or `hostname` or the IP (trimmed,
    cut to 128), `description` = `sys_descr` (first 500 chars) or `""`, `enabled: true`.
  - **SNMP credentials** (when SNMP did not answer → `null`). Let `C` = the stored credential that answered
    (`discovery_scans.credentials[index]`) and `G` = the **import group's** current effective credentials
    (group chain → built-in default). If `C` is index 0 **and** `C` equals `G` comparing every field except
    `timeout_s` and `retries` (**`port` included**, as are version, community and all v3 fields) →
    `snmp_credentials = null` (inherit). Otherwise the device gets its own copy of `C` with `timeout_s = 2`,
    `retries = 1`. So importing into a different group than the scan's, or after the group's credentials
    changed, or with an `snmp_port` override, gives the device its own (working) credentials.
  - Apply the template (same code as apply-template; ping is never probed).
  - Set `discovery_results.imported_device_id`.
  - Broadcast one `config.changed` `{"entity":"device","action":"created"}` per device.

Response `200`:

```json
{ "created": [ { "result_id": 812, "device_id": 21, "device_name": "OPNsense.localdomain", "template_id": "opnsense",
                 "sensors_created": 9,
                 "sensors_skipped": [ { "name": "Disk /volume1", "type": "snmp_disk", "reason": "probe_failed",
                                        "message": "Storage '/volume1' not found" } ] } ],
  "errors": [ { "result_id": 815, "code": "device_exists", "message": "A device with this host already exists",
                "device_id": 4 } ] }
```

`errors[].code` ∈ `not_found`, `device_exists`, `internal_error`; `device_id` is null except for `device_exists`.
For an unexpected failure the `message` is the fixed text `Internal error` (details only in the server log);
the only other `internal_error` message is the time-budget one above.

**Client recovery** (the request may outlive a reverse proxy's timeout, e.g. a `504` after 60 s, while the
server keeps importing): on a `5xx`/network error from `POST .../import`, the client does **not** retry blindly.
It refetches `GET /api/discovery/scans/{id}/results` (and the tree): rows with `imported_device_id` set were
imported (show them as done, linking to the device); rows still without it and without `existing_device_id`
stay selected so the user can import them again. Re-importing an already-imported row is harmless — it
returns `device_exists`.

#### Discovery WebSocket messages
Sent to admin sockets only.

| type | data | When |
|---|---|---|
| `discovery.progress` | `{"scan_id":57,"status":"running","total_hosts":254,"scanned_hosts":31,"found_hosts":6,"new_hosts":2}` | Right after a scan starts, then at most once per second while counters change |
| `discovery.result` | `{"scan_id":57,"result": DiscoveryResult}` | Each time a host is found (row written) |
| `discovery.finished` | `{"scan_id":57,"status":"completed"\|"cancelled"\|"failed","total_hosts":254,"scanned_hosts":254,"found_hosts":11,"new_hosts":3,"error":null}` | Once, when the scan ends |

The client should refetch `GET /api/discovery/scans/{id}` and the results after a reconnect or after
`discovery.finished`.

---

## [2b-1] Phase 2b-1 contract

Dependencies + `unreachable`, maintenance windows, acknowledge, escalation, fingerprinting / re-identify, HPE
templates, topology. Table semantics: [data-model.md § Phase 2b-1](data-model.md#2b-1-phase-2b-1-additions).
All Phase 1/2a conventions apply. Everything below is **additive**: new keys appear in existing responses,
existing keys keep their meaning (except `Status` gaining a value).

### [2b-1] Errors
| HTTP | code | When |
|---|---|---|
| 400 | `dependency_cycle` | A dependency change (device, group, device move, group move, bulk apply) would create a cycle. `details: [{"device_ids": [3, 7, 3]}]` |
| 409 | `not_acknowledgeable` | Acknowledge on a sensor whose status is not `down`/`warning` |
| 409 | `topology_refresh_running` | Refresh requested while one runs (`details: [{"refresh_id": 12}]`) |
| 400 | `link_not_manual` | PATCH of a discovered (lldp/cdp) link |

### [2b-1] Type changes
```ts
type Status = "up" | "warning" | "down" | "paused" | "unknown" | "unreachable";        // [2b-1] unreachable
type StatusCounts = { up: number; warning: number; down: number; paused: number; unknown: number;
  unreachable: number; total: number };                                              // [2b-1] unreachable

type Acknowledgement = { by: string | null;        // username snapshot
  at: string; comment: string; status: "down" | "warning" };

type Dependency = { mode: "inherit" | "none" | "device"; device_id: number | null; device_name: string | null;
  sensor_id: number | null; sensor_name: string | null };
type EffectiveDependency = { device_id: number; device_name: string;
  sensor_id: number | null; sensor_name: string | null;   // the resolved dependency sensor (null = whole device)
  source: "device" | "group"; group_id: number | null;    // group_id set when source = "group"
  parent_down: boolean };
type MaintenanceRef = { window_id: number; name: string; ends_at: string };

type DeviceIdentity = { device_class: DeviceClass; vendor: string | null; model: string | null;
  confidence: "high" | "medium" | "low"; classified_by: "snmp" | "fingerprint" | "ports" | "none";
  suggested_template: string; snmp_ok: boolean; sys_object_id: string | null; sys_descr: string | null;
  open_ports: number[]; fingerprint: Fingerprint | null; identified_at: string };
type Fingerprint = {
  http: { port: number; scheme: "http" | "https"; status: number | null; title: string | null; server: string | null;
          realm: string | null; markers: string[]; redirects: number; error: string | null }[];
  ssh: { port: number; banner: string | null } | null;
  telnet: { port: number; banner: string | null } | null };
```

- `Sensor` gains `acknowledgement: Acknowledgement | null`, `in_maintenance: boolean`.
- `Device` gains `dependency: Dependency`, `effective_dependency: EffectiveDependency | null`,
  `in_maintenance: boolean`, `maintenance: MaintenanceRef | null`, `identity: DeviceIdentity | null`.
- `Group` gains `dependency: Dependency` (`mode: "inherit"` at a root means none).
- `Event` gains `in_maintenance: boolean`; `kind` gains `"acknowledge" | "maintenance"`.
- `AlertRule` gains `escalation_after_s: number | null`, `escalation_channel_ids: number[]`,
  `repeat_every_s: number | null`.
- `Settings` gains `timezone: string` and `topology_refresh_min: number`.
- `DiscoveryResult` gains `fingerprint: Fingerprint | null`, `model: string | null`,
  `classified_by: "snmp" | "fingerprint" | "ports" | "none"`, `confidence: "high" | "medium" | "low"`.
- `ScanOptions` gains `fingerprint: boolean` (default true) and `fingerprint_timeout_ms: number`
  (default 2000, 500–5000).
- `DeviceTemplate.match[]` gains `enterprises: number[] | null`. `TemplateEntry` kind `snmp_table` gains
  `skip_zero: boolean`, `filters: {oid: string; in: number[] | null; not_in: number[] | null}[]`,
  `interval_s: number | null`.
- `config_fields[].type` gains `value_map`; `snmp_custom.value_type` gains `"enum"` with config keys
  `value_map: Record<string, {status: "up" | "warning" | "down"; text: string}>` and
  `value_map_default: "up" | "warning" | "down"`.
- Tree (`GET /api/tree`): devices gain `in_maintenance: boolean`, `parent_device_id: number | null`
  (effective); sensors gain `acknowledged: boolean`. `status_counts` include `unreachable`.

Query filters: `GET /api/sensors` and `GET /api/devices` accept `status=unreachable`; `GET /api/sensors`
gains `acknowledged` (bool). `GET /api/events` accepts `kind=acknowledge|maintenance` and gains
`in_maintenance` (bool).

### [2b-1] Dependencies

Device/group create and PATCH accept:

```json
{ "dependency": { "mode": "device", "device_id": 2, "sensor_id": null } }
```

- `mode` required in the object; `device_id` required for `device` and must exist (422,
  loc `["body","dependency","device_id"]`); `sensor_id` optional, must belong to `device_id` (422). For
  `inherit`/`none` the ids must be null (422). Omitted `dependency` on create = `{"mode":"inherit"}`.
- Device example response fragment:

```json
{ "dependency": { "mode": "inherit", "device_id": null, "device_name": null, "sensor_id": null, "sensor_name": null },
  "effective_dependency": { "device_id": 2, "device_name": "branch-gw", "sensor_id": 11, "sensor_name": "Ping",
                            "source": "group", "group_id": 4, "parent_down": false },
  "in_maintenance": false, "maintenance": null }
```

- A cycle → `400 dependency_cycle`. Devices/groups mutations broadcast `config.changed` as today.
- **Bulk apply** `POST /api/devices/dependencies` (admin):
  `{"items": [{"device_id": 5, "dependency": {"mode":"device","device_id":1,"sensor_id":null}}]}` (1–1000
  items, unique `device_id`). Atomic: all or nothing, cycles checked on the final state. `200 {"updated": [5, ...]}`.
  One `config.changed {"entity":"device","id":<id>,"action":"updated"}` per changed device.

### [2b-1] Acknowledge (admin)

| Method & path | Request | Response |
|---|---|---|
| `POST /api/sensors/{id}/acknowledge` | `{"comment": "Replacing PSU, ETA 14:00"}` (`comment` optional, ≤ 1000 chars) | `200 Sensor`. `409 not_acknowledgeable` unless the status is `down`/`warning`. Re-ack replaces the comment. |
| `DELETE /api/sensors/{id}/acknowledge` | — | `200 Sensor` (idempotent: not acknowledged → 200 unchanged, no event) |

Both write an `acknowledge` event (`event.created`) and broadcast `sensor.ack`. Viewers → `403 forbidden`.

```json
{ "acknowledgement": { "by": "admin", "at": "2026-11-05T09:12:00Z", "comment": "Replacing PSU", "status": "down" } }
```

### [2b-1] Maintenance windows

```ts
type MaintenanceWindow = { id: number; name: string; enabled: boolean;
  scope: "global" | "group" | "device"; group_id: number | null; group_name: string | null;
  device_id: number | null; device_name: string | null;
  kind: "once" | "weekly";
  starts_at: string | null; ends_at: string | null;                 // once
  weekdays: number[] | null; start_time: string | null;             // weekly: ISO 1=Mon..7=Sun, "HH:MM" local
  duration_min: number | null;                                      // weekly
  timezone: string;                                                 // settings.timezone, informational
  comment: string;
  active_now: boolean;
  occurrence_start: string | null; occurrence_end: string | null;   // current occurrence if active, else next; null = none left
  created_by: string | null; created_at: string; updated_at: string };
```

| Method & path | Role | Request | Response |
|---|---|---|---|
| `GET /api/maintenance-windows` | viewer | Query: `scope`, `group_id`, `device_id`, `active` (bool), `include_past` (bool, default true) | Page of `MaintenanceWindow`, sorted: active first, then `occurrence_start` asc (nulls last), then `id` |
| `POST /api/maintenance-windows` | admin | see below | `201 MaintenanceWindow` |
| `GET /api/maintenance-windows/{id}` | viewer | — | `MaintenanceWindow` |
| `PATCH /api/maintenance-windows/{id}` | admin | subset; `scope`/`kind` changes must send the matching fields | `MaintenanceWindow` |
| `DELETE /api/maintenance-windows/{id}` | admin | — | `204` |
| `GET /api/maintenance-windows/{id}/occurrences` | viewer | Query `days` 1–90 (default 14) | `{"items":[{"start":"...","end":"..."}]}` (≤ 100, from now; a current occurrence is included) |
| `GET /api/maintenance/active` | viewer | — | `{"now":"...","windows": MaintenanceWindow[], "device_ids": number[]}` (devices currently covered) |

Create examples:

```json
{ "name": "Firmware night", "scope": "group", "group_id": 4, "kind": "once",
  "starts_at": "2026-11-10T22:00:00Z", "ends_at": "2026-11-11T02:00:00Z", "comment": "HPE firmware 16.11" }
```
```json
{ "name": "Weekly backup", "scope": "device", "device_id": 9, "kind": "weekly",
  "weekdays": [7], "start_time": "02:00", "duration_min": 90 }
```

- `enabled` defaults true, `comment` "". Validation (422 with loc): scope/id consistency (the other id must be
  null; the referenced group/device must exist), `once`: `starts_at < ends_at`, span ≤ 366 d, weekly fields
  null; `weekly`: `weekdays` 1–7 distinct non-empty, `start_time` `^([01]\d|2[0-3]):[0-5]\d$`, `duration_min`
  1–10080, once fields null. A `once` window entirely in the past is allowed (record).
- Mutations broadcast `config.changed {"entity":"maintenance_window",...}`; the tracker re-evaluates
  immediately (`maintenance.changed` follows when coverage changed).

### [2b-1] Escalation fields (alert rules)

```json
{ "name": "Core down", "sensor_id": null, "on_statuses": ["down","up"], "delay_s": 60, "enabled": true,
  "channel_ids": [1], "escalation_after_s": 900, "escalation_channel_ids": [4], "repeat_every_s": 3600 }
```

Defaults: `null`, `[]`, `null`. Validation in [data-model.md](data-model.md#escalation-and-persistent-alert-state).
PATCH: `escalation_channel_ids` replaces the set; sending `escalation_after_s: null` also clears
`escalation_channel_ids`.

### [2b-1] Notification payload additions
Every sensor notification (webhook body; email/Telegram use the same data) gains three keys; the test payload
has them as `null`/`false`/`null`:

```json
{ "event": "sensor.escalation", "stage": "escalation", "after_maintenance": false,
  "problem_since": "2026-11-05T08:40:00Z", "...": "Phase 1 keys unchanged" }
```

| stage | `event` | Sent to | Subject en / he |
|---|---|---|---|
| `problem` | `sensor.status_changed` | rule channels | `[NetworkMonitor] DOWN: core-sw / Ping` (unchanged); after maintenance: `[NetworkMonitor] DOWN (after maintenance): …` / `… (לאחר תחזוקה)` |
| `recovery` | `sensor.status_changed` (`new_status: "up"`) | every channel in `notified_channel_ids` | unchanged |
| `escalation` | `sensor.escalation` | `escalation_channel_ids` | `[NetworkMonitor] ESCALATION DOWN: core-sw / Ping` / `[NetworkMonitor] הסלמה DOWN: …` |
| `reminder` | `sensor.reminder` | rule channels | `[NetworkMonitor] REMINDER DOWN: …` / `[NetworkMonitor] תזכורת DOWN: …` |

`event_id`, `old_status`, `new_status`, `message`, `time` refer to the status_change event that opened (or
changed) the problem — for `recovery`, to the `up` event (so `time` is the recovery time). `problem_since` is
always the **start of the problem**, also in `recovery` payloads (downtime = `time − problem_since`). Email/Telegram bodies gain a row "Problem since" / "בעיה מאז" (in `settings.timezone`).

### [2b-1] Dashboard
`GET /api/dashboard` gains:

```json
{ "problem_sensors": [ /* down/warning, NOT acknowledged (Phase 1 order) */ ],
  "acknowledged_sensors": [ /* Sensor × up to 20, acknowledged, down then warning, oldest ack first */ ],
  "acknowledged_count": 2,
  "unreachable_devices": [ { "id": 7, "name": "ap-2", "host": "10.0.1.20", "parent_device_id": 2, "parent_device_name": "branch-gw" } ],
  "maintenance": { "active_window_ids": [3], "device_count": 12 } }
```
`unreachable_devices`: devices whose rollup status is `unreachable`, up to 50, by name. `sensor_counts` /
`device_counts` include `unreachable`.

### [2b-1] Settings
- `timezone` (IANA, validated), `topology_refresh_min` (0 or 5–1440). A change of `topology_refresh_min`
  reschedules the topology loop; a change of `timezone` re-evaluates maintenance immediately.
- `POST /api/setup` accepts optional `"timezone"`; an invalid zone is **ignored** (the default — `TZ` if valid,
  else `UTC` — is stored, a warning is logged, setup succeeds), since the wizard sends the browser zone without
  a field to correct it. `PUT /api/settings` still answers 422 for an invalid zone. `GET /api/settings/public` gains
  `timezone` (viewers need it to render windows).

### [2b-1] Re-identify
**`POST /api/devices/{id}/identify`** (admin, no body): runs the discovery probe against `device.host`: SNMP
identity + subtree probes with the device's effective credentials (retries 0, 25 s SNMP budget), TCP connect
to the default discovery port list, fingerprinting, classification, template suggestion. No ping, no DNS. Total
budget 40 s. Stores the result in `devices.identity` and returns `200 DeviceIdentity`. Broadcasts
`config.changed` (device updated). Applying the suggested template is a separate call (existing
`apply-template`). Discovery import also fills `identity` from the result.

### [2b-1] Discovery fingerprinting
- Runs after the TCP probe for found hosts with open 80/443/8080/8443/22/23, when `options.fingerprint`.
  Each probe has `fingerprint_timeout_ms` (connect + read); all of a host's fingerprint probes run
  concurrently and share the scan's TCP connect semaphore (256).
- HTTP: `GET /` with `Host: <ip or target hostname>`, `User-Agent: NetworkMonitor/<version> (discovery)`,
  `Accept: text/html`, no cookies/credentials, TLS verification **off** (fingerprinting only; the TLS setting
  of sensors is unaffected), at most **2** redirects and only when the `Location` resolves to the **same host**
  (same IP literal or the same target hostname) on a port in {80, 443, 8080, 8443}; anything else stops (the
  last response is used). Body read is capped at **64 KiB** (streamed; the rest is discarded).
- SSH: connect, read one line (no client banner is sent). Telnet: connect, read (nothing sent).
- Results: `fingerprint` on the result row and the WS `discovery.result`.

### [2b-1] Topology

```ts
type TopologyNode =
  | { key: string; kind: "device"; device_id: number; name: string; host: string; group_id: number;
      status: Status; in_maintenance: boolean; device_class: DeviceClass | null;   // from identity
      x: number | null; y: number | null; in_filter: boolean }
  | { key: string; kind: "neighbor"; neighbor_id: number; name: string;          // sys_name, else chassis id
      chassis_id: string; mgmt_address: string | null; sys_descr: string | null;
      x: number | null; y: number | null; in_filter: true };
type LinkEnd = { node: string; device_id: number | null; neighbor_id: number | null;
  if_index: number | null; port: string;
  sensor_id: number | null;            // snmp_interface sensor of that device with this if_index
  sensor_status: Status | null; oper_status: number | null;     // last oper_status channel value (1 = up)
  in_bps: number | null; out_bps: number | null; in_util_pct: number | null; out_util_pct: number | null };
type TopologyLink = { id: number; source: "lldp" | "cdp" | "manual"; a: LinkEnd; b: LinkEnd;
  stale: boolean; missed_refreshes: number; first_seen: string; last_seen: string };
type TopologyRefresh = { id: number; trigger: "manual" | "scheduled"; status: "running" | "completed" | "failed";
  started_at: string; finished_at: string | null; devices_total: number; devices_ok: number;
  devices_failed: number; links_found: number; error: string | null };
```

| Method & path | Role | Request | Response |
|---|---|---|---|
| `GET /api/topology` | viewer | Query: `group_id` (recursive), `include_stale` (bool, default true) | `{"nodes": TopologyNode[], "links": TopologyLink[], "last_refresh": TopologyRefresh \| null, "refresh_running": boolean}` |
| `POST /api/topology/refresh` | admin | — | `202 TopologyRefresh` (`running`). `409 topology_refresh_running`. |
| `GET /api/topology/links` | admin | Query: `device_id`, `source`, `stale` | Page of `TopologyLink`, sorted by `id` |
| `POST /api/topology/links` | admin | `{"a_device_id":1,"a_if_index":24,"a_port":"24","b_device_id":5,"b_neighbor_id":null,"b_if_index":null,"b_port":"uplink"}` | `201 TopologyLink` (`source: "manual"`). Exactly one of b ids (422); `a_port` defaults to the ifName of `a_if_index` when given. A port stored as the index default (or null) is **resolved when read** — `switch_ports.name`, else the `snmp_interface` sensor's `if_name`, else the index — so a link created before the first port-inventory run shows the name once the inventory exists. |
| `PATCH /api/topology/links/{id}` | admin | subset of the create fields | `TopologyLink`; discovered link → `400 link_not_manual` |
| `DELETE /api/topology/links/{id}` | admin | — | `204` (any source; a discovered one returns on the next refresh if still reported) |
| `DELETE /api/topology/links?stale=true` | admin | — | `200 {"deleted": n}` (removes all stale links; `stale=true` is required, else 422) |
| `PUT /api/topology/layout` | admin | `{"positions": [{"node": "d:12", "x": 120.5, "y": -40}]}` (1–5000; unknown keys ignored) | `204`. Upsert. |
| `DELETE /api/topology/layout` | admin | — | `204` (clears all positions → auto-layout) |
| `GET /api/topology/dependency-suggestions` | admin | Query `root_device_id` (optional) | see below |

Graph rules:
- With `group_id`: device nodes of the group subtree (`in_filter: true`) plus every node linked to them
  (`in_filter: false` for devices outside the group); only links touching an in-filter device.
- Discovered links identical (same endpoints and ports) to a manual link are omitted. Position = the layout
  row, else `null`.
- Node keys: `d:<device_id>`, `n:<neighbor_id>`.

Dependency suggestions response:

```json
{ "root_device_id": 1,
  "items": [ { "device_id": 5, "device_name": "sw-floor2", "suggested_parent_id": 1, "suggested_parent_name": "core-sw",
               "current": { "mode": "inherit", "device_id": null, "device_name": null, "sensor_id": null, "sensor_name": null },
               "effective_parent_id": null, "hops": 1 } ] }
```
Only devices reached from the root are listed (root excluded), sorted by `hops`, then name. Apply via
`POST /api/devices/dependencies`.

### [2b-1] Test clock (QA only)
With env **`NETMON_TEST_MODE=1`** only (otherwise the routes answer `404 not_found`):
`PUT /api/test/clock` (admin) `{"offset_s": 3600}` → `200 {"now": "...", "offset_s": 3600}`. It shifts
`rt.clock.now()` (used by the maintenance tracker, the alert loop, `active_now`/occurrences and the event times
written by those two components) and wakes both loops. `offset_s` −31536000…31536000; `0` resets. The poller,
JWT expiry and DB `now()` are not affected. Never enabled by installers or compose. When enabled, the server
logs a `WARNING` at startup ("NETMON_TEST_MODE is enabled: PUT /api/test/clock is available — never use this
in production"), writes the same text as a `system` event, and `GET /api/health` reports `"test_mode": true`.

### [2b-1] WebSocket additions

| type | data | When / who |
|---|---|---|
| `sensor.update` | unchanged shape; `status` may be `unreachable`; gains `"acknowledged": bool` | all |
| `sensor.ack` | `{"id":7,"device_id":3,"acknowledgement": Acknowledgement \| null}` | ack, unack, auto-clear; all |
| `maintenance.changed` | `{"active_window_ids":[3],"entered_device_ids":[9,10],"left_device_ids":[]}` | coverage changed (window boundary, CRUD, timezone change); all. Clients refetch tree/devices/sensors/dashboard. |
| `topology.updated` | `{"reason":"refresh"\|"link"\|"layout","refresh_id":12\|null}` | refresh finished (also failed), link CRUD, layout save/reset; all. Clients refetch `GET /api/topology`. |
| `topology.refresh` | `{"refresh": TopologyRefresh}` | refresh started and finished; admins only |
| `config.changed` | `entity` gains `"maintenance_window"`, `"topology_link"` | as today |
| `device.status` / `group.status` | `status_counts` include `unreachable` | as today |

---

## [2b-2] Phase 2b-2 contract

Switch ports view (port inventory, MAC/IP search) and the vendor template library. Table semantics and
algorithms: [data-model.md § Phase 2b-2](data-model.md#2b-2-phase-2b-2-additions). All Phase 1/2a/2b-1
conventions apply. Everything is **additive**: new keys in existing responses, new endpoints, new WS types.
No endpoint performs SNMP on a GET: ports data is read from the last snapshot.

### [2b-2] Errors
| HTTP | code | When |
|---|---|---|
| 409 | `ports_disabled` | `POST /api/devices/{id}/ports/refresh` on a device whose port inventory and ARP collection are both not effective (`details: [{"port_inventory": "auto", "arp_collect": "auto", "effective_class": "server"}]`) |
| 409 | `ports_refresh_running` | A run for this device is in progress (`details: [{"device_id": 3}]`) |
| 404 | `not_found` | `GET /api/devices/{id}/ports/{if_index}` for an ifIndex not in the snapshot |
| 422 | `validation_error` | `GET /api/search/mac` without exactly one of `q` / `ip`, a `q` that is not 6–12 hex digits after removing separators, or an invalid `ip` |

### [2b-2] Type changes
```ts
type SensorType = /* … */ | "snmp_walk_count";                       // [2b-2]
type InventoryMode = "auto" | "on" | "off";
type TemplateSkipReason = "exists" | "probe_failed" | "snmp_error" | "max_reached" | "no_rows"
  | "not_applicable";                                                // [2b-2]
```

- `Device` gains `port_inventory: InventoryMode`, `arp_collect: InventoryMode`, `ports_effective: boolean`,
  `arp_effective: boolean`, `effective_class: DeviceClass` (identity class, else detected class, else
  `"unknown"`). Device create / PATCH accept `port_inventory` and `arp_collect` (422 on other values; omitted on
  create = `"auto"`). A change makes the device due at once (first run within ~60 s).
- `Settings` gains `port_inventory_min: number` (0 or 5–1440), `uplink_mac_threshold: number` (1–1000),
  `port_mac_retention_days: number` (1–365), `reverse_dns: boolean`. A change of `port_inventory_min`
  takes effect on the next scheduler pass.
- `GET /api/sensor-types` lists `snmp_walk_count` (config `oid` str, `count_values` int_list, `label` str,
  `max_rows` int; channels `rows`, `matched`, `unmatched`, unit `#`, primary `matched`) and the new
  `snmp_custom` field `percent_base` (enum `["total","sum"]`, default `"total"`).
- `ApplyTemplateResult.skipped[].reason` may be `not_applicable`.
- `DeviceTemplate`, `TemplateEntry` and the match rule gain keys (below).

### [2b-2] Ports

```ts
type PortKind = "physical" | "lag" | "vlan" | "mgmt" | "other";
type PortState = "up" | "down" | "admin_down" | "absent";
type ConnectedKind = "none" | "client" | "ap" | "switch";
type IfStatus = "up" | "down" | "testing" | "unknown" | "dormant" | "notPresent" | "lowerLayerDown";
type SourceState = "ok" | "empty" | "error" | "skipped" | "unmapped";

type PortPoe = { admin_enabled: boolean | null;
  detection: "disabled" | "searching" | "delivering" | "fault" | "test" | "other_fault" | null;
  power_class: number | null;          // 0–4
  power_w: number | null };            // null on every device in 2b-2 (no vendor column yet)

type PortMac = { mac: string;          // "aa:bb:cc:dd:ee:ff"
  vlan: number | null; vendor: string | null; locally_administered: boolean;
  ip: string | null; ips: string[];    // ip = newest (may be a last-known, non-present one; then ips = [])
  hostname: string | null;             // reverse DNS of ip
  device_id: number | null; device_name: string | null;   // a monitored device whose host equals ip
  first_seen: string; last_seen: string };

type PortNeighbor = { link_id: number; source: "lldp" | "cdp" | "manual"; kind: "device" | "neighbor";
  device_id: number | null; neighbor_id: number | null;
  name: string;                        // device name, else neighbour sys_name, else chassis id
  port: string;                        // the remote port text
  device_class: DeviceClass | null;    // effective class (devices only)
  status: Status | null;               // device rollup status (devices only)
  mgmt_address: string | null };

type Port = { if_index: number; name: string; descr: string; alias: string; kind: PortKind;
  if_type: number | null; admin_status: IfStatus; oper_status: IfStatus;
  state: PortState; alert: boolean; alert_reasons: ("sensor" | "errors" | "poe_fault")[];
  speed_bps: number | null; duplex: "full" | "half" | "unknown" | null; mac: string | null;
  last_change_at: string | null;
  pvid: number | null; mode: "access" | "trunk" | null; untagged_vlans: number[]; tagged_vlans: number[];
  poe: PortPoe | null;                 // null = no PoE row for this port
  in_bps: number | null; out_bps: number | null;
  in_errors_delta: number | null; out_errors_delta: number | null;
  is_uplink: boolean; uplink_reason: "neighbor" | "mac_count" | null;
  connected_kind: ConnectedKind; trunk: boolean;
  neighbor: PortNeighbor | null;       // primary neighbour
  mac_count: number;
  first_mac: PortMac | null;           // null for uplinks
  macs: PortMac[];                     // all present MACs; [] for uplinks (count only)
  sensor_id: number | null; sensor_status: Status | null;  // linked snmp_interface sensor
  collected_at: string };

type PortDetail = Port & {
  neighbors: PortNeighbor[];           // all non-stale neighbours of the port
  macs_hidden: boolean;                // true when is_uplink and include_uplink_macs was not set
  sensor: Sensor | null };             // the full linked sensor (channels with last values)

type HealthItem = { category: "cpu" | "memory" | "temperature" | "poe" | "psu" | "fan";
  status: Status; value: number | null; unit: string; text: string;
  sensor_ids: number[]; primary_sensor_id: number };

type PortInventoryStatus = { last_run_at: string | null; last_ok_at: string | null;
  duration_ms: number | null; error: string | null; running: boolean; next_run_at: string | null;
  sources: { if_xtable: SourceState; duplex: SourceState; bridge: SourceState; vlans: SourceState;
             fdb: SourceState; poe: SourceState; arp: SourceState };
  fdb_mib: "q_bridge" | "bridge" | null; vlan_mib: "static" | "current" | null;
  ports: number; macs: number; arp_entries: number;
  arp_sources: number };               // devices currently collecting ARP (global; 0 → no IPs anywhere)
// next_run_at = last_run_at + port_inventory_min (now when never run); null when the interval is 0 or nothing is effective.
// running = a run of this device is in progress (in-memory).

type DevicePorts = { device_id: number; ports_effective: boolean; arp_effective: boolean;
  port_inventory: InventoryMode; arp_collect: InventoryMode; effective_class: DeviceClass;
  status: PortInventoryStatus;         // never-run devices: nulls, zeros, sources all "skipped"
  settings: { interval_min: number; uplink_mac_threshold: number };
  health: HealthItem[];
  ports: Port[] };                     // all kinds, sorted by if_index; the last snapshot even when no longer effective
```

| Method & path | Role | Request | Response |
|---|---|---|---|
| `GET /api/devices/{id}/ports` | viewer | Query `kind` (repeatable; default all) | `200 DevicePorts` |
| `GET /api/devices/{id}/ports/{if_index}` | viewer | Query `include_uplink_macs` (bool, default false; then the MAC list is returned for uplinks too, capped at 1000) | `200 PortDetail`, `404 not_found` |
| `POST /api/devices/{id}/ports/refresh` | admin | — (no body) | `202 {"device_id": 3, "queued": true}`. Runs ports (if effective) and ARP (if effective) for this device now; `409 ports_disabled`, `409 ports_refresh_running`. `ports.updated` follows. When the device needs class detection (no identity, `detected_at` missing/old) the detection runs inside the request first (SNMP, 10 s budget), so the `202` / `409 ports_disabled` can take up to ≈ 10 s. |
| `GET /api/search/mac` | viewer | Query **exactly one** of `q` (MAC in any notation — `aa:bb:cc:dd:ee:ff`, `aa-bb-…`, `aabb.ccdd.eeff`, `aabbccddeeff` — or a prefix of 6–11 hex digits) and `ip` (IPv4) | `200 MacSearchResult` |

`GET /api/devices/{id}/ports` example (trimmed):

```json
{ "device_id": 3, "ports_effective": true, "arp_effective": false,
  "port_inventory": "auto", "arp_collect": "auto", "effective_class": "switch",
  "status": { "last_run_at": "2026-11-05T10:00:00Z", "last_ok_at": "2026-11-05T10:00:00Z", "duration_ms": 2140,
              "error": null, "running": false, "next_run_at": "2026-11-05T10:10:00Z",
              "sources": { "if_xtable": "ok", "duplex": "ok", "bridge": "ok", "vlans": "ok", "fdb": "ok",
                           "poe": "ok", "arp": "skipped" },
              "fdb_mib": "q_bridge", "vlan_mib": "static", "ports": 13, "macs": 14, "arp_entries": 0,
              "arp_sources": 2 },
  "settings": { "interval_min": 10, "uplink_mac_threshold": 5 },
  "health": [ { "category": "cpu", "status": "up", "value": 7, "unit": "%", "text": "", "sensor_ids": [41], "primary_sensor_id": 41 },
              { "category": "psu", "status": "down", "value": null, "unit": "", "text": "0/1", "sensor_ids": [44], "primary_sensor_id": 44 } ],
  "ports": [
    { "if_index": 1, "name": "1", "descr": "1", "alias": "Reception PC", "kind": "physical", "if_type": 6,
      "admin_status": "up", "oper_status": "up", "state": "up", "alert": false, "alert_reasons": [],
      "speed_bps": 1000000000, "duplex": "full", "mac": "00:1a:1e:00:01:01", "last_change_at": "2026-11-05T07:13:20Z",
      "pvid": 1, "mode": "access", "untagged_vlans": [1], "tagged_vlans": [],
      "poe": { "admin_enabled": true, "detection": "delivering", "power_class": 3, "power_w": null },
      "in_bps": 18233.6, "out_bps": 90211.2, "in_errors_delta": 0, "out_errors_delta": 0,
      "is_uplink": false, "uplink_reason": null, "connected_kind": "client", "trunk": false,
      "neighbor": null, "mac_count": 1,
      "first_mac": { "mac": "00:50:56:01:00:01", "vlan": 1, "vendor": "VMware, Inc.", "locally_administered": false,
                     "ip": "10.0.0.11", "ips": ["10.0.0.11"], "hostname": "pc-reception.lan",
                     "device_id": null, "device_name": null,
                     "first_seen": "2026-11-04T08:00:00Z", "last_seen": "2026-11-05T10:00:00Z" },
      "macs": [ { "mac": "00:50:56:01:00:01", "...": "same as first_mac" } ],
      "sensor_id": null, "sensor_status": null, "collected_at": "2026-11-05T10:00:00Z" },
    { "if_index": 10, "name": "10", "...": "...", "state": "up", "is_uplink": true, "uplink_reason": "neighbor",
      "connected_kind": "switch", "trunk": true,
      "neighbor": { "link_id": 7, "source": "lldp", "kind": "device", "device_id": 5, "neighbor_id": null,
                    "name": "access-1", "port": "Ten-GigabitEthernet1/0/49", "device_class": "switch",
                    "status": "up", "mgmt_address": "10.0.0.5" },
      "mac_count": 2, "first_mac": null, "macs": [] } ] }
```

Search result:

```ts
type MacLocation = { mac: string; vendor: string | null; locally_administered: boolean;
  ip: string | null; ips: string[]; hostname: string | null;
  device_id: number; device_name: string; device_host: string;
  if_index: number; port: string; port_alias: string; vlan: number | null;
  is_uplink: boolean; present: boolean; first_seen: string; last_seen: string;
  best: boolean };                     // the most likely access port of this MAC (≤ 1 per MAC)
type ArpHit = { ip: string; mac: string; hostname: string | null; source_device_id: number;
  source_device_name: string; present: boolean; first_seen: string; last_seen: string };
type MacSearchResult = { query: { mac: string | null; mac_prefix: string | null; ip: string | null };
  items: MacLocation[];                // ≤ 200
  arp: ArpHit[] };                     // ≤ 50; the ARP rows behind the answer
```

- `q` with 12 hex digits → exact MAC (`query.mac` normalised); 6–11 → prefix on the hex digits
  (`query.mac_prefix` lower-case hex, e.g. `"00505601"`). The server turns the prefix into the stored colon
  form and searches `mac LIKE '00:50:56:01%'` (odd digit count: `'0050560'` → `'00:50:56:0%'`), served by the
  index `ix_port_macs_mac_prefix` (`varchar_pattern_ops`, migration 0005); never `replace(mac, ':', '')`. `ip` → the MACs that `arp_entries` maps that IP to
  (present first, then newest), then their locations (`query.ip`).
- `items` sort: `best` desc, `present` desc, `is_uplink` asc, `last_seen` desc, `device_name`, `if_index`.
  `best` = for each MAC, its first row in that order if it is `present` and not an uplink.
- Uplink status in search uses the same rule as the port view.

### [2b-2] Device templates

```ts
type TemplateRule = { device_classes: DeviceClass[] | null; vendors: string[] | null; requires_snmp: boolean;
  any_open_ports: number[] | null; enterprises: number[] | null;
  sys_object_id_prefixes: string[] | null;            // [2b-2]
  sys_descr_regex: string | null };                    // [2b-2]
type TemplateFilter = { oid: string; in: number[] | null; not_in: number[] | null };
type Confidence = "high" | "medium" | "low";
type TemplateEntry =
  | { kind: "sensor"; type: SensorType; name: L10n; config: Record<string, unknown>; channels: ChannelOverride[];
      interval_s: number | null; probe: boolean;
      when: TemplateFilter[];                          // [2b-2]
      confidence: Confidence | null; notes: string }   // [2b-2]
  | { kind: "snmp_interfaces"; oper_up_only: boolean; exclude_if_types: number[]; include_name_regex: string | null;
      exclude_name_regex: string | null; max: number; channels: ChannelOverride[];
      confidence: Confidence | null; notes: string }
  | { kind: "snmp_table"; walk_oid: string; label_oid: string | null; label2_oid: string | null;
      percent_of_column: string | null; name: L10n; config: Record<string, unknown>; channels: ChannelOverride[];
      max: number; skip_zero: boolean; filters: TemplateFilter[]; interval_s: number | null;
      entity_sensor_scale?: true;  // [1.2.1] present only when set
      confidence: Confidence | null; notes: string };
type DeviceTemplate = { id: string; name: L10n; description: L10n; requires_snmp: boolean; priority: number;
  match: TemplateRule[]; entries: TemplateEntry[];
  version: number; verified: boolean; verified_note: string; vendor: string | null;   // [2b-2]
  source: "builtin" | "user"; overrides_builtin: boolean; file: string };             // [2b-2]
type TemplateLoadError = { source: "builtin" | "user"; file: string; template_id: string | null;
  path: string; message: string };
type TemplateLibraryStatus = { loaded_at: string; builtin_count: number; user_count: number;
  overridden: string[];                // ids of built-ins replaced by user files, sorted
  user_dir: string;                    // absolute path of ${NETMON_DATA_DIR}/templates (admins only, else "")
  errors: TemplateLoadError[] };       // admins only, else []
```

| Method & path | Role | Response |
|---|---|---|
| `GET /api/device-templates` | viewer | `{"items": DeviceTemplate[], "library": TemplateLibraryStatus}` — items sorted by `priority` desc, then `id`. `library.errors`/`user_dir` are filled for admins only. |
| `GET /api/device-templates/{id}` | viewer | `DeviceTemplate` (unchanged otherwise) |
| `POST /api/device-templates/reload` | admin | — (no body). Re-reads the built-in library and the user directory, swaps the registry atomically. `200 TemplateLibraryStatus` (always 200: a bad file — invalid syntax, too large, too deeply nested, any validation error — is only an entry in `errors`, never a 500, and never removes the other templates). Broadcasts `templates.reloaded`. |
| `POST /api/devices/{id}/apply-template` | admin | unchanged; `template_id` is validated against the **current** registry (user templates included). |

`POST /api/discovery/import` validates `template_id` against the current registry too (unknown → 422, loc as
today).

### [2b-2] WebSocket additions

| type | data | When / who |
|---|---|---|
| `ports.updated` | `{"device_id": 3, "ok": true, "collected_at": "2026-11-05T10:00:00Z", "arp_source": false}` | After every port/ARP run of a device (scheduled or manual), also failed ones (`ok: false`); all. `arp_source` = this run collected ARP for the device (`arp_effective`), so clients know without their own cache. Clients refetch `GET /api/devices/{id}/ports` (and an open port modal). |
| `templates.reloaded` | `{"loaded_at": "...", "count": 19, "error_count": 0}` | After `POST /api/device-templates/reload` (not at startup); all. Clients invalidate the templates queries. |
| `config.changed` | unchanged; a device PATCH of `port_inventory` / `arp_collect` is a normal `device` update | as today |

**Client behaviour on `ports.updated`** (replaces the earlier "refetch everything" recommendation — with many
switches that refetched every few seconds):
- `data.device_id` = the device whose ports view / port modal is open → refetch `GET /api/devices/{id}/ports`
  and the open port detail at once;
- `data.arp_source` is true (an **ARP source**; older servers without the key: `arp_effective` from the cached `/api/devices`) → IPs/hostnames shown
  elsewhere may have changed: mark the open ports view / port detail stale, refetch them **debounced** (trailing,
  at most once per 30 s, only while a view/detail is open, so a wall-screen dashboard with no focus events also
  updates) and also on the next window focus;
- any other device → nothing;
- MAC/IP search results (`mac-search`) are **not** invalidated (the user re-runs the search).

---

## [2b-3] Phase 2b-3 contract

Feature toggles, traps, syslog, event rules/sensors, flapping, SIEM forwarding, LAG, Active Directory login,
audit log, device timeline, Microsoft Teams, API tokens. Table semantics: [data-model.md § Phase
2b-3](data-model.md#2b-3-phase-2b-3-additions); flows: architecture.md § 12. All earlier conventions apply.
Everything is **additive**. Every feature is **OFF by default**; a disabled feature's endpoints answer `409
feature_disabled` (data-model § Feature toggles lists the exceptions).

### [2b-3] Errors
| HTTP | code | When |
|---|---|---|
| 409 | `feature_disabled` | The endpoint belongs to a feature that is OFF. `details: [{"feature": "traps"}]` |
| 409 | `sensor_passive` | `POST /api/sensors/{id}/run` / `POST /api/sensors/test` for `trap` / `syslog` / `port_flap` |
| 400 | `not_event_sensor` | `/clear` or `/conditions` on a sensor that is not an event sensor |
| 409 | `builtin_rule` | `DELETE` of a built-in event rule |
| 409 | `ldap_managed` | Password / role / email change of an LDAP user (`PATCH /api/users/{id}`, `POST /api/auth/change-password`, `PATCH /api/auth/me` email) |
| 409 | `last_admin` | (extended) the action would leave no active **local** admin |
| 503 | `directory_unavailable` | AD login when no DC answered, the service bind failed, or the bind password is unreadable |
| 429 | `rate_limited` | (extended) also: 5 failed directory logins per username in 15 min; 20 invalid API tokens per IP in 5 min |
| 400 | `invalid_cursor` | A malformed or foreign `cursor` |
| 502 | `teams_error` | Teams test send failed (`message` = HTTP status + first 200 chars of the body; the URL never appears) |
| 502 | `forward_error` | SIEM test send failed (connect/TLS error text) |
| 403 | `forbidden` | (extended) an API token used on `/api/auth/*`, `/api/users*`, `/api/tokens*` |

### [2b-3] Type changes
```ts
type SensorType = /* … */ | "trap" | "syslog" | "port_flap";
type NotificationType = "email" | "webhook" | "telegram" | "teams";
type EventKind = /* … */ | "notification_sent" | "port_change";
type FeatureName = "traps" | "syslog" | "event_alerts" | "lag" | "ldap" | "audit" | "timeline"
  | "flapping" | "siem_forwarding";   // Teams channels and API tokens are always available (no toggle)
type Features = Record<FeatureName, boolean>;

type User = { /* Phase 1 fields */ source: "local" | "ldap"; display_name: string | null;
  ldap_synced_at: string | null };
```
- `Device` gains `source_addresses: string[]` (create/PATCH; ≤ 16 IPv4/IPv6 addresses, 422 otherwise),
  `source_address_conflicts: {address: string; device_id: number; device_name: string}[]` (read-only),
  `event_sensor_optout: ("trap" | "syslog" | "port_flap")[]` (read-only; managed by sensor create/delete).
- `SensorTypeInfo` (`GET /api/sensor-types`) gains `passive: boolean`; the three event types are listed.
- `Sensor` for event types: `config` `{}`, `channels` = `[{"key":"active",…}]`; PATCH accepts `name`,
  `enabled`, `sort_order` only (other keys ignored). `POST /api/sensors` with an event type creates it (one per
  device per type, `409 conflict` `details: [{"field":"type"}]`) and removes it from `event_sensor_optout`;
  `DELETE` of an event sensor adds the type to `event_sensor_optout`.
- `Port` (2b-2) gains (null/[]/false while `lag` is OFF):
  `lag: {if_index: number; name: string; attached: boolean | null} | null`, `lag_members: number[]`,
  `vlans_from_lag: boolean`; `PortMac` gains `via_lag: boolean`; `PortInventoryStatus.sources` gains `lag:
  SourceState` and the status gains `lag_mib: "ieee8023" | "if_stack" | "both" | null`; `MacLocation` gains
  `lag_members: string[]`.
- `AlertPayload` (webhook/Telegram/email/Teams) gains `"after_flapping": boolean` (like `after_maintenance`).
- `GET /api/health` gains `"trgm": bool` and `"ingest": {"trap": ReceiverState, "syslog_udp": ReceiverState,
  "syslog_tcp": ReceiverState}` with `ReceiverState = "listening" | "disabled" | "error"`.

### [2b-3] Feature toggles and settings
- `GET /api/settings/public` gains `features: Features` and `ldap: {"domain": "mnidom.net", "netbios_domain":
  "MNIDOM"} | null` (null while `ldap` is OFF).
- `Settings` gains every key of data-model § Settings keys. Changes go through `PUT /api/settings` (partial
  merge). `feature_*_enabled` changes are applied at runtime and broadcast as `features.changed`. Unknown
  keys are ignored (not stored, no 422) — this includes `feature_teams_enabled` / `feature_api_tokens_enabled`
  from earlier builds (Teams and API tokens are no longer toggles). Turning
  `ldap` ON validates the stored `ldap` config (422 with the missing fields).
- Secret handling (same rules as § Secret fields):
  - `trap_communities`: response `null` + `trap_communities_count`; request omitted/`null` keep, list (also
    `[]`) replaces.
  - `trap_v3_users[]`: each `auth_key`/`priv_key` `null` + `auth_key_set`/`priv_key_set` in responses; in a
    request, an entry with the same `username` as a stored entry keeps a key given as `null`/omitted, `""`
    clears, a string replaces; the list replaces the stored list.
  - `ldap.bind_password`: response `null` + `bind_password_set` + `bind_password_unreadable`; request rules as
    `smtp.password`. `ldap` is merged field by field.
- `siem_targets[]` has no secrets.

```json
{ "feature_traps_enabled": true, "feature_syslog_enabled": true, "feature_event_alerts_enabled": true,
  "feature_lag_enabled": true, "feature_ldap_enabled": false, "feature_audit_enabled": true,
  "feature_timeline_enabled": true, "feature_flapping_enabled": true,
  "feature_siem_forwarding_enabled": false,
  "trap_retention_days": 30, "syslog_retention_days": 30, "audit_retention_days": 365,
  "trap_community_check": "any", "trap_communities": null, "trap_communities_count": 0,
  "trap_v3_users": [ { "username": "netmon-trap", "security_level": "authPriv", "auth_protocol": "SHA",
     "auth_key": null, "auth_key_set": true, "priv_protocol": "AES128", "priv_key": null, "priv_key_set": true } ],
  "ingest_unknown_sources": "store", "ingest_ignored_sources": ["10.9.0.0/16"],
  "flap_threshold": 5, "flap_window_min": 10, "flap_quiet_min": 15, "event_linkdown_scope": "important",
  "siem_targets": [ { "id": "a1b2c3", "name": "QRadar", "enabled": true, "host": "siem.mnidom.net", "port": 6514,
     "protocol": "tls", "tls_verify": true, "kinds": ["syslog","trap"], "min_severity": 7, "sources": [],
     "include_unknown": false } ],
  "ldap": { "servers": ["dc1.mnidom.net","dc2.mnidom.net"], "security": "ldaps", "tls_verify": true,
     "domain": "mnidom.net", "netbios_domain": "MNIDOM", "base_dn": "DC=mnidom,DC=net",
     "bind_dn": "svc-netmon@mnidom.net", "bind_password": null, "bind_password_set": true,
     "bind_password_unreadable": false,
     "admin_group_dn": "CN=NetMon-Admins,OU=Groups,DC=mnidom,DC=net",
     "viewer_group_dn": "CN=NetMon-Viewers,OU=Groups,DC=mnidom,DC=net", "timeout_s": 5 } }
```

### [2b-3] Cursor pages (traps, syslog, timeline)
These large, append-only collections use **keyset pagination** instead of the page envelope (exact counts are
too expensive): query `limit` (1–500, default 100) and `cursor` (opaque string from the previous response);
response `{"items": [...], "next_cursor": "…" | null, "limit": 100}`, sorted `time DESC, id DESC`. Counts come
from the `/stats` endpoints. Time range: `since` / `until` (ISO); `since` defaults to `until − 24 h`
(`until` defaults to now); a range longer than 31 days → 422.

### [2b-3] Traps (viewer; feature `traps`)
```ts
type TrapVarbind = { oid: string; name: string | null; type: string; value: string; display: string };
type Trap = { id: number; time: string; source_ip: string; agent_addr: string | null;
  device_id: number | null; device_name: string | null;
  version: "v1" | "v2c" | "v3"; pdu: "trap" | "inform"; security_name: string | null;
  trap_oid: string; trap_name: string | null; uptime_ticks: number | null;
  summary: string; varbinds: TrapVarbind[]; matched_rule_ids: number[] };
```
| Method & path | Request | Response |
|---|---|---|
| `GET /api/traps` | Query: `device_id`, `group_id` (recursive), `unknown` (bool: only unattributed), `source_ip`, `trap_oid` (prefix), `rule_id` (in `matched_rule_ids`), `q` (substring of `summary`, 1–200 chars, case-insensitive), `since`, `until`, `cursor`, `limit` | cursor page of `Trap` |
| `GET /api/traps/{id}` | — | `Trap`, `404` |
| `GET /api/traps/stats` | same filters (no cursor) | `TrapStats` |

```ts
type TrapStats = { since: string; until: string; bucket_s: number;   // 60 | 300 | 900 | 3600 | 21600 | 86400 (≈ 60 buckets)
  buckets: { t: string; count: number }[]; total: number;
  top_sources: { device_id: number | null; device_name: string | null; source_ip: string; count: number }[];  // ≤ 10
  top_traps: { trap_oid: string; trap_name: string | null; count: number }[] };                              // ≤ 10
```

### [2b-3] Syslog (viewer; feature `syslog`)
```ts
type SyslogMessage = { id: number; time: string; device_time: string | null; source_ip: string;
  device_id: number | null; device_name: string | null; transport: "udp" | "tcp";
  format: "rfc5424" | "rfc3164" | "raw"; facility: number; severity: number;
  hostname: string; program: string; pid: string; msgid: string; structured_data: string | null;
  message: string; truncated: boolean; matched_rule_ids: number[] };
type SyslogStats = { since: string; until: string; bucket_s: number;
  buckets: { t: string; counts: number[] }[];        // 8 entries, index = severity
  total: number; by_severity: number[];              // 8
  top_sources: { device_id: number | null; device_name: string | null; source_ip: string; count: number }[];
  top_programs: { program: string; count: number }[] };
```
| Method & path | Request | Response |
|---|---|---|
| `GET /api/syslog` | Query: `device_id`, `group_id`, `unknown`, `source_ip`, `severity_max` (0–7), `facility` (repeatable), `program` (repeatable, case-insensitive exact), `hostname`, `rule_id`, `q` (substring of `message`, 1–200), `since`, `until`, `cursor`, `limit` | cursor page of `SyslogMessage` |
| `GET /api/syslog/{id}` | — | `SyslogMessage` |
| `GET /api/syslog/stats` | same filters | `SyslogStats` |

### [2b-3] Ingest status, unknown sources, forwarding (feature: `traps` or `syslog`; forwarding: `siem_forwarding`)
```ts
type ReceiverStatus = { kind: "trap" | "syslog_udp" | "syslog_tcp"; port: number; enabled: boolean;
  state: "listening" | "disabled" | "error"; error: string | null; since: string | null };
type IngestCounters = { received: number; stored: number; forwarded: number;
  dropped: { rate_limited: number; queue_full: number; too_large: number; parse_error: number;
    ignored_source: number; unknown_source: number; bad_community: number; v3_auth: number;
    unsupported: number; db_error: number } };
type ForwarderStatus = { id: string; name: string; state: "connected" | "connecting" | "idle" | "error" | "disabled";
  sent: number; dropped: number; errors: number; queue: number; last_error: string | null };
type IngestStatus = { receivers: ReceiverStatus[]; counters_since: string;
  counters: { trap: IngestCounters; syslog: IngestCounters };
  rate_per_min: { trap: number; syslog: number };          // last 5 min average
  queue: { trap: number; syslog: number; capacity: number };
  tcp_connections: number; nat_suspected: boolean; nat_address: string | null;
  v3_failures: { time: string; source_ip: string; engine_id: string; username: string; reason: string }[];   // admins only, else []
  forwarders: ForwarderStatus[];                            // admins only, else []
  trgm: boolean; timescale: boolean };
type UnknownSource = { source_ip: string; syslog_count: number; trap_count: number; last_seen: string;
  last_hostname: string; ignored: boolean };
```
| Method & path | Role | Request | Response |
|---|---|---|---|
| `GET /api/ingest/status` | viewer | — | `IngestStatus` |
| `GET /api/ingest/unknown-sources` | viewer | Query `days` (1–30, default 7) | `{"items": UnknownSource[]}` (≤ 200, by `last_seen` desc) |
| `POST /api/ingest/unknown-sources/assign` | admin | `{"source_ip": "10.0.0.12", "device_id": 3}` | `200 {"device_id": 3, "source_addresses": ["10.0.0.12"], "reassigning": true}`; `422` invalid IP / already a source of this device; `404` device. Audited `ingest.assign_source`; `config.changed` device. |
| `POST /api/ingest/forwarders/test` | admin | `{"target_id": "a1b2c3"}` or `{"target": SiemTarget}` (unsaved) | `200 {"ok": true, "message": "sent"}`; `502 forward_error` |

"Ignore source" is `PUT /api/settings {"ingest_ignored_sources": [...]}` (the UI appends).

### [2b-3] Event rules (read: viewer; write: admin; feature: `traps` or `syslog`)
```ts
type TrapMatch = { trap_oids: string[]; varbinds: { oid: string; op: "eq" | "ne" | "contains" | "regex" | "exists" | "absent"; value: string | null }[];
  ports: "any" | "important" };
type SyslogMatch = { severity_max: number | null; facilities: number[]; programs: string[];
  text: string | null; text_regex: boolean };
type EventRule = { id: number;
  name: string;            // built-in rules with their default name: in the request user's language (he/en)
  kind: "trap" | "syslog"; enabled: boolean;
  builtin: string | null;                       // builtin_key
  priority: number; scope: "global" | "group" | "device"; group_id: number | null; device_id: number | null;
  scope_name: string | null;                    // group or device name
  match: TrapMatch | SyslogMatch; action: "warning" | "down" | "ignore";
  key_varbind_oid: string | null; key_regex: string | null;
  clear_match: TrapMatch | SyslogMatch | null; clear_after_min: number | null;
  active_conditions: number; last_matched_at: string | null; created_at: string; updated_at: string };
```
| Method & path | Request | Response |
|---|---|---|
| `GET /api/event-rules` | Query `kind` | `{"items": EventRule[]}` sorted `priority, id` (not paginated; ≤ 500 rules, enforced on create → 422) |
| `POST /api/event-rules` | `EventRule` writable fields (`name`, `kind`, `enabled`?, `priority`?, `scope`, `group_id`, `device_id`, `match`, `action`, `key_varbind_oid`?, `key_regex`?, `clear_match`?, `clear_after_min`?) | `201 EventRule` |
| `GET /api/event-rules/{id}` | — | `EventRule` |
| `PATCH /api/event-rules/{id}` | subset (`kind` immutable → 422). Removes the rule's conditions. | `EventRule` |
| `DELETE /api/event-rules/{id}` | — | `204`; `409 builtin_rule` |
| `POST /api/event-rules/{id}/reset` | — (built-in only, else 404) | `EventRule` |
| `POST /api/event-rules/test` | `{"rule": <writable fields>, "hours": 24}` (1–168) | `{"scanned": 1000, "matched": 12, "cleared": 3, "items": [ {"kind": "trap", "id": 991, "time": "...", "device_name": "sw1", "text": "linkDown …", "result": "raise" \| "clear" \| "ignore", "key": "5"} ]}` — newest stored records of that kind (≤ 5000 scanned, ≤ 50 items), no side effects; scope respected |

Validation (422, `details` with `loc`): data-model § Event rules. `config.changed` entity `event_rule`.

### [2b-3] Event sensors (feature `event_alerts`, except `/conditions` and `/clear`)
```ts
type EventCondition = { id: number; source: "rule" | "flap"; rule_id: number | null; rule_name: string | null;
  key: string; status: "warning" | "down"; first_at: string; last_at: string; count: number; message: string;
  last_kind: "trap" | "syslog" | null; last_ref_id: number | null; expires_at: string | null };
```
| Method & path | Role | Response |
|---|---|---|
| `GET /api/sensors/{id}/conditions` | viewer | `{"items": EventCondition[]}` (worst, newest first); `400 not_event_sensor` |
| `POST /api/sensors/{id}/clear` | admin | `200 Sensor` (status `up`); audited `sensor.clear_events` |

### [2b-3] Microsoft Teams channel (always available)
`notification_channels.type = "teams"`, `config`: `{"url": "https://prod-12.westeurope.logic.azure.com/workflows/…/triggers/manual/paths/invoke?api-version=…&sig=…"}`.
- `url` is **secret**: responses `"url": null, "url_set": true, "url_host": "prod-12.westeurope.logic.azure.com",
  "legacy": false`; requests follow § Secret fields; required on create; must be `https://`, ≤ 2048 chars.
  `legacy = true` when the host ends with `webhook.office.com` or `outlook.office.com` (UI warning).
- Test: `POST /api/notification-channels/{id}/test` → `200` / `502 teams_error`.
- No feature toggle: create/patch/test/send never answer `409 feature_disabled`.
- Body sent (problem example, `en`):
```json
{ "type": "message",
  "attachments": [ { "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": null,
    "content": { "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
      "version": "1.4", "msteams": { "width": "Full" },
      "body": [
        { "type": "TextBlock", "text": "DOWN: core-sw / Ping", "weight": "Bolder", "size": "Medium",
          "color": "Attention", "wrap": true },
        { "type": "FactSet", "facts": [
          { "title": "Status", "value": "up → down" }, { "title": "Device", "value": "core-sw (10.0.0.2)" },
          { "title": "Sensor", "value": "Ping" }, { "title": "Message", "value": "Host unreachable" },
          { "title": "Since", "value": "2026-11-08 12:00 (Asia/Jerusalem)" }, { "title": "Rule", "value": "All down" } ] } ],
      "actions": [ { "type": "Action.OpenUrl", "title": "Open in NetworkMonitor", "url": "https://netmon.mnidom.net/sensors/7" } ] } } ] }
```
  `color`: `Attention` down, `Warning` warning/escalation/reminder, `Good` recovery; the title prefix follows the
  existing stage subjects (`ESCALATION:`, `REMINDER:`, `RECOVERED:`); Hebrew texts when `default_language = he`
  (then `"version": "1.5"` and `"rtl": true` on the card, which Teams supports; otherwise 1.4). `actions` is omitted without
  `public_base_url`. Test send: title "NetworkMonitor test message", no facts.

### [2b-3] LAG
No new endpoints; fields above. `lag` OFF → the fields are null/[]/false and walks #13/#14 are not sent.

### [2b-3] Active Directory (feature `ldap`; test endpoints work while OFF)
**Login:** `POST /api/auth/login` request unchanged (`username` now 1–256 chars, `password` 0–1024). Errors:
`401 invalid_credentials` (every credential/account/group reason — the reason is only in the audit log),
`429 rate_limited`, `503 directory_unavailable`. Response `{"user": User}` (`source: "ldap"` for AD users).
Login failure reasons (audit `reason`): `bad_password`, `unknown_user`, `inactive`, `empty_password`,
`not_in_group`, `account_disabled`, `account_locked`, `account_expired`, `password_expired`,
`password_must_change`, `not_permitted`, `ambiguous_user`, `ldap_disabled`, `directory_unavailable`,
`rate_limited`, `token_invalid`.

| Method & path | Role | Request | Response |
|---|---|---|---|
| `POST /api/ldap/test` | admin | `{"ldap": <partial LDAP settings>}` or `{}` (stored); merged over the stored config, secret rules apply | `200 LdapTestResult` |
| `POST /api/ldap/test-user` | admin | `{"username": "jdoe", "password": "…", "ldap": <optional partial>}` | `200 LdapUserTestResult` (no user is created; counts toward the directory guard; the password is never logged or audited) |

```ts
type LdapServerResult = { server: string;           // "dc1.mnidom.net:636"
  ok: boolean; security: "ldaps" | "starttls"; latency_ms: number | null;
  tls: { subject: string; issuer: string; not_after: string; verified: boolean } | null;
  active_directory: boolean;                         // rootDSE supportedCapabilities has 1.2.840.113556.1.4.800
  error: string | null };                            // e.g. "certificate verify failed: hostname mismatch"
type LdapTestResult = { ok: boolean; discovered_via_dns: boolean; servers: LdapServerResult[];
  service_bind: { ok: boolean; error: string | null };
  base_dn: { ok: boolean; error: string | null };
  admin_group: { dn: string; found: boolean } | null; viewer_group: { dn: string; found: boolean } | null;
  message: string };
type LdapUserTestResult = { ok: boolean;
  steps: { step: "connect" | "service_bind" | "search_user" | "account_state" | "user_bind" | "groups";
           ok: boolean; detail: string; server: string | null }[];
  user: { dn: string; sam_account_name: string; user_principal_name: string | null; display_name: string | null;
          mail: string | null; disabled: boolean; locked: boolean } | null;
  role: "admin" | "viewer" | null; reason: string | null };   // reason code as above
```

**Users API changes:** `User` gains `source`, `display_name`, `ldap_synced_at`. `POST /api/users` creates
local users only. `PATCH /api/users/{id}` for an LDAP user: `is_active`, `language` allowed; `password`,
`role`, `email` → `409 ldap_managed`. `POST /api/auth/change-password` for an LDAP user → `409 ldap_managed`.
`last_admin` counts active **local** admins. Session cookie: for LDAP users the sliding renewal stops at 12 h
after the login (then 401 → login page).

### [2b-3] Audit log (admin; feature `audit`)
```ts
type AuditEntry = { id: number; time: string; actor_type: "user" | "token" | "system" | "anonymous";
  actor_user_id: number | null; actor_name: string; token_id: number | null; ip: string | null;
  action: string; entity_type: string | null; entity_id: number | null; entity_name: string | null;
  device_id: number | null; source: "local" | "ldap" | "token" | null; reason: string | null;
  changes: Record<string, [unknown, unknown]> };
```
| Method & path | Request | Response |
|---|---|---|
| `GET /api/audit` | Query: `since`, `until`, `actor` (substring of `actor_name`), `actor_user_id`, `action` (exact or prefix ending in `.`, e.g. `device.`), `entity_type`, `entity_id`, `device_id`, `ip`, `page`, `page_size` | page envelope of `AuditEntry`, `time DESC, id DESC` (counts are cheap here: indexed, bounded volume) |
| `GET /api/audit/export.csv` | same filters | `200 text/csv; charset=utf-8` with BOM, `Content-Disposition: attachment; filename="netmon-audit-<from>-<to>.csv"`, columns `time,actor_type,actor_name,ip,action,entity_type,entity_id,entity_name,source,reason,changes` (`changes` as compact JSON), ≤ 100 000 rows (header `X-Netmon-Truncated: true` when cut), streamed |

### [2b-3] Device timeline (viewer; feature `timeline`)
```ts
type TimelineKind = "status" | "notification" | "ack" | "maintenance" | "trap" | "syslog" | "port" | "audit";
type TimelineItem = { kind: TimelineKind; id: number; time: string;
  title: string;                         // short English fallback; the UI builds texts from the fields
  status: Status | null; old_status: Status | null;     // status, port, notification
  severity: number | null;               // syslog
  sensor_id: number | null; sensor_name: string | null;
  message: string;                       // event message / trap summary / syslog message (≤ 500) / audit action
  ref: { type: "event" | "trap" | "syslog" | "audit"; id: number };
  actor: string | null };                // ack / audit
```
`GET /api/devices/{id}/timeline` — query `kinds` (repeatable; default all available: `trap`/`syslog` only when
those features are ON, `audit` only for admins with `audit` ON, `port` always selectable), `since`, `until`,
`cursor`, `limit` (1–200, default 50) → cursor page of `TimelineItem` (`time DESC`, ties by kind order above,
then `id DESC`). `404` device.

### [2b-3] API tokens (always available)
No feature toggle: `/api/tokens*` never answer `409 feature_disabled` and `Authorization: Bearer` is always
accepted (subject to the rules below).
```ts
type ApiToken = { id: number; name: string; prefix: string; role: "admin" | "viewer";
  user_id: number; username: string; created_at: string; expires_at: string;
  last_used_at: string | null; last_used_ip: string | null; revoked_at: string | null;
  state: "active" | "expired" | "revoked" };
```
| Method & path | Role | Request | Response |
|---|---|---|---|
| `GET /api/tokens` | viewer (session only) | Query `all` (admin: every user's tokens) | `{"items": ApiToken[]}` (own; newest first) |
| `POST /api/tokens` | viewer (session only) | `{"name": "backup-script", "role": "viewer", "expires_days": 90}` (`role` ≤ own role → else 422; 1–365) | `201 {"token": "nmt_…", "item": ApiToken}` — the only time the token text is returned |
| `DELETE /api/tokens/{id}` | owner or admin (session only) | — | `204` (sets `revoked_at`; idempotent) |

Usage: `Authorization: Bearer nmt_…` on any other `/api/*` endpoint, with the effective role min(token role,
owner's role). Bearer requests skip the CSRF Origin check, never set cookies, and get `401 unauthenticated`
when the token is unknown/expired/revoked or the feature is OFF.

### [2b-3] WebSocket additions

**Client → server:** `{"type":"subscribe","topics":["syslog","trap"]}`, `{"type":"unsubscribe","topics":[…]}`
→ server answers `{"type":"subscribed","data":{"topics":["syslog"]}}`. Unknown topics are ignored. Topics of a
disabled feature are accepted but receive nothing.

| type | data | When / who |
|---|---|---|
| `syslog.batch` | `{"items": SyslogMessage[], "skipped": 0}` | ≤ 1/s while new records exist; subscribers of `syslog` only |
| `trap.batch` | `{"items": Trap[], "skipped": 0}` | same for `trap` |
| `features.changed` | `{"features": Features}` | after a toggle change; all. Clients refetch `/api/settings/public`. |
| `ingest.receiver` | `{"receiver": ReceiverStatus}` | a receiver changed state (listening/error/disabled); admins only |
| `config.changed` | `entity` gains `"event_rule"`, `"api_token"` (owner's sockets and admins) | as today |
| `sensor.update` | unchanged; event sensors send it on status/condition changes | all |

## [3] Phase 3 contract

Config backup, flows, new-device alerts, SSO (Kerberos, OIDC), reports, NOC kiosk, acknowledge from
Telegram/Teams. Table semantics: [data-model.md § Phase 3](data-model.md#3-phase-3-additions); flows and
decisions: architecture.md § 13. All earlier conventions apply. Everything is **additive** and every feature is
**OFF by default**; a disabled feature's endpoints answer `409 feature_disabled` (exceptions: data-model § [3]
Feature toggles; kiosk endpoints answer `404`).

### [3] Errors
| HTTP | code | When |
|---|---|---|
| 409 | `backup_running` | `POST …/config/backup` while a backup of that device runs (the request is merged; `details: [{"device_id": 3}]`) — informational, the UI shows "already running" |
| 409 | `host_key_mismatch` | Accepting a host key whose fingerprint differs from the presented one (it changed again) |
| 400 | `no_host_key_change` | Accept on a device whose host key is not in state `changed` |
| 422 | `invalid_private_key` | SSH private key does not parse (no key material in the message) |
| 502 | `ssh_error` | `POST …/ssh/test` failed: `message` = error code + detail (`auth_failed: Permission denied`), `details: [{"error": "auth_failed", "step": "auth"}]` |
| 422 | `invalid_keytab` | Keytab upload not parseable / unsupported version |
| 401 | `sso_failed` | Kerberos/OIDC sign-in failed (reason only in the audit log) |
| 401 | `sso_ntlm` | The browser offered NTLM instead of Kerberos (`message` explains the intranet-zone/SPN setup) |
| 409 | `sso_conflict` | OIDC user name clashes with a local/AD user (callback → `/login?sso_error=sso_conflict`) |
| 502 | `oidc_error` | `POST /api/sso/oidc/test`: discovery/JWKS fetch failed (proxy/TLS text) |
| 409 | `report_running` | `POST /api/reports/{id}/run` while that report runs |
| 410 | `ack_expired` | Ack token expired |
| 400 | `ack_invalid` | Ack token malformed or signature invalid |
| 409 | `ack_stale` | The problem the token names is no longer active (recovered or a newer problem) |
| 502 | `telegram_error` | (extended) "Take over this bot" / poller test failed |
| 404 | `not_found` | (kiosk) any kiosk guard failure — deliberately indistinguishable |

### [3] Type changes
```ts
type SensorType = /* … */ | "config_backup" | "new_mac";
type EventKind = /* … */ | "config_change" | "new_device";
type FeatureName = /* 2b-3 names */ | "config_backup" | "flows" | "new_device_alerts" | "sso_kerberos"
  | "sso_oidc" | "reports" | "noc" | "chat_ack";
type User = { /* 2b-3 */ source: "local" | "ldap" | "oidc" };
type TimelineKind = /* 2b-3 */ | "config" | "new_mac";
type EventCondition = { /* 2b-3 */ source: "rule" | "flap" | "backup_failed" | "host_key" | "config_changed" | "new_mac" };
type EventRule = { /* 2b-3 */ action: "warning" | "down" | "ignore" | "config_backup" };
type SshCredentials = { username: string; port: number;
  password: null; password_set: boolean;                 // write-only (secret rules as SnmpCredentials)
  private_key: null; private_key_set: boolean;
  passphrase: null; passphrase_set: boolean;
  enable_password: null; enable_password_set: boolean;
  legacy_algorithms: boolean; credentials_unreadable: boolean };
```
- `Group` gains `ssh_credentials: SshCredentials | null`. `Device` gains `ssh_credentials: SshCredentials |
  null`, `effective_ssh_source: "device" | "group" | "none"`, `effective_ssh_group_id: number | null`,
  `config_backup: "auto" | "on" | "off"`, `config_backup_driver: string | null`, `newdev_excluded_ports:
  number[]`. Create/PATCH accept them (secret rules as `snmp_credentials`).
- `GET /api/settings/public` `features` gains the 8 names; gains `sso: {"kerberos": {"auto_login": true} | null,
  "oidc": {"button_label": "Sign in with Microsoft"} | null}` (null while the feature is OFF).
- `GET /api/health` gains `"flows_rollup": "cagg" | "table" | null` and `ingest` gains `"netflow"`, `"ipfix"`,
  `"sflow"` receiver states; `"kerberos": "ready" | "unavailable" | "disabled" | "error"`.
- `Settings` gains every key of data-model § [3] Settings keys (secret rules: `kerberos.keytab` is uploaded through
  its own endpoint, never in `PUT /api/settings`; `oidc.client_secret` write-only with `client_secret_set`).
- `AlertPayload` (webhook) gains `"ack_url": string | null` (set when `chat_ack` is ON and `public_base_url` is set,
  stages problem/escalation/reminder).
- `TelegramChannelConfig` gains `ack_buttons: boolean`, `ack_link: boolean`; Teams config gains `ack_link`; email
  config gains `ack_link`.

### [3] Config backup (feature `config_backup`)
```ts
type BackupStatus = { device_id: number; device_name: string; group_id: number;
  eligible: boolean; reason: "ok" | "disabled_device" | "mode_off" | "no_credentials" | "no_driver" | "host_key_changed";
  mode: "auto" | "on" | "off"; driver: string | null; driver_source: "override" | "template" | "sys_object_id" | null;
  last_attempt_at: string | null; last_result: "ok" | "unchanged" | "failed" | "cancelled" | null;
  last_error: string | null; last_message: string | null; last_trigger: "schedule" | "manual" | "event" | "retry" | null;
  last_duration_ms: number | null; last_success_at: string | null; last_change_at: string | null;
  consecutive_failures: number; next_due_at: string | null; running: boolean; versions: number;
  host_key: HostKey | null };
type HostKey = { algorithm: string; fingerprint: string; state: "pinned" | "changed"; pinned_at: string;
  presented_algorithm: string | null; presented_fingerprint: string | null; changed_at: string | null;
  accepted_by: string | null };
type ConfigVersion = { id: number; device_id: number; version_no: number; collected_at: string;
  trigger: "schedule" | "manual" | "event" | "retry"; driver: string; sha256: string; masked: boolean;
  size_bytes: number; lines: number; added: number; removed: number; initial: boolean; is_latest: boolean };
type ConfigDriver = { id: string; name: string; version: number; confidence: "verified" | "unverified";
  mode: "shell" | "exec"; commands: string[]; match_templates: string[]; notes: string };
type BackupRunnerStatus = { running: number; queued: number; concurrency: number; next_schedule_at: string | null;
  last_schedule_at: string | null; eligible_devices: number; drivers: ConfigDriver[];
  driver_errors: { file: string; error: string }[] };
```
| Method & path | Role | Request | Response |
|---|---|---|---|
| `GET /api/config-backups` | viewer | Query `group_id` (recursive), `result`, `q` (device name), `page`, `page_size` | page of `BackupStatus` (devices with mode ≠ off or with versions), by device name |
| `GET /api/config-backups/status` | viewer | — | `BackupRunnerStatus` |
| `POST /api/config-backups/run` | admin | `{"device_ids": [3, 4]}` or `{}` (all eligible) | `202 {"queued": 24}`; audited `config.backup_all` |
| `GET /api/config-backups/search` | admin | Query `q` (2–200 chars), `regex` (bool), `group_id`, `all_versions` (bool), `since`, `until` | `{"items": [{"device_id": 3, "device_name": "sw1", "version_id": 88, "collected_at": "…", "matches": [{"line": 120, "text": " vlan 20"}], "more": false}], "truncated": false}` (≤ 200 devices, ≤ 20 lines each); `422` invalid regex |
| `GET /api/config-backups/export.zip` | admin | Query `group_id` | `200 application/zip` streamed, `Content-Disposition: attachment; filename="netmon-configs-<yyyymmdd-hhmm>.zip"`, one `<device name>.cfg` per device (latest; names sanitised, duplicates suffixed `-<id>`); audited `config.export` |
| `GET /api/devices/{id}/config` | viewer | — | `BackupStatus` |
| `POST /api/devices/{id}/config/backup` | admin | — | `202 {"queued": 1}`; `409 backup_running` (merged); `400` with `details: [{"reason": "no_credentials"}]` when not eligible |
| `GET /api/devices/{id}/config/versions` | viewer | `page`, `page_size` | page of `ConfigVersion`, newest first |
| `GET /api/devices/{id}/config/versions/{vid}` | admin | — | `ConfigVersion & {"content": string}`; audited `config.view` (at most once per user/device/10 min) |
| `GET /api/devices/{id}/config/versions/{vid}/download` | admin | — | `200 text/plain; charset=utf-8`, attachment `<device>_<yyyymmdd-hhmm>.cfg`; audited `config.export` |
| `GET /api/devices/{id}/config/diff` | admin | Query `from` (version id), `to` (version id, default latest) | `{"from": ConfigVersion, "to": ConfigVersion, "diff": string, "added": 12, "removed": 3, "truncated": false}` (unified diff text) |
| `POST /api/devices/{id}/config/host-key/accept` | admin | `{"fingerprint": "SHA256:…"}` | `200 HostKey`; `400 no_host_key_change`; `409 host_key_mismatch`; audited `config.host_key_accept` |
| `DELETE /api/devices/{id}/config/host-key` | admin | — | `204` (pin reset); audited `config.host_key_reset` |
| `POST /api/devices/{id}/ssh/test` | admin | `{"ssh_credentials": <partial, optional, merged over effective>, "driver": "<id>" \| null}` | `200 {"ok": true, "steps": [{"step": "connect" \| "host_key" \| "auth" \| "prompt" \| "setup" \| "command", "ok": true, "detail": "ssh-ed25519 SHA256:…", "ms": 120}], "driver": "hpe_comware", "output_preview": "first 20 lines, masked"}`; failure → `502 ssh_error`. Never stores a version. Allowed while the feature is OFF (explicit admin test, like the 2b-3 LDAP test endpoints); a pinned key is enforced; with no pin the key is pinned (TOFU) only while the feature is ON — while OFF the fingerprint is only reported. |

`config.changed` entities gain `config_backup` (status changes: `{"entity": "config_backup", "id": <device id>}`).

### [3] Flows (viewer; feature `flows`)
```ts
type FlowExporter = { id: number; address: string; device_id: number | null; device_name: string | null;
  state: "accepted" | "pending" | "ignored"; protocols: ("netflow5" | "netflow9" | "ipfix" | "sflow5")[];
  sampling_override: number | null; sampling_seen: number | null; first_seen: string; last_seen: string;
  records_per_s: number;               // last 5 min
  both_directions_warning: boolean; unsampled_warning: boolean };
type FlowCounters = { received_datagrams: number; records: number; stored_rows: number;
  dropped: { rate_limited: number; queue_full: number; parse_error: number; no_template: number;
    unknown_exporter: number; ignored_exporter: number; over_budget: number; unsupported: number;
    exporter_limit: number; aggregation_overflow: number; db_error: number } };
type FlowStatus = { receivers: ReceiverStatus[];           // kinds "netflow" | "ipfix" | "sflow" (2b-3 shape)
  counters_since: string; counters: FlowCounters; records_per_s: number; budget_per_s: number;
  queue: number; queue_capacity: number; templates: number; rollup: "cagg" | "table" | null;
  storage_bytes: number | null };                          // admins only (pg_total_relation_size), else null
type FlowQuery = { exporter_id?: number; device_id?: number; if_index?: number; direction?: "in" | "out";
  since?: string; until?: string; limit?: number };        // limit 1–100, default 10; range ≤ 31 d (conversations ≤ 24 h)
type FlowTopItem = { key: string;                          // ip / "tcp/443" / conversation key
  label: string;                                          // "10.0.0.9 (srv1.mnidom.net)" / "https (tcp/443)"
  ip?: string; hostname?: string | null; device_id?: number | null;     // talkers
  protocol?: number; port?: number; app?: string | null;                // applications
  src_ip?: string; dst_ip?: string; src_port?: number; dst_port?: number; in_if?: number; out_if?: number;  // conversations
  bytes: number; packets: number; flows: number; share: number };       // share of the total, 0–1
type FlowTop = { since: string; until: string; source: "minute" | "hour"; total_bytes: number;
  items: FlowTopItem[]; other_bytes: number };
type FlowSeries = { since: string; until: string; bucket_s: number; source: "minute" | "hour";
  interface: { exporter_id: number; if_index: number; name: string; speed_bps: number | null } | null;
  series: { key: string; label: string; direction: "in" | "out"; points: [string, number][] }[] };  // bits/s per bucket
```
| Method & path | Role | Request | Response |
|---|---|---|---|
| `GET /api/flows/status` | viewer | — | `FlowStatus` |
| `GET /api/flows/exporters` | viewer | Query `state` | `{"items": FlowExporter[]}` |
| `PATCH /api/flows/exporters/{id}` | admin | `{"state": "accepted" \| "ignored", "device_id": 3 \| null, "sampling_override": 4096 \| null}` | `FlowExporter`; audited `flows.exporter_update` |
| `DELETE /api/flows/exporters/{id}` | admin | — | `204` (its rows deleted in the background) |
| `GET /api/flows/top/talkers` | viewer | `FlowQuery` + `side` (`src` \| `dst` \| `both`) | `FlowTop` |
| `GET /api/flows/top/applications` | viewer | `FlowQuery` | `FlowTop` |
| `GET /api/flows/top/conversations` | viewer | `FlowQuery` (range ≤ 24 h → else 422) | `FlowTop` |
| `GET /api/flows/interfaces` | viewer | Query `exporter_id`, `since`, `until` | `{"items": [{"exporter_id": 1, "if_index": 5, "name": "Ten-GigabitEthernet1/0/49", "in_bytes": 0, "out_bytes": 0, "speed_bps": 10000000000}]}` (interfaces with traffic, by bytes desc) |
| `GET /api/flows/series` | viewer | `FlowQuery` + `split` (`none` \| `app` \| `host`, top 8 + other) | `FlowSeries` |

Requires `exporter_id` or `device_id` for interface-level queries; without both, tops are over all accepted
exporters (each flow counted once per exporter — the UI shows "may double count when several exporters see the
same traffic").

### [3] New-device alerts (feature `new_device_alerts`)
```ts
type KnownMac = { mac: string; state: "baseline" | "new" | "known" | "allowed"; vendor: string | null;
  locally_administered: boolean;
  first_seen: string; first_device_id: number | null; first_device_name: string | null;
  first_if_index: number | null; first_port_name: string | null; first_vlan: number | null;
  first_ip: string | null; first_hostname: string | null;
  last_seen: string; last_device_id: number | null; last_device_name: string | null;
  last_port_name: string | null; last_vlan: number | null;
  ip: string | null; hostname: string | null;                   // current (2b-2 MAC→IP, dns_cache)
  detected_at: string | null; ack_at: string | null; ack_by: string | null; ack_comment: string };
type AllowEntry = { id: number; prefix: string; bits: 24 | 28 | 36 | 48; vendor: string | null;
  comment: string; created_by: string | null; created_at: string };
type NewDeviceStatus = { learning: boolean; learning_until: string | null; scope_group_ids: number[];
  switches_in_scope: number; switches_baselined: number; known: number; new_unacknowledged: number;
  last_detection_at: string | null };
```
| Method & path | Role | Request | Response |
|---|---|---|---|
| `GET /api/new-devices/status` | viewer | — | `NewDeviceStatus` |
| `GET /api/new-devices` | viewer | Query `state` (repeatable), `device_id`, `group_id`, `q` (MAC prefix / vendor / IP / hostname substring), `since` (detected or first seen), `page`, `page_size` | page of `KnownMac` (`new` first, then `detected_at`/`first_seen` desc) |
| `POST /api/new-devices/acknowledge` | admin | `{"macs": ["00:1b:21:aa:00:01"], "comment": "New laptop for Dana"}` (1–500) | `{"acknowledged": 1}`; conditions removed; audited `newdev.acknowledge` |
| `DELETE /api/new-devices/{mac}` | admin | — | `204` (forget); audited `newdev.forget` |
| `POST /api/new-devices/restart-learning` | admin | — | `NewDeviceStatus` (learning until now + `newdev_learning_days`); audited |
| `GET /api/new-devices/allowlist` | viewer | — | `{"items": AllowEntry[]}` |
| `POST /api/new-devices/allowlist` | admin | `{"prefix": "3c:a8:2a", "comment": "HP printers"}` (MAC, OUI or 28/36-bit prefix; separators `:` `-` `.` or none accepted) | `201 AllowEntry`; `409 conflict` duplicate; `422` invalid |
| `DELETE /api/new-devices/allowlist/{id}` | admin | — | `204` |

Excluding ports: `PATCH /api/devices/{id}` `newdev_excluded_ports`.

### [3] Single sign-on
**Kerberos** (feature `sso_kerberos`):

| Method & path | Role | Request | Response |
|---|---|---|---|
| `GET /api/auth/sso/kerberos` | public | header `Authorization: Negotiate <b64>` (optional) | no header → `401` `WWW-Authenticate: Negotiate`, error body `{"code": "sso_challenge"}`; valid → `200 {"user": User}` + session cookie (+ `WWW-Authenticate: Negotiate <mutual>` when the context produced a token); NTLM → `401 sso_ntlm` **without** a new challenge; invalid → `401 sso_failed` without a challenge; AD lookup failures as 2b-3 (`401 invalid_credentials` mapped to `sso_failed`, `503 directory_unavailable`); throttled with the 2b-3 IP throttle (failures only) |
| `GET /api/sso/kerberos` | admin | — | `{"realms": ["MNIDOM.NET"], "auto_login": true, "keytab_set": true, "keytab_uploaded_at": "…", "keytab_entries": [{"principal": "HTTP/netmonitor.mnidom.net@MNIDOM.NET", "kvno": 3, "enctype": 18, "enctype_name": "aes256-cts-hmac-sha1-96"}], "warnings": ["rc4_only" \| "no_http_principal"], "platform_supported": true, "state": "ready" \| "unavailable" \| "disabled" \| "error", "error": null}` |
| `PUT /api/sso/kerberos` | admin | `{"realms": [...], "auto_login": true}` | same shape |
| `POST /api/sso/kerberos/keytab` | admin | `multipart/form-data` field `file` (≤ 64 KiB) | same shape; `422 invalid_keytab`; audited `sso.keytab_upload` (no content) |
| `DELETE /api/sso/kerberos/keytab` | admin | — | same shape (feature turned OFF first → else `409 conflict`) |

The `/api/sso/kerberos*` admin endpoints work while the feature is OFF (prepare before enabling); only
`/api/auth/sso/kerberos` is gated.

**OIDC** (feature `sso_oidc`; the `/api/sso/oidc*` admin endpoints work while OFF):

| Method & path | Role | Request | Response |
|---|---|---|---|
| `GET /api/auth/oidc/start` | public | Query `next` (relative path, default `/`) | `302` to the authorize endpoint + `Set-Cookie: netmon_oidc=…`; OFF → `409` JSON |
| `GET /api/auth/oidc/callback` | public | Query `code`, `state` (or `error`, `error_description`) | success: `200 text/html` (sets the session cookie, navigates to `next`); failure: `302 /login?sso_error=<code>` with code ∈ `sso_failed`, `sso_conflict`, `not_in_group`, `oidc_group_overage`, `oidc_error`, `feature_disabled`, `inactive` |
| `GET /api/sso/oidc` | admin | — | `{"tenant_id": "…", "client_id": "…", "client_secret": null, "client_secret_set": true, "client_secret_expires_at": null, "role_source": "app_roles", "admin_values": [...], "viewer_values": [...], "button_label": null, "redirect_uri": "https://netmonitor.mnidom.net/api/auth/oidc/callback"}` (`redirect_uri` from `public_base_url`, else from the request) |
| `PUT /api/sso/oidc` | admin | the writable fields (secret rules) | same shape |
| `POST /api/sso/oidc/test` | admin | — | `200 {"ok": true, "issuer": "…", "jwks_keys": 3, "latency_ms": 420}`; `502 oidc_error` |

Logout (`POST /api/auth/logout`) unchanged; the SPA sets `sessionStorage['netmon.sso.skip'] = '1'` so the login
page does not sign in again automatically. Audit: `auth.login` with `source` `ldap` / `oidc` and new field value
`method` in `changes` (`{"method": [null, "kerberos"]}`); failures `auth.login_failed` with reasons `sso_failed`,
`sso_ntlm`, `sso_realm`, `sso_conflict`, `oidc_group_overage` + the 2b-3 reasons.

### [3] Reports (feature `reports`)
```ts
type ReportSection = "availability" | "top_alerts" | "flapping_ports" | "port_errors" | "config_changes"
  | "new_devices" | "top_talkers";
type Report = { id: number; name: string; enabled: boolean; language: "en" | "he";
  scope_group_ids: number[]; scope_device_ids: number[]; sections: ReportSection[];
  schedule: "weekly" | "monthly" | "none"; weekday: number | null; month_day: number | null; send_time: string | null;
  email_to: string[]; channel_ids: number[]; next_run_at: string | null;
  last_run: { id: number; status: ReportRunStatus; finished_at: string | null } | null;
  created_at: string; updated_at: string };
type ReportRunStatus = "running" | "ok" | "partial" | "failed";
type ReportRun = { id: number; report_id: number | null; report_name: string; trigger: "schedule" | "manual";
  period_start: string; period_end: string; started_at: string; finished_at: string | null;
  status: ReportRunStatus; language: "en" | "he"; created_by: string | null;
  deliveries: { target: string; ok: boolean; error: string | null; at: string }[] };
type AvailabilityRow = { device_id: number; device_name: string; group_name: string;
  sensor_id: number | null; availability: number | null;  // percent, 2 decimals; null = n/a
  measured_s: number; downtime_s: number; outages: number; longest_outage_s: number };
type ReportData = { generated_at: string; period_start: string; period_end: string; timezone: string;
  scope_label: string; sections: ReportSection[];
  notes: { section: ReportSection; note: "feature_off" | "no_data" }[];
  availability?: { overall: number | null; devices: AvailabilityRow[]; not_measured: { device_id: number; device_name: string }[];
    groups: { group_id: number; group_name: string; availability: number | null; devices: number }[] };
  top_alerts?: { sensor_id: number; sensor_name: string; device_name: string; notifications: number; problem_s: number }[];
  flapping_ports?: { device_id: number; device_name: string; if_index: number; port_name: string; episodes: number; transitions: number }[];
  port_errors?: { device_id: number; device_name: string; if_index: number | null; port_name: string; errors: number; source: "sensor" | "snapshot" }[];
  config_changes?: { device_id: number; device_name: string; changes: number; last_change_at: string; added: number; removed: number }[];
  new_devices?: { mac: string; vendor: string | null; ip: string | null; hostname: string | null; switch_name: string | null; port_name: string | null; detected_at: string; acknowledged: boolean }[];
  top_talkers?: { hosts: FlowTopItem[]; applications: FlowTopItem[] } };
```
| Method & path | Role | Request | Response |
|---|---|---|---|
| `GET /api/reports` | viewer | — | `{"items": Report[]}` (≤ 100, by name) |
| `POST /api/reports` | admin | writable fields | `201 Report`; validation per data-model (422); `config.changed` entity `report` |
| `GET /api/reports/{id}` | viewer | — | `Report` |
| `PATCH /api/reports/{id}` | admin | subset | `Report` (`next_run_at` recomputed) |
| `DELETE /api/reports/{id}` | admin | — | `204` (runs kept with `report_id` null) |
| `POST /api/reports/{id}/run` | admin | `{"period_start": "…", "period_end": "…", "send": false}` (default: the last scheduled period; ≤ 92 days) | `202 ReportRun` (`running`); `409 report_running` |
| `POST /api/reports/preview` | admin | `{"report": <writable fields>, "period_start", "period_end"}` | `202 ReportRun` (not sent; `report_id` null) |
| `GET /api/reports/runs` | viewer | Query `report_id`, `page`, `page_size` | page of `ReportRun`, newest first |
| `GET /api/reports/runs/{id}` | viewer | — | `ReportRun & {"data": ReportData}` |
| `GET /api/reports/runs/{id}/html` | viewer | — | `200 text/html; charset=utf-8`, attachment `netmon-report-<name>-<yyyymmdd>.html` (self-contained, the email body) |
| `POST /api/reports/runs/{id}/resend` | admin | `{"targets": ["email:ops@mnidom.net", "channel:4"]}` (default: the failed ones) | `202 ReportRun` |

WS: `report.run` `{"run": ReportRun}` on start/finish (admins and viewers).

### [3] NOC kiosk (feature `noc`)
Management (admin):

| Method & path | Request | Response |
|---|---|---|
| `GET /api/kiosk-screens` | — | `{"items": KioskScreen[]}` |
| `POST /api/kiosk-screens` | writable fields | `201 {"token": "nmk_…", "url": "https://netmonitor.mnidom.net/noc#nmk_…", "item": KioskScreen}` — token shown only here; `url` null without `public_base_url` (the UI builds it from `location.origin`) |
| `PATCH /api/kiosk-screens/{id}` | subset | `KioskScreen` (open kiosk WS connections receive `kiosk.refresh`) |
| `POST /api/kiosk-screens/{id}/regenerate` | — | `{"token", "url", "item"}`; old token invalid immediately (WS closed 4403) |
| `DELETE /api/kiosk-screens/{id}` | — | `204` |

```ts
type KioskWidget = "summary" | "down_devices" | "problems" | "recent_alerts" | "map" | "group_tiles";
type KioskScreen = { id: number; name: string; enabled: boolean; token_prefix: string;
  allowed_sources: string[]; scope_group_id: number | null; scope_group_name: string | null;
  widgets: KioskWidget[]; language: "en" | "he"; show_addresses: boolean; refresh_s: number;
  last_seen_at: string | null; last_seen_ip: string | null; created_at: string; updated_at: string };
type KioskView = { screen: { name: string; language: "en" | "he"; widgets: KioskWidget[]; refresh_s: number };
  generated_at: string; timezone: string;
  summary: { device_counts: StatusCounts; sensor_counts: StatusCounts; acknowledged: number; in_maintenance: number } | null;
  down_devices: { id: number; name: string; group: string; host: string | null;      // host only with show_addresses
    status: "down" | "unreachable"; since: string | null; acknowledged: boolean }[] | null;     // ≤ 100
  problems: { sensor_id: number; sensor_name: string; device_name: string; status: Status; message: string;
    since: string | null; acknowledged: boolean }[] | null;                                     // ≤ 50
  recent_alerts: { time: string; device_name: string; sensor_name: string; old_status: Status | null;
    new_status: Status | null; message: string }[] | null;                                      // ≤ 20
  group_tiles: { id: number; name: string; status: Status; counts: StatusCounts }[] | null;     // top-level groups of the scope
  map: { nodes: { id: number; name: string; status: Status; x: number | null; y: number | null; kind: string }[];
         links: { a: number; b: number; status: "up" | "down" | "unknown" }[] } | null };
```
Kiosk access (no session; header `X-Netmon-Kiosk: nmk_…`; guard per architecture § 13.7; every failure `404
not_found`, 30 failures per IP per 5 min → `429`):

| Method & path | Response |
|---|---|
| `GET /api/kiosk/view` | `KioskView` (widgets not configured are `null`); `Cache-Control: no-store` |
| WS `/ws/kiosk` | first client message `{"type": "kiosk_auth", "token": "nmk_…"}` within 5 s → `{"type": "kiosk.ready"}`; failure → close 4401; then server messages `{"type": "kiosk.refresh"}` (debounced 5 s) and close 4403 when the screen/feature is disabled or the token regenerated |

### [3] Acknowledge from chat (feature `chat_ack`)
| Method & path | Role | Request | Response |
|---|---|---|---|
| `POST /api/chat-ack/resolve` | viewer (session) | `{"token": "a1.…"}` | `200 {"sensor": Sensor, "device_name": "core-sw", "problem_since": "…", "event_id": 991, "active": true, "acknowledgement": Acknowledgement \| null, "can_acknowledge": true}`; `400 ack_invalid`; `410 ack_expired` (`active` false when stale — not an error here) |
| `POST /api/chat-ack/acknowledge` | admin (session) | `{"token": "a1.…", "comment": "On it"}` (≤ 1000) | `200 Sensor`; `409 ack_stale`; `409 not_acknowledgeable`; already acknowledged → `200` unchanged (idempotent); audited `sensor.acknowledge` with `changes.via = [null, "link"]` |
| `GET /api/chat-links` | viewer | Query `all` (admin) | `{"items": [{"id": 1, "user_id": 2, "username": "jdoe", "provider": "telegram", "display": "@jdoe", "linked_at": "…", "last_used_at": null}]}` |
| `POST /api/chat-links/codes` | viewer (session) | `{"provider": "telegram"}` | `201 {"code": "NM-7KQ2MZ4P", "expires_at": "…", "bot_usernames": ["NetMonOpsBot"], "instructions": "/link NM-7KQ2MZ4P"}` |
| `DELETE /api/chat-links/{id}` | owner or admin | — | `204`; audited `chatack.unlink` |
| `GET /api/chat-ack/telegram/status` | admin | — | `{"pollers": [{"bot_username": "NetMonOpsBot", "channel_ids": [4], "state": "polling", "last_error": null, "updated_at": "…"}]}` |
| `POST /api/notification-channels/{id}/telegram/take-over` | admin | — | `200 {"ok": true}` (calls `deleteWebhook`); `502 telegram_error`; audited |

API tokens (2b-3) are refused on `/api/chat-ack/*` and `/api/chat-links*` (`403 forbidden`).

**Telegram message additions** (problem/escalation/reminder, `chat_ack` ON): `sendMessage` gains `"reply_markup":
{"inline_keyboard": [[{"text": "Acknowledge", "callback_data": "a1.…"}]]}` (`ack_buttons`) or `[[{"text":
"Acknowledge", "url": "https://netmonitor.mnidom.net/ack#a1.…"}]]` (`ack_link`). After a press:
`answerCallbackQuery {"callback_query_id", "text": "Acknowledged" | <reason>, "show_alert": <refusal>}` and
`editMessageReplyMarkup {"chat_id", "message_id", "reply_markup": {"inline_keyboard": [[{"text": "✓ jdoe 12:03",
"callback_data": "noop"}]]}}`. Reason texts (en/he): not linked → "Your Telegram account is not linked to
NetworkMonitor (My profile → Chat accounts)", not admin → "Only administrators can acknowledge", expired / invalid /
stale / already acknowledged.

**Teams card addition:** `actions` gains, before "Open in NetworkMonitor", `{"type": "Action.OpenUrl", "title":
"Acknowledge" | "אישור טיפול", "url": "https://netmonitor.mnidom.net/ack#a1.…"}` (`ack_link`, stages
problem/escalation/reminder). **Email:** a button-styled link "Acknowledge" with the same URL (`ack_link`).
**Webhook:** `ack_url` (see Type changes).

### [3] Timeline
`TimelineKind` gains `config` (from `config_change` events, `ref.type = "event"`; viewers see the item, the diff
link is admin-only) and `new_mac` (from `new_device` events). Available when the respective features have data.

### [3] Audit actions (feature `audit`)
`config.view`, `config.export`, `config.backup_all`, `config.host_key_accept`, `config.host_key_reset`,
`device.update` covers `ssh_credentials` (secrets as `***`), `flows.exporter_update`, `newdev.acknowledge`,
`newdev.forget`, `newdev.restart_learning`, `newdev.allowlist_create|delete`, `sso.keytab_upload`,
`sso.keytab_delete`, `sso.settings_update`, `report.create|update|delete|run|resend`,
`kiosk_screen.create|update|delete|regenerate`, `chatack.press` (Telegram, with reason), `chatack.link`,
`chatack.unlink`, `channel.telegram_take_over`; `sensor.acknowledge` gains `changes.via`.

### [3] WebSocket additions
| type | data | When / who |
|---|---|---|
| `config_backup.update` | `{"device_id": 3, "status": BackupStatus}` | attempt start/finish; all |
| `config_backup.changed` | `{"device_id": 3, "version": ConfigVersion}` | a new non-initial version; all |
| `flows.exporter` | `{"exporter": FlowExporter}` | new exporter / state change; admins |
| `new_device.detected` | `{"items": KnownMac[], "summary_count": 0}` | after a detection run with new MACs; all |
| `report.run` | `{"run": ReportRun}` | run start/finish; all |
| `chat_ack.poller` | `{"bot_username": "…", "state": "polling" \| "error", "last_error": null}` | admins |
| `config.changed` | `entity` gains `report`, `kiosk_screen`, `flow_exporter`, `mac_allowlist`, `config_backup` | as today |
| `features.changed` | 8 new names | as 2b-3 |

## [3b] Phase 3b contract

"Ask the network" assistant on Amazon Bedrock and the AI features. Table semantics:
[data-model.md § Phase 3b](data-model.md#3b-phase-3b-additions); flows and decisions: architecture.md § 14. All
earlier conventions apply. Everything is additive and **OFF by default**. **Every endpoint below is `admin`**
(viewer → `403 forbidden`) and **session only** (API tokens → `403 forbidden`), except where a row says otherwise.
While `assistant` is OFF the endpoints answer `409 feature_disabled` (`details: [{"feature": "assistant"}]`)
except the preparation endpoints listed in data-model § [3b] Feature toggles; an AI feature's own endpoints answer
`409 feature_disabled` with its own name while it is OFF, and `409 feature_idle` (`details: [{"feature":
"ai_config_diff", "needs": ["send_config_excerpts"]}]`) while ON but a dependency is missing.

### [3b] Errors
| HTTP | code | When / `message` (en; he in i18n) |
|---|---|---|
| 422 | `assistant_test_required` | Enabling (or changing a connection field while ON) without a valid `test_id`: "Run a successful Test connection first" |
| 422 | `feature_dependency` | `assistant` ON while `audit` is OFF, or `audit` OFF while `assistant` is ON (`details: [{"feature": "assistant", "requires": "audit"}]`) |
| 409 | `feature_idle` | AI feature ON but a dependency is missing (see above) |
| 409 | `assistant_busy` | This user already has a running question (`details: [{"message_id": 340}]`) |
| 429 | `assistant_busy` | Global concurrency reached ("Too many questions are running, try again shortly") |
| 429 | `assistant_cap_reached` | `details: [{"cap": "user_daily" \| "monthly_tokens" \| "monthly_cost" \| "purpose_daily" \| "chat_hourly", "limit": 50, "used": 50, "resets_at": "…"}]` |
| 409 | `conversation_full` | 40 turns reached — start a new conversation |
| 422 | `model_not_allowed` | **[3b owner]** `POST /api/assistant/ask` with a `model_id` that is not one of the allowed chat models (`details: [{"loc": ["body", "model_id"], "model_id": "…", "allowed": ["us.anthropic…", …]}]`) |
| 422 | `invalid_certificate` | Upload not parseable, not a leaf (`CA:TRUE`), no Digital Signature usage, expired, unsupported key (`details: [{"reason": "…"}]`) |
| 422 | `invalid_private_key` | Key not parseable / wrong password (no key material in the message) |
| 422 | `key_mismatch` | Key does not match the certificate |
| 502 | `bedrock_throttled` | "Amazon Bedrock is busy (throttled). Try again in a minute." |
| 502 | `bedrock_quota` | "Your AWS account's Bedrock quota is exhausted for this model." |
| 502 | `bedrock_model_access` | "This model is not enabled for your AWS account in <region> (Bedrock → Model access)." |
| 502 | `bedrock_access_denied` | "AWS denied access: the role/user lacks bedrock:InvokeModel… on this model or profile." (+ AWS text) |
| 502 | `bedrock_model_invalid` | "The model ID is not valid in <region>. Use an inference profile ID (e.g. eu.anthropic…)." |
| 502 | `bedrock_unavailable` | "Amazon Bedrock could not answer right now." |
| 502 | `bedrock_validation` | AWS message (≤ 300 chars) |
| 502 | `aws_auth_failed` | "AWS rejected the credentials." (+ AWS error type) |
| 502 | `aws_token_expired` | AWS answered `ExpiredTokenException` / `ExpiredToken`: "AWS rejected the credentials: the session token has expired. …" — paste fresh temporary credentials (access key id, secret, session token). Same retry (forced credentials refresh) and `error` state as `aws_auth_failed` |
| 409 | `aws_session_token_required` | `access_key` mode with a temporary key id (`ASIA…`) and no `session_token`: the Test fails at the `credentials` step before any AWS call ("The access key id is a temporary key (ASIA…): it needs the session token. …") |
| 502 | `aws_clock_skew` | "The server clock differs from AWS time by more than 5 minutes (NTP)." (`details: [{"server_time": "…"}]`) |
| 502 | `rolesanywhere_failed` | AWS message + hint |
| 502 | `aws_network` | "Cannot reach <host>: <exception text>" (`details: [{"host": "…", "via": "direct" \| "proxy http://…"}]`) |
| 504 | `assistant_timeout` | "The answer took too long." |
| 502 | `kb_unavailable` | Knowledge Base test failed (AWS text) |
| 409 | `walk_running` | A walk of this device is running |
| 409 | `template_exists` | `POST /api/device-templates/user` with an existing id and `overwrite: false` |
| 422 | `invalid_template` | Template YAML fails validation (`details`: validator errors `[{"path": "sensors[2].oid", "error": "…"}]`) |

In an SSE stream, errors after the stream started arrive as `event: error` with the same `{code, message, details}`.

### [3b] Type changes
```ts
type FeatureName = /* phase-3 names */ | "assistant" | "ai_explain_alert" | "ai_correlation" | "ai_alert_notes"
  | "ai_config_diff" | "ai_syslog_digest" | "ai_rule_drafts" | "ai_template_drafts" | "ai_knowledge"
  | "ai_chat_ask" | "ai_report_summary";
type AlertRule = { /* … */ ai_note: boolean };                 // create/PATCH accept it
type Report = { /* … */ ai_summary: boolean };
type ReportRun = { /* … */ ai_summary_status: "ok" | "failed" | "skipped_cap" | "timeout" | "unverified_numbers" | "disabled" | null };
type ReportData = { /* … */ ai_summary: string | null };
type TelegramChannelConfig = { /* … */ ai_ask: boolean };
type EmailChannelConfig = { /* … */ ai_note_email: boolean };
type AssistantState = "disabled" | "unconfigured" | "ready" | "degraded" | "error";
```
- `GET /api/health` gains `"assistant": AssistantState`.
- `GET /api/settings/public` `features` gains the 11 names (viewers see them too; all 3b UI is admin-only anyway).
- `GET /api/settings` gains the 3b scalar keys of data-model § [3b] Settings (except `assistant`, `assistant_kb`,
  which are only in `/api/assistant/settings` and `/api/assistant/knowledge`); `PUT /api/settings` accepts the
  scalar keys; `feature_assistant_enabled: true` / `feature_ai_knowledge_enabled: true` → `422
  assistant_test_required`.
- Audit `source` gains `"telegram"`.

### [3b] Assistant settings, test, certificate
```ts
type AssistantSettings = {
  enabled: boolean;                                   // = feature_assistant_enabled
  state: AssistantState; state_error: { code: string; message: string; at: string } | null;
  auth_mode: "roles_anywhere" | "access_key" | "default_chain";
  region: string; model_id: string; model_id_light: string | null;
  endpoint_url: string | null; control_endpoint_url: string | null;
  proxy_mode: "env" | "direct" | "custom"; proxy_url: string | null; prompt_caching: boolean;
  access_key_id: string | null;
  secret_access_key: null; secret_access_key_set: boolean; secret_access_key_unreadable: boolean;
  session_token: null; session_token_set: boolean; session_token_unreadable: boolean;  // optional, temporary (ASIA…) keys
  access_key_created_at: string | null;
  ra_region: string | null; ra_endpoint_url: string | null;
  ra_trust_anchor_arn: string | null; ra_profile_arn: string | null; ra_role_arn: string | null;
  ra_session_seconds: number;
  certificate: CertInfo | null;                       // uploaded or host files
  send_config_excerpts: boolean; redact_internal_ips: boolean; internal_cidrs: string[]; chat_redact_ips: boolean;
  prices: { currency: string; input_per_mtok: number | null; output_per_mtok: number | null;
            cache_read_per_mtok: number | null; cache_write_per_mtok: number | null };
  // [3b owner] models offered in the chat composer (writable, ≤ 8, unique ids, label ≤ 60 chars or null;
  // null/[] = default). NOT a connection field: changing it needs no new Test (a model_id change still does)
  chat_models: ChatModel[];
  chat_models_effective: ChatModel[];   // read-only: main model first (always allowed, the default), then the
                                        // configured entries; [] configured = [model_id, model_id_light if set]
  last_test: { ok: boolean; at: string; by: string; error_code: string | null } | null;
  warnings: ("cert_expiring" | "vpce_via_proxy" | "stream_buffered" | "cache_unsupported" | "access_key_old" | "key_file_mode"
             | "global_inference_profile" | "arn_accounts_differ" | "temporary_credentials")[];
  // temporary_credentials: access_key mode with a session token — they expire (hours); for the server use an
  // IAM user key, Roles Anywhere or the default chain
  // [3b owner decisions] data location per profile prefix, CSR flow
  profile: ProfileInfo; profile_light: ProfileInfo;            // classification of model_id / model_id_light
  profile_scopes: { scope: "global" | "geo" | "in_region" | "arn"; prefix: string;
                    explanation: { en: string; he: string } }[];   // the trade-off table shown in the form
  recommended: { region: "us-east-1"; profile_prefix: "us" };
  certificate_host_files: boolean;                             // NETMON_AWS_RA_CERT_FILE/_KEY_FILE in use
  csr: { subject: string; created_at: string; pem: string; key_unreadable: boolean } | null };  // pending CSR
type ChatModel = { id: string; label: string | null };      // id: model / inference-profile id ([A-Za-z0-9.:/_-]{1,256})
type ProfileInfo = { scope: "global" | "geo" | "in_region" | "arn" | null; prefix: string | null;
  geography: string | null; explanation: { en: string; he: string } | null;
  warning: "global_inference_profile" | null };              // global. → warning only, never refused
type CertInfo = { source: "uploaded" | "files"; subject: string; issuer: string; serial_hex: string; serial_decimal: string;
  not_before: string; not_after: string; key_type: "rsa" | "ec"; key_bits: number; sha256: string;
  chain: { subject: string; not_after: string }[]; uploaded_at: string | null; key_unreadable: boolean };
type AssistantTestStep = { step: "config" | "credentials" | "endpoint" | "converse" | "knowledge"; ok: boolean;
  detail: string; ms: number; error_code: string | null };
type AssistantTestResult = { ok: boolean; test_id: string | null; expires_at: string | null;
  steps: AssistantTestStep[]; via: string; endpoint_host: string; address_class: "private" | "public" | null;
  model_id: string; time_to_first_event_ms: number | null;
  usage: { input_tokens: number; output_tokens: number } | null; warnings: string[];
  error: { code: string; message: string } | null };
// step error_code: a mapped AWS code (api.md [3b] Errors) or "validation_error" (config step); warnings as in
// AssistantSettings (+ "stream_buffered", "global_inference_profile", "temporary_credentials" when the tested
// access-key settings carry a session token)
```
| Method & path | Request | Response |
|---|---|---|
| `GET /api/assistant/settings` | — | `AssistantSettings` (allowed while OFF) |
| `PUT /api/assistant/settings` | partial `AssistantSettings` writable fields + optional `"enabled": bool`, `"test_id": string` (secret rules for `secret_access_key` (≤ 256) and `session_token` (≤ 4096, base64 characters; whitespace removed): omitted/null keeps, `""` clears, string replaces; both stored encrypted, audited only as changed) | `AssistantSettings`; `422` validation; `422 assistant_test_required`; `422 feature_dependency`; audited `assistant.settings_update` (+ `feature.enable|disable`) |
| `POST /api/assistant/test` | `{"settings": <partial writable fields, optional>}` merged over stored | `200 AssistantTestResult` (also when `ok` is false — the step carries the code); `429 rate_limited` (10 per admin per 10 min); audited `assistant.test` |
| `POST /api/assistant/cert` | `multipart/form-data`: `file` (PEM cert[+chain] or `.pfx`/`.p12`, ≤ 64 KiB), `key` (PEM, optional with PEM), `password` (optional) | `200 CertInfo`; `422 invalid_certificate` / `invalid_private_key` / `key_mismatch`; audited `assistant.cert_upload` (metadata) — refused `409 conflict` when host files are configured |
| `DELETE /api/assistant/cert` | — | `204`; `409 conflict` while `assistant` is ON with `auth_mode = roles_anywhere`; audited |
| `POST /api/assistant/cert/csr` | `{"common_name": "netmonitor-bedrock.mnidom.net", "key_type": "rsa" \| "ec", "key_size": 2048 \| 3072 \| 4096 \| 256 \| 384 \| null, "organization": null, "organizational_unit": null}` | `200 {"subject", "created_at", "pem"}` — key generated and kept on the server (encrypted, never returned); replaces a pending CSR; `409 conflict` with host files; audited `assistant.csr_create`. Then `POST /api/assistant/cert` with only `file` (the issued certificate) uses the pending key (`422 key_mismatch` when it does not match) and clears the CSR |
| `GET /api/assistant/cert/csr` | — | `200 application/pkcs10` (`attachment; filename="netmonitor-bedrock.csr"`); `404` when none |
| `DELETE /api/assistant/cert/csr` | — | `204` (discard the pending CSR and its key); audited `assistant.csr_delete` |
| `POST /api/assistant/models` | `{"settings": <partial, optional>}` | `200 {"items": [{"id": "eu.anthropic.claude-…", "name": "…", "kind": "inference_profile" \| "foundation_model", "streaming": true, "status": "ACTIVE"}], "error": null}` (Anthropic only); on failure `200 {"items": [], "error": {"code", "message"}}` |
| `GET /api/assistant/status` | — (409 while `assistant` is OFF) | `{"state", "state_error", "tools": [{"name": "search_syslog", "available": false, "reason": "feature_off:syslog"}], "features": [{"name": "ai_correlation", "enabled": true, "idle_reason": null, "status_line": "2 open incidents"}], "me": {"questions_today": 3, "daily_limit": 50, "running_message_id": null}, "month": UsageMonthSummary, "chat_models": ChatModel[] /* = chat_models_effective */, "default_model_id": "us.anthropic…" \| null}` |

### [3b] Ask, stream, conversations
| Method & path | Request | Response |
|---|---|---|
| `POST /api/assistant/ask` | `{"conversation_id": 12 \| null, "question": "…", "model_id": "us.anthropic…" \| null}` (1–2000 chars; **[3b owner]** `model_id` optional, one of `chat_models_effective`, null/omitted = the main model) | `200 text/event-stream` (below); pre-stream errors as JSON: `422 model_not_allowed`, `409 feature_disabled`, `409 assistant_busy`, `429 assistant_busy`, `429 assistant_cap_reached`, `409 conversation_full`, `404` (not own conversation). The chosen model is used for every model call of that answer (tool rounds included), stored in the message's `model_id`, the `assistant.answer` audit (`model_id`) and the usage counters; the audit-title call stays on the light model; caps and limits are unchanged. Telegram `/ask` and the AI features keep the main / light models |
| `GET /api/assistant/messages/{id}/stream` | header `Last-Event-ID` (optional) | `200 text/event-stream`: replay + live; a finished message → its `done` immediately; not own → `404` |
| `POST /api/assistant/messages/{id}/cancel` | — | `200 {"status": "cancelled"}` (idempotent; a finished message → current status); audited `assistant.cancel` |
| `POST /api/assistant/messages/{id}/feedback` | `{"rating": 1 \| -1 \| 0, "comment": "…" \| null}` (0 clears, comment ≤ 1000) | `200 AssistantMessage`; audited `assistant.feedback` (rating only) |
| `GET /api/assistant/conversations` | Query `q` (title substring), `kind`, `page`, `page_size` | page of `AssistantConversation` (own), by `last_message_at` desc |
| `GET /api/assistant/conversations/{id}` | — | `AssistantConversation & {"messages": AssistantMessage[]}` (own only, else 404) |
| `PATCH /api/assistant/conversations/{id}` | `{"title": "…"}` (1–200) | `AssistantConversation` |
| `DELETE /api/assistant/conversations/{id}` | — | `204`; a running message is cancelled first; audited |
| `DELETE /api/assistant/conversations` | — | `200 {"deleted": 17}` (all own); audited |

```ts
type AssistantConversation = { id: number; kind: "chat" | "explain" | "rule_draft" | "template_draft";
  channel: "web" | "telegram"; title: string; language: "en" | "he"; message_count: number;
  ref: { sensor_id: number | null; event_id: number | null } | null;
  created_at: string; last_message_at: string };
type AssistantStep = { call_id: string; round: number; tool: string; label_key: string;   // i18n "assistant.tool.search_syslog"
  args: Record<string, unknown>;                // as the model sent them (placeholders shown translated for the admin)
  state: "running" | "done"; ok: boolean | null; rows: number | null; truncated: boolean;
  ms: number | null; error: string | null };
type AssistantMessage = { id: number; conversation_id: number; seq: number; role: "user" | "assistant";
  purpose: string; content: string;            // Markdown; links only nm:<ref>
  notes: { round: number; text: string }[]; steps: AssistantStep[];
  sources: { ref: string; label: string; url: string | null }[];   // url set only for kb/<n>
  status: "running" | "complete" | "cancelled" | "error" | "limit_reached"; error: { code: string; message: string } | null;
  notices: string[]; model_id: string | null; model_calls: number; tool_calls: number;
  usage: { input_tokens: number; output_tokens: number; cache_read_tokens: number; cache_write_tokens: number };
  cost_estimate: number | null; currency: string | null; duration_ms: number | null;
  feedback: 1 | -1 | null; feedback_comment: string | null; created_at: string; finished_at: string | null };
```
**SSE events** (`text/event-stream; charset=utf-8`, `Cache-Control: no-store`, `X-Accel-Buffering: no`; each event
`id: <n>`; `: keepalive` every 15 s):

| event | data |
|---|---|
| `start` | `{"conversation_id": 12, "message_id": 340, "user_message_id": 339, "language": "he", "redaction": false}` |
| `step` | `AssistantStep` (state `running`, then `done`); `done` adds `"refs": ["device/12", "sensor/7"]` (new refs the UI may link) |
| `delta` | `{"text": "…"}` (append to the current round's text) |
| `round_end` | `{"round": 1, "reason": "tool_use"}` (the UI moves the round's text into `notes`) |
| `notice` | `{"code": "limit_reached" \| "truncated_context" \| "retrying" \| "kb_unavailable", "message": "…"}` |
| `done` | `{"message": AssistantMessage}` (authoritative final content; links already validated) |
| `error` | `{"code": "bedrock_throttled", "message": "…", "details": []}` — followed by `done` with status `error` |

Example:
```
id: 1
event: start
data: {"conversation_id":12,"message_id":340,"user_message_id":339,"language":"en","redaction":false}

id: 2
event: step
data: {"call_id":"tooluse_1","round":1,"tool":"list_problems","label_key":"assistant.tool.list_problems","args":{},"state":"running","ok":null,"rows":null,"truncated":false,"ms":null,"error":null}

id: 3
event: step
data: {"call_id":"tooluse_1","round":1,"tool":"list_problems","label_key":"assistant.tool.list_problems","args":{},"state":"done","ok":true,"rows":2,"truncated":false,"ms":41,"error":null,"refs":["device/3","sensor/7","device/9","sensor/31"]}

id: 4
event: delta
data: {"text":"Two sensors are down: [core-sw / Ping](nm:sensor/7) since 11:58"}

id: 5
event: done
data: {"message":{…}}
```

### [3b] Tools (closed allow-list; schemas offered to the model)
Each tool is sent as `{"toolSpec": {"name", "description", "inputSchema": {"json": <JSON Schema>}}}`. All objects
have `"additionalProperties": false`; times are ISO-8601 strings; `since`/`until` default to the last 24 h;
`limit` defaults to 20. Result envelope: `{"ok": true, "tool", "rows", "truncated", "data", "untrusted_fields",
"note"}` / `{"ok": false, "tool", "error", "detail"}` (architecture § 14.4). Every item carries `ref`.

| Tool | Input (JSON Schema, compact) | Caps | Output items (projection) |
|---|---|---|---|
| `search_devices` | `{query?: string≤100, group_id?: int, status?: ("up"\|"warning"\|"down"\|"unreachable"\|"paused"\|"unknown")[], vendor?: string≤50, limit?: 1–50}` | 50 | `id, ref, name, host, group_path, status, status_counts, vendor, model, sys_descr (≤200, untrusted), in_maintenance, acknowledged_problems` |
| `get_device` | `{device_id: int}` | sensors 100 | device fields above + `sensors[] {id, ref, name, type, status, message (≤200), last_check_at, primary_value, unit}`, `problems[]`, `depends_on[] {id, ref, name, status}`, `dependents_count`, `maintenance_now` |
| `list_problems` | `{group_id?: int, include_acknowledged?: bool (true), statuses?: [...], limit?: 1–100}` | 100 | `sensor_id, ref, sensor_name, device_id, device_ref, device_name, status, message, since, acknowledged, ack_comment (≤200)` |
| `search_sensors` | `{query?: string≤100, device_id?: int, group_id?: int, type?: string, status?: [...], limit?: 1–100}` | 100 | `id, ref, name, type, device_name, status, message, last_check_at` |
| `get_sensor_history` | `{sensor_id: int, since?, until?, channel?: string}` | range ≤ 31 d, ≤ 48 buckets | `channels[] {key, unit, min, avg, max, last, buckets[[time, avg]]}` |
| `list_events` | `{device_id?, sensor_id?, group_id?, kinds?: string[], status?: string, since?, until?, limit?: 1–200}` | range ≤ 31 d, 200 | `id, ref, time, kind, device_name, sensor_name, old_status, new_status, message (≤200)` |
| `list_maintenance` | `{device_id?, group_id?, active_only?: bool, since?, until?}` | 50 | `id, ref, name, scope, target_name, next/current occurrence, recurrence summary, active` |
| `topology_neighbors` | `{device_id: int, include_stale?: bool (false)}` | 100 | `local_port, neighbor_device_id, neighbor_ref, neighbor_name (untrusted when not a device), remote_port, source, stale, last_seen` |
| `topology_path` | `{from_device_id: int, to_device_id: int}` | ≤ 16 hops | `hops[] {device_id, ref, name, status, out_port, in_port}`, `found` |
| `get_device_ports` | `{device_id: int, only_down?: bool, query?: string≤50, vlan?: int, limit?: 1–200}` | 200 | `if_index, ref, name, alias (untrusted), kind, oper, admin, speed_mbps, vlan_untagged, vlans_tagged (≤20), lag, neighbor, mac_count, errors_24h, last_change` |
| `find_mac_or_ip` | `{mac?: string, ip?: string}` (exactly one) | 50 | as `MacSearchResult` items: `mac, vendor, ip, hostname (untrusted), device_ref, device_name, port_ref, port_name, vlan, last_seen, is_uplink` |
| `availability` | `{device_ids?: int[≤100], group_id?: int, since: string, until: string}` | ≤ 92 d, 100 rows | `AvailabilityRow` fields + `ref` |
| `list_groups` | `{parent_id?: int}` | 500 | `id, ref, name, parent_id, status, device_count` |
| `get_timeline` | `{device_id: int, kinds?: string[], since?, until?, limit?: 1–100}` | ≤ 7 d | timeline items (`kind, time, summary (≤200, untrusted for trap/syslog), ref`) |
| `search_syslog` | `{device_id?, group_id?, severity_max?: 0–7, program?: string≤50, text?: string≤100, since?, until?, limit?: 1–100}` | ≤ 7 d, 100 | `id, ref, time, device_name, host (untrusted), severity, facility, program, message (≤500, untrusted)` |
| `syslog_stats` | `{device_id?, group_id?, since?, until?, by: "severity"\|"program"\|"device"\|"hour"}` | ≤ 31 d, 50 buckets | `key, count` |
| `search_traps` | `{device_id?, trap_oid?: string, text?: string≤100, since?, until?, limit?: 1–100}` | ≤ 7 d, 100 | `id, ref, time, device_name, trap_name, trap_oid, summary (≤300, untrusted)` |
| `flow_top` | `{kind: "talkers"\|"applications"\|"conversations", device_id?, exporter_id?, if_index?, direction?: "in"\|"out", side?: "src"\|"dst"\|"both", since?, until?, limit?: 1–25}` | § 13.3 ranges, 25 | `FlowTopItem` fields (`hostname` untrusted) + `ref` (`flows/<query>`) |
| `flow_interface_series` | `{device_id: int, if_index: int, since?, until?, split?: "none"\|"app"\|"host"}` | ≤ 60 points/series, 9 series | `interface, series[] {label, direction, points[[time, bps]]}` |
| `list_new_devices` | `{state?: string[], device_id?, since?, limit?: 1–100}` | 100 | `mac, vendor, ip, hostname (untrusted), switch_name, port_name, vlan, state, detected_at, ref` |
| `config_versions` | `{device_id: int, limit?: 1–20}` | 20 | `ConfigVersion` metadata + `ref` |
| `config_diff` | `{device_id: int, from_version_id?: int, to_version_id?: int}` (default: previous → latest) | 400 lines / 16 KiB | `from, to, added, removed, diff (masked, untrusted), truncated, ref` |
| `config_search` | `{text: string 2–100}` (literal) | 20 devices × 5 lines | `device_ref, device_name, version_ref, lines[] {line, text (masked, untrusted)}` |
| `list_notification_channels` | `{}` | 100 | `id, name, type, enabled` (rule drafts only) |
| `search_knowledge` | `{query: string 3–300}` | `results` ≤ 10, 1500 chars each | `ref ("kb/<n>"), title, source_url, score, text (untrusted)` |

### [3b] Converse request (what the server sends; for the mock and reviewers)
```json
{ "system": [ {"text": "<system prompt>"}, {"cachePoint": {"type": "default"}} ],
  "messages": [ {"role": "user", "content": [ {"text": "<question, redacted> (Answer in English.)"} ]},
                {"role": "assistant", "content": [ {"toolUse": {"toolUseId": "tooluse_1", "name": "list_problems", "input": {}}} ]},
                {"role": "user", "content": [ {"toolResult": {"toolUseId": "tooluse_1", "status": "success",
                    "content": [ {"json": {"ok": true, "tool": "list_problems", "rows": 2, "truncated": false,
                                           "untrusted_fields": ["message"], "data": {"items": [ … ]}, "note": null}} ]}} ]} ],
  "toolConfig": { "tools": [ {"toolSpec": {…}}, …, {"cachePoint": {"type": "default"}} ], "toolChoice": {"auto": {}} },
  "inferenceConfig": { "maxTokens": 2000, "temperature": 0.2 } }
```
Tool errors are sent with `"status": "error"` and the error envelope as `json`. Drafts use `"toolChoice": {"tool":
{"name": "propose_alert_rule"}}` on the final call.

### [3b] AI features
| Method & path | Feature | Request | Response |
|---|---|---|---|
| `POST /api/assistant/explain` | `ai_explain_alert` | `{"sensor_id": 7, "event_id": 991 \| null, "refresh": false}` (event default: the sensor's current problem event; else the latest status change) | `200 text/event-stream` as ask (`start` has the new/cached conversation id; cached → `done` immediately); `404` sensor; audited `assistant.explain_alert` |
| `GET /api/incidents` | `ai_correlation` | Query `state` (repeatable), `since`, `page`, `page_size` | page of `Incident` (newest first) |
| `GET /api/incidents/{id}` | `ai_correlation` | — | `Incident & {"members": IncidentMember[]}` |
| `POST /api/incidents/{id}/summarize` | `ai_correlation` | — | `202 Incident` (forces a new AI summary within the 5-per-incident limit; `429 assistant_cap_reached`) |
| `POST /api/assistant/config-diff/explain` | `ai_config_diff` | `{"device_id": 3, "from": 80, "to": 88, "refresh": false}` | `200 DiffExplanation` (≤ 60 s; cached per pair); `409 feature_idle`; audited `assistant.diff_explain` |
| `GET /api/assistant/config-diff/flags` | `ai_config_diff` | Query `device_id`, `from`, `to` | `200 {"flags": DiffFlag[]}` (deterministic only, no model call) |
| `GET /api/assistant/digests` | `ai_syslog_digest` | Query `page`, `page_size` | page of `DigestSummary` (newest first) |
| `GET /api/assistant/digests/{day}` | `ai_syslog_digest` | `day` = `YYYY-MM-DD` | `Digest` |
| `POST /api/assistant/digests/{day}/run` | `ai_syslog_digest` | `{"send": false}` (last 7 days only) | `202 Digest` (`status: "running"`); `429 assistant_cap_reached` (> 5 manual per day) |
| `POST /api/assistant/rule-drafts` | `ai_rule_drafts` | `{"kind": "alert_rule" \| "event_rule", "description": "…" (5–1000)}` | `200 RuleDraft` (≤ 45 s; also when no draft is possible: `draft: null` + `cannot_draft`, see below); audited `assistant.rule_draft` (`cannot_draft: "model" \| "placeholder" \| null`) |
| `POST /api/assistant/template-drafts/walks` | `ai_template_drafts` | `{"device_id": 3, "presets": ["system", "entity", "host_resources", "enterprise", "if_sample", "entity_sensor"], "oids": ["1.3.6.1.4.1.25506.2.6"]}` (≤ 8 subtrees in total) | `202 SnmpWalk`; `409 walk_running`; `422` forbidden subtree (`details: [{"oid": "1.3.6.1.6.3.15", "reason": "never_walked"}]`); audited `assistant.template_walk` |
| `GET /api/assistant/template-drafts/walks/{id}` | `ai_template_drafts` | — | `SnmpWalk` (with `varbinds` when finished, ≤ 5000) |
| `POST /api/assistant/template-drafts/walks/{id}/cancel` | `ai_template_drafts` | — | `200 SnmpWalk` |
| `POST /api/assistant/template-drafts` | `ai_template_drafts` | `{"walk_id": 5, "template_id": "acme_x100" \| null, "notes": "…" \| null}` | `200 TemplateDraft` (≤ 90 s; `yaml: ""` + `cannot_draft` when no template is possible); audited `assistant.template_draft` |
| `POST /api/device-templates/user` | **always available** (admin, not an assistant endpoint; API tokens refused) | `{"yaml": "…" (≤ 256 KiB), "overwrite": false}` | `201 {"template": DeviceTemplate, "file": "acme/acme_x100.yaml", "library": TemplateLibraryStatus}`; `422 invalid_template`; `409 template_exists`; registry reloaded, `templates.reloaded` WS; audited `device_template.create_user` |
| `GET /api/assistant/knowledge` | (prepare: allowed while `ai_knowledge` OFF; needs `assistant` settings) | — | `{"enabled": false, "knowledge_base_id": "…", "region": null, "endpoint_url": null, "results": 5, "min_score": 0, "description": "…", "last_test": {…} \| null}` |
| `PUT /api/assistant/knowledge` | same | writable fields + `"enabled"`, `"test_id"` | same shape; `422 assistant_test_required` for OFF→ON or KB field change while ON |
| `POST /api/assistant/knowledge/test` | same | `{"settings": <partial, optional>}` | `200 {"ok", "test_id", "expires_at", "results": 3, "first_source": {"title", "url"}, "ms", "error"}`; audited `assistant.kb_test` |
| `GET /api/assistant/usage` | (allowed while `assistant` OFF) | Query `month` (`YYYY-MM`, default current) | `UsageReport` |

```ts
type Incident = { id: number; state: "open" | "resolved" | "stale"; opened_at: string; last_change_at: string;
  resolved_at: string | null; title: string; title_fields: { root: string; root_status: string; count: number;
  unit: "devices" | "sensors"; status: string };
  root_device: { id: number; name: string } | null; root_candidates: { id: number; name: string }[];
  ai: { title: string; summary: string; root_guess: { device_id: number; confidence: "low" | "medium" | "high" } | null;
        generated_at: string } | null;
  member_count: number; down: number; unreachable: number; warning: number; recovered: number };
type IncidentMember = { event_id: number; sensor_id: number | null; sensor_name: string | null; device_id: number | null;
  device_name: string | null; status: string; time: string; recovered_at: string | null; is_root: boolean };
type DiffFlag = { rule_id: string; severity: "high" | "medium" | "info"; title_key: string;   // i18n diffRisk.<rule_id>
  side: "added" | "removed"; lines: number[] };
type DiffExplanation = { device_id: number; from: number; to: number; flags: DiffFlag[];
  ai: { summary: string[]; risks: { severity: "high" | "medium" | "info"; title: string; explanation: string; lines: number[] }[];
        confidence: "low" | "medium" | "high"; truncated: boolean } | null;
  status: "ok" | "failed" | "skipped"; error: { code: string; message: string } | null;
  generated_at: string; usage: { input_tokens: number; output_tokens: number }; cost_estimate: number | null };
type DigestSummary = { day: string; status: "ok" | "failed" | "skipped" | "running"; unusual_count: number;
  new_types: number; total_messages: number; generated_at: string };
type Digest = DigestSummary & { language: "en" | "he"; baseline_days: number;
  stats: { by_severity: DigestRow[]; by_program: DigestRow[]; by_device: DigestRow[]; by_hour: [number, number][];
           new_types: { signature: string; sample: string; count: number; example: string; example_ref: string }[];
           sampled: boolean };
  ai_markdown: string | null; deliveries: { target: string; ok: boolean; error: string | null; at: string }[] };
type DigestRow = { key: string; ref: string | null; count: number; baseline: number | null; ratio: number | null; unusual: boolean };
type RuleDraft = { kind: "alert_rule" | "event_rule";
  draft: Record<string, unknown> | null;       // exactly the writable fields of AlertRule / EventRule create; null with cannot_draft
  // [3b owner] "cannot draft" is a normal outcome (200, same shape, no form is opened): the model answered
  // {cannot_draft: true, reason} (source "model": the text describes no condition/event to match, or it cannot be
  // expressed with the supported fields), or the server's deterministic backstop rejected a placeholder draft
  // (source "placeholder": empty draft, a `<UNKNOWN>`-style value, a placeholder name, no statuses / no match
  // criteria on a global event rule; generic reason). No validation and no repair round then:
  // explanation "", assumptions [], validation {ok: false, errors: []}.
  cannot_draft: { reason: string; source: "model" | "placeholder" } | null;
  language: "en" | "he";                       // detected from the description as in the chat (no letters → UI language);
                                               // name, explanation, assumptions and reason are written in it,
                                               // technical values (ids, OIDs, regexes, programs, kinds) unchanged
  explanation: string; assumptions: string[];
  validation: { ok: boolean; errors: { loc: (string | number)[]; msg: string }[] };
  conversation_id: number; usage: { input_tokens: number; output_tokens: number } };
// Event-rule drafts are normalised before validation: a pattern the model put into `text_regex` (a string) or
// under `pattern` / `regex` / `message` moves to `match.text` (with `text_regex: true` for regex keys), a single
// `program` becomes `programs: [...]`, `text_regex` is coerced to a boolean.
type SnmpWalk = { id: number; device_id: number; status: "running" | "ok" | "partial" | "failed" | "cancelled";
  subtrees: string[]; counts: Record<string, number>; total: number; error: string | null;
  started_at: string; finished_at: string | null; varbinds?: [string, string, string][] };
type TemplateDraft = { walk_id: number; yaml: string; valid: boolean;
  errors: { path: string; error: string }[]; warnings: string[]; notes: string; repair_rounds: number;
  // [3b owner] as RuleDraft: the model's {cannot_draft, reason} (notes that are not about a device template, nothing
  // device-specific in the walk) or the backstop (empty YAML / `<UNKNOWN>` placeholders); yaml "", valid false
  cannot_draft: { reason: string; source: "model" | "placeholder" } | null;
  language: "en" | "he";                       // from the admin's notes (no letters / no notes → UI language): notes, reason
  conversation_id: number; usage: { input_tokens: number; output_tokens: number } };
type UsageMonthSummary = { month: string; tokens: number; token_cap: number; cost_estimate: number | null;
  cost_cap: number | null; currency: string | null; pct_of_cap: number; projection_pct: number;
  background_stopped: boolean };
type UsageReport = UsageMonthSummary & {
  by_day: { day: string; questions: number; tokens: number; cost_estimate: number | null }[];
  by_user: { user_id: number; username: string; questions: number; tokens: number; cost_estimate: number | null }[];  // user 0 = "system"
  by_purpose: { purpose: string; calls: number; tokens: number; cost_estimate: number | null }[];
  // [3b owner] per model / inference profile of the month (assistant_usage.model_id), tokens desc then id;
  // rows without any model call are left out; model_id "" = tokens counted without a model id (cost null).
  // cost: the recorded estimates (one price set for every model, at the prices valid when counted), null
  // without prices
  by_model: { model_id: string; calls: number; input_tokens: number; output_tokens: number;
              cache_read_tokens: number; cache_write_tokens: number; tokens: number; cost_estimate: number | null }[];
  top_tools: { tool: string; calls: number; errors: number; avg_ms: number }[];
  errors: { code: string; count: number }[]; feedback: { up: number; down: number };
  last_12_months: { month: string; tokens: number; cost_estimate: number | null }[] };
```
**Alert-note deliveries** (no endpoint; § 14.8.3): Telegram `editMessageText {"chat_id", "message_id", "text":
"<original html>\n\n<i>AI:</i> <note escaped>", "parse_mode": "HTML", "reply_markup": <unchanged>}`, fallback
`sendMessage {"chat_id", "text": "<i>AI note:</i> …", "reply_to_message_id": <id>}`; Teams follow-up Adaptive Card
(title "AI note: DOWN core-sw / Ping" | "הערת AI: …", TextBlock with the note, `Action.OpenUrl` to the sensor);
email reply `Subject: Re: <original subject>`, `In-Reply-To` / `References: <original Message-ID>`, plain text +
HTML with the note and "AI-generated".

**Telegram `/ask`** (no endpoint; § 14.8.9): `sendMessage` "Working on it…" / "עובד על זה…", then `editMessageText`
with `parse_mode: "HTML"`, ≤ 3500 chars + `\n\n<a href="<public_base_url>/assistant/c/<id>">Full answer</a>`.
Refusals (en/he): not linked, not admin, group chat, feature off, cap reached (with reset time), busy.

### [3b] Audit actions (feature `audit`, required by `assistant`)
`assistant.question`, `assistant.tool_call`, `assistant.answer`, `assistant.cancel`, `assistant.feedback`,
`assistant.conversation_delete`, `assistant.test`, `assistant.kb_test`, `assistant.settings_update`,
`assistant.cert_upload`, `assistant.cert_delete`, `assistant.csr_create`, `assistant.csr_delete`,
`assistant.explain_alert`, `assistant.diff_explain`,
`assistant.rule_draft`, `assistant.template_walk`, `assistant.template_draft`, `assistant.incident_summary`
(system), `assistant.alert_note` (system), `assistant.digest` (system or user for *Run now*),
`assistant.report_summary` (system), `device_template.create_user`; `alert_rule.update` covers `ai_note`,
`report.update` covers `ai_summary`. Field contents: architecture § 14.7. **Owner decision:** no question text in
the audit log — `assistant.question` / `assistant.answer` carry a **neutral audit `title`**, never the question
and never a conversation title derived from it: the light model writes a ≤ 6-word topic title after the answer
(usage purpose `title`; no IPs, hostnames, usernames, MACs, numbers or secrets — enforced again by post-processing);
`assistant.question` (written while it is not yet known), failed/cancelled answers and any failure use the generic
`"Assistant question"` / `"שאלה לעוזר"`; *Explain alert* uses `"Explain alert"`. `assistant.tool_call` has no
arguments (tool name, size, duration only).

### [3b] WebSocket additions
| type | data | When / who |
|---|---|---|
| `assistant.message` | `{"conversation_id": 12, "message_id": 340, "status": "complete"}` | a message of this user finished; **only the asking user's sockets** |
| `assistant.state` | `{"state": AssistantState, "error_code": null}` | state change; admins |
| `incident.update` | `{"incident": Incident}` | open / grow / AI summary / resolve; admins |
| `digest.ready` | `{"day": "2026-11-08", "status": "ok"}` | admins |
| `walk.update` | `{"walk": SnmpWalk}` (without varbinds) | progress every 2 s / finish; the requesting admin |
| `features.changed` | 11 new names | as 2b-3 |
| `templates.reloaded` | as 2b-2 | after `POST /api/device-templates/user` |

---

## [1.3] Release 1.3 contract

Improvements of core behaviour requested by the owner's network team after 1.2.0 in production (HPE Comware FF 5945
IRF, ArubaOS-Switch, Aruba CX). **Not feature-flagged** (owner decision): everything below is always active.
Semantics: [data-model.md § 1.3](data-model.md#13-release-13-additions); behaviour:
[architecture.md § 14](architecture.md#14-release-13-sensor-impact-important-ports-interface-selection-connected-hosts-lag-degraded-irf).

### [1.3] Type changes

```ts
type Impact = "critical" | "warning" | "none";
type Sensor = Sensor & { impact: Impact };          // GET/POST/PATCH sensor responses, sensor lists, dashboard
// GET /api/tree: every sensor node gains `impact: Impact`
// SensorCreate gains `impact?: Impact | null` (absent/null: snmp_interface -> by port importance, else "critical")
// SensorPatch gains `impact?: Impact` (also allowed on passive event sensors)

type ImportantReason = "uplink" | "lag" | "trunk" | "irf";   // [1.3.1] irf
type PortConnectedHost = { mac: string; ip: string | null; hostname: string | null; vendor: string | null;
  device_id: number | null; device_name: string | null };
type Port = Port & {                                // also PortDetail
  important: boolean;                               // important_reasons.length > 0
  important_reasons: ImportantReason[];             // in this order; trunks always listed (see settings)
  connected: { count: number;                       // MACs on the port (uplink: its mac_count, MACs not loaded)
               first: PortConnectedHost | null };   // = first_mac (null for uplinks / no MAC)
  neighbor_name: string | null;                     // primary neighbour's name (= neighbor.name)
  neighbor_mgmt_address: string | null };           // primary neighbour's management address

type ApplyTemplateRequest = { template_id: string;
  interfaces?: "important" | "none" | "all" | number[];   // default "important"; number[] = ifIndexes, ≤ 1000
  remove_unselected_interfaces?: boolean };               // default false
type ApplyTemplateResult = ApplyTemplateResult & {
  removed: { sensor_id: number; name: string }[] };       // [] unless remove_unselected_interfaces
```

`status_counts` (device / group / dashboard `sensor_counts`) keep counting raw **sensor** statuses; only the
**device status** (and therefore group status, `device_counts`, tree, kiosk, `device.status` WS) honours impact.

### [1.3] Sensor impact
| Method & path | Role | Request | Response |
|---|---|---|---|
| `POST /api/sensors/bulk-impact` | admin | `{"sensor_ids": int[] (1–1000), "impact": Impact}` | `200 {"updated": n}` — `n` = sensors whose impact actually changed. An unknown id → `422 validation_error` (nothing changed). One `sensor.update` audit row per changed sensor (`changes.impact = [old, new]`). WS: `config.changed` (`device`, `updated`) once per affected device, then `device.status` / `group.status` as usual. |

No generic sensor bulk endpoint existed before 1.3; this is a new endpoint.

### [1.3] Template apply: interface selection
`POST /api/devices/{id}/apply-template` takes the new request fields above.

- `"important"` (default): interfaces of the template's `snmp_interfaces` entries that are important
  ([data-model § Important ports](data-model.md#13-important-ports)), then the entry filters (`oper_up_only`,
  `exclude_if_types`, name regexes, `max`) as before. Without port inventory rows the server first runs a port
  inventory (only when it is effective for the device; up to ≈ 20 s inside the request). Still none: a switch
  (port inventory `on`, or class `switch`) → only LAG interfaces (ifType 161) are important; any other device
  (server, router, firewall, AP) → the 1.2 selection (as `"all"`).
- `"all"`: the 1.2 behaviour (entry filters + `oper_up_only` + `max`).
- `"none"`: no interface sensors.
- `number[]`: exactly these ifIndexes; `oper_up_only` and `max` are bypassed, the type / name filters are not.
  An ifIndex the device does not have → skipped `not_applicable` "Interface 99 not found"; one excluded by
  the template's filters → skipped `not_applicable` "Excluded by the template". Interfaces are only created
  through a template that has an `snmp_interfaces` entry.
- New interface sensors get `impact` `critical` on important ports, `warning` on the others; `critical` when
  the device has no port inventory.
- `remove_unselected_interfaces: true`: deletes this device's `snmp_interface` sensors whose ifIndex is not
  selected and reports them in `removed`. Selected = the ifIndexes of the list; `"none"` = nothing (all are
  removed); `"important"` / `"all"` = the interfaces passing the entries' type / name filters (and important,
  for `"important"`) **before** `oper_up_only` / `max`, so a down uplink keeps its sensor. With `"important"` /
  `"all"` nothing is removed when the template has no `snmp_interfaces` entry or the SNMP enumeration failed.
  Audited inside `device.apply_template` (`changes.removed`).
- A switch whose port inventory cannot run yet (no SNMP sensor so far, e.g. just imported by discovery) is
  applied in two steps inside the same request: all other entries first, then one bounded port inventory run,
  then the `snmp_interfaces` entries on its result (`created` / `skipped` are concatenated). Discovery import
  uses `"important"` the same way.

**`GET /api/devices/{id}/apply-template/interfaces?template_id=`** (admin) — the dialog's list:

```json
{ "items": [
  { "if_index": 3, "name": "Gi1/0/3", "alias": "", "oper_status": "up", "speed_bps": 1000000000,
    "kind": "physical", "important": true, "reasons": ["trunk"], "has_sensor": false, "sensor_id": null } ],
  "inventory": "ok" }
```

- `inventory: "ok"`: rows from the stored port inventory (`switch_ports`), importance as in the ports view.
  No inventory: one bounded port inventory run (≈ 20 s, only when effective; class detection first if needed);
  still none → `"missing"` and the rows come from the SNMP enumeration with `important: false` except LAG
  interfaces (`reasons: ["lag"]`). SNMP failure then → `502 snmp_error`.
- Rows are filtered by the template's `snmp_interfaces` type / name filters (all rows when it has none), sorted
  by ifIndex. `kind` is the 2b-2 `PortKind`. `sensor_id` = the existing `snmp_interface` sensor of that ifIndex
  (lowest id). Unknown `template_id` → `422`; unknown device → `404`.

### [1.3] Ports: CSV export
**`GET /api/devices/{id}/ports.csv`** (viewer): `text/csv; charset=utf-8`, UTF-8 **with BOM** (Excel / Hebrew),
`Content-Disposition: attachment; filename="ports-<device name>.csv"`, CRLF lines, spreadsheet formula guard
(a cell starting with `= + - @` is prefixed with `'`). Header:
`port,alias,status,vlan,mode,mac,ip,hostname,vendor,neighbor`. One row per port × present MAC (the MACs the
port view lists, including LAG MACs on members while `lag` is ON); a port without MACs and every uplink → one row
with empty mac/ip/hostname/vendor. `status` = `PortState`; `vlan` = the MAC's VLAN, else the port's PVID; `mode`
= `access` / `trunk` / empty; `neighbor` = `"<name> (<mgmt address>)"` (or the name only). `404` for an unknown
device.

### [1.3] Settings
| Key | Type | Default | Limits |
|---|---|---|---|
| `trunk_min_tagged_vlans` | int | 2 | 1–100 (else 422) |
| `event_linkdown_include_trunks` | bool | true | |

Both are in `GET/PUT /api/settings`; a change reloads the ingest configuration (important-port caches dropped).

### [1.3] Event conditions
`EventCondition.source` gains **`lag_degraded`** (on the device's `trap` sensor, `key` = the LAG ifIndex,
`rule_id` null, status `warning`, message `LAG BAGG1 degraded (1 of 2 members up)`).

### [1.3] Device templates
Template entries may carry `expect_from_probe` (sensor entries, only when set) and `when` (`snmp_table` entries,
only when set); `GET /api/device-templates*` returns them as stored ([data-model § 1.3](data-model.md#13-template-options)).

---

## [1.3.1] Release 1.3.1 contract

Patch release; not feature-flagged. Semantics: [data-model.md § 1.3.1](data-model.md#131-release-131-additions).

### [1.3.1] Ports
```ts
type ImportantReason = "uplink" | "lag" | "trunk" | "irf";   // order as listed
type Port = Port & {                     // also PortDetail
  irf_port: number | null };             // IRF port (1/2) of an IRF physical port (Comware), else null
type PortSources = PortSources & { irf: SourceState };   // ok | empty | unmapped | error | skipped
```
`important_reasons` contains `irf` for IRF physical ports; `GET /api/devices/{id}/apply-template/interfaces`
items list it in `reasons` (so "important" selects them). The CSV export is unchanged.

### [1.3.1] Sensor types and named value maps
```ts
type NamedValueMap = {
  title: { en: string; he: string };
  codes: { value: string; status: "up" | "warning" | "down"; mib: string;
           text: { en: string; he: string } }[];
  unknown: { en: string; he: string } };          // "{code}" = the value
// GET /api/sensor-types -> { items: SensorTypeInfo[]; value_maps: Record<string, NamedValueMap> }
// snmp_custom config gains value_map_ref?: "" | "hh3c_error_status"
//   (enum field; stored only when set, "" clears; with it value_map may be empty for value_type enum)
```
The check message of a code from the table is `<label>: <text> (<mib>, <code>)` in the server
`default_language`; the UI renders values and the value-map table from `value_maps` in the viewer's language.

### [1.3.1] Device templates
`snmp_table` entries may carry `label_format` and `rename_from` (only when set;
[data-model § 1.3.1](data-model.md#131-template-options)).

