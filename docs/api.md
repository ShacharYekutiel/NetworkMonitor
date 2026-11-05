# NetworkMonitor — API Contract (Phase 1 + Phase 2a + Phase 2b-1)

Items marked **[2a]** are the Phase 2a contract (discovery, device templates, `snmp_custom`/`snmp_disk`,
Telegram). The Phase 2a endpoints are specified in [§ Phase 2a](#2a-phase-2a-endpoints).
Items marked **[2b-1]** are specified in [§ Phase 2b-1](#2b-1-phase-2b-1-contract).

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
| `PATCH /api/sensors/{id}` | admin | Subset of `{name, config, interval_s, enabled, sort_order, channels}`. `type` cannot change. `config` is merged over the stored config. | `Sensor` |
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
| 400 | `dependency_cycle` | A dependency change (device, group, device move, bulk apply) would create a cycle. `details: [{"device_ids": [3, 7, 3]}]` |
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
changed) the problem. Email/Telegram bodies gain a row "Problem since" / "בעיה מאז" (in `settings.timezone`).

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
- `POST /api/setup` accepts optional `"timezone"` (invalid → 422). `GET /api/settings/public` gains
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
| `POST /api/topology/links` | admin | `{"a_device_id":1,"a_if_index":24,"a_port":"24","b_device_id":5,"b_neighbor_id":null,"b_if_index":null,"b_port":"uplink"}` | `201 TopologyLink` (`source: "manual"`). Exactly one of b ids (422); `a_port` defaults to the ifName of `a_if_index` when given. |
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
JWT expiry and DB `now()` are not affected. Never enabled by installers or compose.

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
