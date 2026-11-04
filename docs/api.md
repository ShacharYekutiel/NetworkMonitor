# NetworkMonitor — API Contract (Phase 1)

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
| 409 | `last_admin` | You may not delete, demote or deactivate the last active admin, or yourself |
| 415 | `unsupported_media_type` | |
| 422 | `validation_error` | Pydantic validation. `details` is a list of `{loc, msg}`. |
| 429 | `rate_limited` | Login throttling: 10 failures per 5 minutes per (IP, username) |
| 502 | `snmp_error` | An SNMP enumeration or test against the device failed (`message` gives the reason) |
| 502 | `smtp_error` / `webhook_error` | A test notification failed |
| 500 | `internal_error` | |

### Secret fields
`snmp_credentials.v3_auth_key`, `snmp_credentials.v3_priv_key` and `smtp.password` are **write-only**.

- **Responses:** the field is always `null`, and a sibling boolean reports whether a value is stored:
  - `v3_auth_key_set`
  - `v3_priv_key_set`
  - `password_set`
- **Requests:**
  - omitted, or `null` → keep the stored value;
  - `""` → clear it;
  - any other string → replace it.
- The `*_set` keys are ignored on input.

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
  community: string | null; v3_username: string | null;
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

type SensorType = "ping" | "snmp_system" | "snmp_cpu" | "snmp_memory" | "snmp_interface" | "http" | "tcp_port" | "dns";

type Event = { id: number; time: string; kind: "status_change" | "notification" | "system";
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
  `str_nullable`, `int_nullable`, `headers` (an object of string to string).
- The frontend translates field labels with the i18n key `sensorConfig.<type>.<key>`.

### Sensors

| Method & path | Role | Request | Response |
|---|---|---|---|
| `GET /api/sensors` | viewer | Query: `device_id`, `group_id` (recursive), `status`, `type`, `q` (name) | Page of `Sensor`, sorted by `device_name, sort_order, name`. A `status` filter returns the "worst first" order: severity desc, then `last_status_change_at` asc. |
| `POST /api/sensors` | admin | See below | `201 Sensor` |
| `GET /api/sensors/{id}` | viewer | — | `Sensor` |
| `PATCH /api/sensors/{id}` | admin | Subset of `{name, config, interval_s, enabled, sort_order, channels}`. `type` cannot change. `config` is merged over the stored config. | `Sensor` |
| `DELETE /api/sensors/{id}` | admin | — | `204` |
| `POST /api/sensors/{id}/run` | admin | — (no body) | `200 CheckResult`. Runs the check now and persists it (measurements, status, events, WS) like a scheduled run. Does not move the schedule. |
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
- `name` defaults to the localized type name, sent by the frontend. If it is missing, the backend uses the type
  key, or `"<if_name>"` for interfaces.

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
| `POST /api/notification-channels/{id}/test` | — | `200 {"ok": true, "message": "sent"}`. On failure: `502 smtp_error` / `webhook_error`. |

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
  - If an `Origin` header is present and does not match `Host` (same rule as the CSRF check), it closes
    with **1008**.
- **Who receives what:** every authenticated client receives every message (no subscriptions in Phase 1). Both
  viewers and admins receive messages.
- **Frames:** text frames carrying JSON `{"type": string, "ts": ISO, "data": object}`.

**Server → client messages:**

| type | data | When |
|---|---|---|
| `hello` | `{"user": User, "version": "0.1.0", "timescale": true}` | Right after connect |
| `sensor.update` | `{"id":7,"device_id":3,"status":"up","status_message":"","last_check_at":"...","status_changed":false,"channels":[{"key":"latency_ms","value":1.8,"status":"up"}]}` | After every check, scheduled or run-now |
| `event.created` | `Event` | After an event is written |
| `device.status` | `{"id":3,"status":"down","status_counts":StatusCounts}` | The device's rollup status or counts changed |
| `group.status` | `{"id":1,"status":"down","status_counts":StatusCounts}` | Sent for every ancestor group whose rollup changed |
| `config.changed` | `{"entity":"group"\|"device"\|"sensor"\|"alert_rule"\|"notification_channel","id":7,"action":"created"\|"updated"\|"deleted"}` | After any successful mutation through the API. The client should invalidate its queries (e.g. `/api/tree`). |
| `pong` | `{}` | Reply to the client's `ping` |

**Client → server:** `{"type":"ping"}`. Anything else is ignored.

**Keepalive and reconnect:**

- The server sends WS ping frames every 30 s.
- On close the client reconnects with backoff (1 s, 2 s, 5 s, then 10 s max), except after 4401, which sends it
  to login.
- After a reconnect the client refetches `/api/tree` and `/api/dashboard`.
