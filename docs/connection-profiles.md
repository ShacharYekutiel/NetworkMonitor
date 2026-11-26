# Connection profiles (release 1.6.0) — design notes and work split

"Connection profiles" / "פרופילי חיבור". Never call them "templates" (UI, code, i18n keys, docs): that word belongs
to the sensor/device templates.

The contract is in [data-model.md § Connection profiles](data-model.md#profiles-connection-profiles-release-160) and
[api.md § Connection profiles](api.md#profiles-connection-profiles). This file explains the decisions, splits the
work so backend, frontend and QA can work in parallel against the contract, and lists the edge cases and the open
questions. If the contract turns out wrong while implementing, report it to the architect (conventions).

## Key decisions

1. **Two generic tables, no per-kind columns.** `connection_profiles (kind, name, description, data)` and
   `connection_profile_assignments (kind, profile_id, group_id | device_id)`. A future kind (WMI, HTTP auth) is a
   code-registry entry (`conn_profiles.KINDS`); neither table changes. The composite FK `(profile_id, kind) →
   connection_profiles (id, kind)` makes a kind mismatch impossible in the database, and `ON DELETE RESTRICT`
   backs the "in use cannot be deleted" rule.
2. **Mode is derived, not stored.** Profile = an assignment row exists; manual = no assignment and the existing
   `<kind>_credentials` column is NOT NULL; inherit = neither. Existing rows therefore need no migration: upgrade
   changes nothing.
3. **The profile wins at a node that has both** (owner decision Q2): choosing a profile keeps the node's manual
   data **dormant** in its column (encrypted as before, never returned, never used while the reference exists);
   `{"K_profile_id": null}` restores it, `K_credentials: null` (inherit) discards it. No start-up repair.
4. **One resolver.** Every credential reader goes through `conn_profiles.resolve()`; the walk is the existing
   device → group → … → root walk, with "manual or profile" checked at each node.
5. **Encryption as today.** SNMP profile data uses the [1.4.2] secretbox form with owner `conn_profile:<id>`
   (encrypted-only, no plaintext copy: no older release reads the table); SSH profile data uses the [3] form, so the
   config-backup code decrypts it with the same function.
6. **Viewers see the profile name only** (embedded in group/device payloads); every profile endpoint is admin-only.
   A node in mode `profile` returns `*_credentials: null`, so no field of the profile's data reaches a viewer.
7. **Backward-compatible requests.** Old payloads keep their meaning (`*_credentials` object = manual, `null` =
   inherit); the new `*_profile_id` field selects a profile; `*_profile_id: null` alone never deletes manual data.
8. **Copy into manual is a server action** (`…/credentials/{kind}/copy-from-profile`): the client never holds the
   secrets, so it cannot copy them itself.
9. **Discovery links by reference** only when the profile itself answered on its own port; otherwise it keeps the
   2a behaviour (inherit or a manual copy). The opt-in SSH login check (Q3) links the SSH profile that logged in,
   or saves typed SSH credentials as manual, when the admin ticks "save SSH settings".
   **Owner decision (1.6.0): both "save SNMP" and "save SSH" are unticked by default** (and omitted = false in the
   API): a default-ticked box would override the import group's inheritance on every imported device. The import
   shows per host, and in a summary before the import, whether inheritance from the chosen group gives the
   credential that worked (`GET /api/discovery/scans/{id}/inheritance`, computed server-side): "differs" → tick the
   box for those hosts or assign a profile to the group; "same" → nothing needs saving.
10. **Profile tests are read-only and never pin a host key**; SSH tests send no backup command.

## Work split

Everyone: read both contract sections first. New user-visible strings need `en` and `he`. Commit style
`area: summary`. Branch `conn-profiles` (from `main`); migration `0019` with `down_revision = "0018"`
(release 1.6.0 ships directly on 1.5.1; the interface-noise migrations follow it in a later release).

### Backend developer (`backend/`)

| File | Task |
|---|---|
| `netmon/migrations/versions/0019_connection_profiles.py` | New. upgrade / downgrade exactly as data-model § Migration (`0019`), including the downgrade materialisation and the removal of `snmp_profile_ids` from discovery options. Plain DDL, idempotent. |
| `netmon/models/profiles.py`, `netmon/models/__init__.py` | New ORM models `ConnectionProfile`, `ConnectionProfileAssignment`; `DiscoveryResult.snmp_profile_id` in `models/discovery.py`. |
| `netmon/services/conn_profiles.py` | New. `KINDS` registry; **pure** `resolve(kind, chain, profiles)` and `origin(...)`; `usage(session, id)` and the counts; `assign` / `unassign` (with `FOR SHARE` on the profile row); `delete` (with `FOR UPDATE` + count); `copy_to_manual`; `reconcile_on_start` (manual-wins repair + audit); loader helpers that fetch all assignments and profiles in one query each. |
| `netmon/schemas/__init__.py` (or a new `schemas/profiles.py`) | `ConnectionProfileIn`, `ConnectionProfilePatch`, `ProfileTestIn`, `ProfileTestNewIn`, `CopyFromProfileIn` (all `extra="forbid"`); group/device create/PATCH gain `snmp_profile_id`, `ssh_profile_id`; `ScanOptions` gains `snmp_profile_ids`; response models gain the fields of api.md § Types. |
| `netmon/api/conn_profiles.py`, `netmon/app.py` | New router (admin dependency on every route), registered in the app. Tests: SNMP GET via `poller/snmp_client`, SSH via the config-backup session engine with a "no command" mode and "never pin". |
| `netmon/api/groups.py`, `netmon/api/devices.py`, `netmon/api/common.py` | The mode table of api.md § Groups and devices; `copy-from-profile` endpoints; audit change keys `snmp_mode`, `snmp_profile`, `ssh_mode`, `ssh_profile`; `config.changed` + `scheduler.reload_device` as for any credential edit. |
| `netmon/poller/snmp_client.py`, `netmon/services/sensors.py` | `resolve_credentials` / `group_chain` / `effective_credentials` become profile-aware (call `conn_profiles.resolve`); `build_context` unchanged otherwise. |
| `netmon/services/status.py`, `netmon/services/serialize.py` | `Inventory` loads assignments and profile names; `effective_snmp` / `effective_ssh` also return mode and profile; serializers add the new Group/Device fields (groups too). |
| `netmon/services/tree_move.py` | `effects` with `profile_id` / `profile_name`; a profile-only change is a change. |
| `netmon/discovery/runner.py`, `netmon/discovery/importer.py`, `netmon/api/discovery.py` | Credential order with profiles, snapshot `_profile_id` / `_origin`, `discovery_results.snmp_profile_id`; the **pure** import decision `snmp_choice(...)`; result / import response fields; option validation (kind, existence, ≤ 8); saved discovery profiles count as uses. |
| `netmon/topology/refresh.py`, `netmon/ingest/pipeline.py` (`rebuild_sources`), `netmon/ingest/trap_receiver.py`, `netmon/assistant/features/templates.py`, `netmon/ports/runner.py`, `netmon/templates/apply.py`, `netmon/templates/selection.py`, `netmon/configbackup/sysoid.py` | Use the profile-aware resolver (those that already call `effective_credentials` only need a test). The ingest pipeline adds every SNMP profile's community and v3 user to the known communities / v3 users, and per-device effective users via the resolver. |
| `netmon/configbackup/service.py`, `netmon/configbackup/credentials.py` | `load_contexts` resolves SSH through profiles (one load of groups, assignments and SSH profiles); `unreadable()` for profile data; `ssh/test` merges over the profile-aware effective credentials. |
| `netmon/services/secretbox.py`, `netmon/services/rekey.py` | Owner `conn_profile:<id>`; encrypted-only form for profile rows in every `transform_all` mode (untouched in the 1.4.1 form); `rekey` covers `connection_profiles.data`. |
| `netmon/runtime.py` | `config_changed("connection_profile", …)` also marks ingest sources dirty and config-backup / new-device runners dirty, like `device` / `group`. `reconcile_on_start` after migrations. |
| `docs/production.md` | A `[1.6.0] Upgrade and rollback` paragraph (nothing to do on upgrade; rollback behaviour per Q1). |

**Backend tests** (`backend/tests/test_conn_profiles_units.py`, `test_conn_profiles.py`, `test_conn_profiles_discovery.py`):

- Units (pure, exhaustive): `resolve` for every combination per level (device manual / profile / inherit × group
  manual / profile / inherit × parent levels × fallback), SNMP default vs SSH none, both present → manual, a cycle /
  depth guard; `origin` output for groups and devices; `snmp_choice` decision table (rules 1–4 incl. port override,
  deleted profile, import group inheriting the same profile); discovery credential order and the skip of the
  profile that already supplied credential 0; the request-mode table of api.md (every row).
- API: CRUD; name unique per kind, case-insensitive (same name in both kinds is allowed); `kind` immutable; secrets
  never returned (list, get, usage, group/device, audit, WS) and `*_set` flags; `credentials_unreadable` after a key
  change; viewer and viewer-token `403` on every profile endpoint and on assign / copy; viewer sees names only.
- Reference semantics: assign to group → `build_context` of a device two levels below gets the profile's
  credentials; PATCH the profile → the next `build_context` returns the new values without any reload call.
- Kind mismatch: API `422 profile_kind_mismatch` (group, device, discovery options); a direct SQL insert of a
  mismatched assignment fails on `fk_cpa_profile_kind`.
- Delete: `409 profile_in_use` with counts for group, device and discovery-profile uses; after removing them `204`;
  deleting a group cascades its assignment, after which the profile can be deleted; `usage` lists and `truncated`.
- Backward compatibility: PATCH without new fields keeps a profile; `snmp_credentials` object → manual, reference
  gone; `null` → inherit; `snmp_profile_id: null` on a manual node changes nothing; both fields → `422`; the full
  pre-1.6 group/device test suite passes unchanged except for the new response keys.
- Copy from profile: node becomes manual with the profile's secrets (an SNMP poll against snmpsim works), the
  ciphertext is re-sealed for the node owner, a later profile edit does not affect the node.
- Tests of a profile: SNMP against snmpsim (ok; wrong community → `502 snmp_error`; v3 wrong key), SSH against
  `tests/ssh_sim.py` (ok; wrong password → `ssh_error` step `auth`; device with a pinned key that differs → step
  `host_key` fails; no pin is ever written; no command but the driver's session commands is sent); `host` rules
  (`422` for a CIDR); unsaved-profile test; audit row `connection_profile.test`.
- Consumers: trap v3 user from an SNMP profile is accepted after the profile is assigned (ingest rebuild);
  config-backup eligibility and `load_contexts` credentials via an SSH profile; tree-move preview with
  profile fields; topology refresh / port inventory use profile credentials (one test each).
- Discovery SSH (Q3): opt-in default off (no SSH without the options); stop at the first success; skipped when the
  port is not open; no command executed and no host key stored (against `tests/ssh_sim.py`); result recording
  (`ssh_attempted`, `ssh_ok`, `ssh_credential`, `ssh_profile_id`); import links the SSH profile / saves typed
  credentials as manual when `save_ssh: true`; omitted / `false` → inherit; option validation (kind, existence, ≤ 3); an SSH
  profile in a saved discovery profile is "in use"; typed SSH credentials never returned; `ssh_choice` table.
- Discovery: scan with `snmp_profile_ids` (order, result `snmp_credential: "profile"`, `snmp_profile_id`), import
  links the device to the profile (`save_snmp: true`; omitted → inherit); import with a different `snmp_port` →
  manual copy; inheritance check `same` / `differs` (profile id, value, port, child group, SSH by decrypted login); import into a group that
  inherits that profile → inherit; profile deleted between scan and import → manual copy; a saved discovery
  profile makes the profile "in use".
- Dormant manual data (Q2): manual → profile keeps the column (`K_has_saved_manual: true`, `K_credentials: null`
  in the response, nothing of it in any response / audit value); resolution uses the profile; `{"K_profile_id":
  null}` restores manual (SNMP poll works with the old values); `K_credentials: null` clears both; `K_profile_id` +
  `K_credentials: null` discards; `copy-from-profile` overwrites; rekey re-encrypts dormant data; downgrade writes
  the profile value over dormant data.
- `netmon secrets rekey` re-encrypts profiles; `transform_all` modes leave them encrypted-only.
- Migrations (both DB modes, `test_migrations.py`): head `0019`; upgrade leaves every effective credential
  byte-identical; downgrade materialises references into manual columns (polls still work on 0018 data) and stops
  on an unreadable profile without changing anything.

### Frontend developer (`frontend/`)

| File | Task |
|---|---|
| `src/api/types.ts` | `CredentialKind`, `CredentialMode`, `ConnectionProfile`, `CredentialOrigin`, `ProfileUsage`, `ProfileTestResult`; Group/Device new keys; `ScanOptions.snmp_profile_ids`; `DiscoveryResult` additions; tree-move effect additions. Exactly as api.md. |
| `src/api/endpoints.ts` (or new `src/api/profiles.ts`) | The profile endpoints and the two `copy-from-profile` calls. |
| `src/api/ws.ts` | `config.changed` entity `connection_profile` → invalidate profile queries **and** groups, devices, tree. |
| `src/lib/credentials.ts` (new, pure, unit-tested) | `buildCredentialPatch(kind, initialMode, mode, profileId, manualForm, manualTouched)` implementing the request table: sends `K_credentials` only when the user changed that kind; `originText(origin, t)`. |
| `src/components/CredentialModePicker.tsx` (new) | Per kind: segmented Inherit / Profile / Manual; profile select (filtered by kind, admin only); manual editor (existing `SnmpEditor` / `SshEditor`); the origin line ("Inherited from group X · profile Y"); "Copy into manual" button when mode = profile; "Switch back to saved manual settings" when `K_has_saved_manual` (sends `{"K_profile_id": null}`); confirm dialog when switching to inherit discards saved manual data. |
| `src/components/EntityForms.tsx`, `src/components/SnmpEditor.tsx`, `src/components/configbackup/SshEditor.tsx` | Use the picker for SNMP and SSH on group and device forms; editors reusable inside the profile form (no own submit logic). |
| `src/pages/DevicePage.tsx`, `src/pages/GroupPage.tsx` | Show mode / origin for both kinds (viewer: names only, never a field of profile data). |
| `src/components/profiles/ConnectionProfilesTab.tsx`, `ProfileForm.tsx`, `ProfileUsageDialog.tsx`, `ProfileTestDialog.tsx` (new) | Settings tab "Connection profiles": table (name, kind, description, used by, devices affected), create / edit (editing a used profile shows "affects N devices"), delete (blocked → "used by" dialog with links), test (device picker or address; SNMP shows sysName/sysDescr, SSH shows steps). |
| `src/lib/features.ts`, `src/pages/SettingsPage.tsx`, `src/components/SettingsSearch.tsx` | `SETTINGS_TABS` gains `connectionProfiles` (always present, admin only, no toggle); search entries. |
| `src/components/discovery/ScanForm.tsx`, `src/pages/ScanPage.tsx`, `src/pages/DiscoveryPage.tsx` | Profile multi-select (SNMP profiles, ordered) next to extra communities / v3; opt-in SSH section (SSH profiles ≤ 3, ordered, and/or typed SSH credentials) with the lockout warning; result columns show the SNMP profile and the SSH login result; import has "save SNMP / SSH settings that worked" (per row and a tri-state per column, **unticked by default**) with the per-host and summary inheritance hint; import result shows the links. |
| Tree-move confirmation (`src/components/treemenu/…`) | Show profile names in the before/after effects. |
| `src/i18n/locales/en.json`, `he.json` | `profiles.*`, `credMode.*`, `errors.profile_in_use`, `errors.profile_kind_mismatch`, audit action names. Hebrew: "פרופיל חיבור" / "פרופילי חיבור"; modes "ירושה" / "פרופיל" / "ידני". |

RTL rules as usual (`ms-/me-`, hosts and IPs in `dir="ltr"`, `<bdi>` for names). Vitest: `credentials.ts` (every
row of the request table), picker rendering per mode, i18n parity.

### QA developer (`tests/`)

E2E (`tests/e2e/specs/94-connection-profiles.spec.ts`, `95-connection-profiles-discovery.spec.ts`), each in he
(RTL) and en where UI text matters, against the snmpsim / SSH simulators:

1. Admin creates an SNMP v2c and an SNMP v3 profile and an SSH profile in Settings; after save the secret fields
   show "set", never a value; duplicate name in the same kind is refused, the same name in the other kind works.
2. Assign the SNMP profile to a group; a device two levels below shows "Inherited from group X · profile Y"; its SNMP
   sensors go up.
3. Edit the profile's community to a wrong value → the device's SNMP sensors go down at their next check; fix it →
   up (reference semantics, no device edit).
4. Device overrides: manual on the device wins over the group's profile; switch the device to another profile;
   back to inherit.
5. Delete an in-use profile → blocked, "used by" lists the group, the device and the discovery profile with links;
   after removing the uses, delete works.
6. Copy from profile into manual on a device; then change the profile; the device keeps working with the copied
   values and shows "Manual".
7. Test connection: SNMP profile against a device and against an address (ok and failure); SSH profile against the
   SSH simulator (ok, wrong password); no host key appears pinned afterwards.
8. Discovery: scan with an SNMP profile selected (and no typed community); the result shows the profile; import →
   the device is in mode "profile" with that profile.
9. Config backup: an SSH profile on a group makes its devices eligible; a backup runs with it.
10. Viewer: no Connection profiles tab; group/device pages show only the profile name; direct API calls get 403.
11. Upgrade check (integration, `tests/integration`): on a database with 1.5 data, after the upgrade every group /
    device shows "Manual" or "Inherit" exactly as before, and polls are unchanged.
12. Tree move between groups with different profiles: the preview names both profiles.
13. Mobile width and RTL layout of the profiles tab and the picker.

QA also reviews the backend test list above for gaps and adds `tests/integration` checks for the downgrade
(`netmon migrate` to the previous head) materialisation.

## How-to: one SNMP profile for a group

(For the owner / network engineer, in Hebrew. Checked on 1.6.0 against simulated switches whose SNMP sensors were
down with "No SNMP response received before timeout" because the built-in default `v2c / public / 161` did not fit.)

<div dir="rtl">

**הגדרת פרופיל SNMP אחד לקבוצה (לכל ההתקנים שמתחתיה)**

1. **הגדרות ← פרופילי חיבור ← "הוספת פרופיל".** סוג: SNMP. שם ברור (למשל "SNMP סניפים"), גרסה, Community (או
   משתמש v3 עם מפתחות האימות/הצפנה), פורט (בדרך כלל 161). הסודות נשמרים מוצפנים ולא מוצגים שוב ("שמור").
2. **"בדיקת חיבור"** (כפתור ההפעלה בשורת הפרופיל): בוחרים אחד מההתקנים ← "הרצת בדיקה". בדיקה לקריאה בלבד
   (GET אחד של sysName / sysDescr / sysObjectID); "החיבור תקין" עם שם ההתקן מאשר שהפרטים נכונים. לא נשמר דבר.
3. **פותחים את הקבוצה העליונה ← "עריכה" ← באזור SNMP בוחרים "פרופיל"** ובוחרים את הפרופיל ← "שמירה".
   אין צורך לגעת בהתקנים: כל התקן שמתחת לקבוצה (גם בתתי-קבוצות) שמוגדר "ירושה" מקבל את הפרופיל.
   התקן או תת-קבוצה עם הגדרה "ידני" או פרופיל משלהם ממשיכים להשתמש בהגדרה שלהם.
4. **בדיקה:** בדף של התקן שמתחת לקבוצה מופיעה השורה
   "SNMP: בירושה מהקבוצה ‹שם הקבוצה› · פרופיל ‹שם הפרופיל› (v2c)". בהגדרות ← פרופילי חיבור, בעמודה
   "התקנים מושפעים", מופיע מספר ההתקנים שמקבלים את הפרופיל.
5. **מתי החיישנים חוזרים לתקין:** אין צורך באתחול. כל חיישן משתמש בפרטים החדשים בבדיקה המתוזמנת הבאה שלו,
   כלומר תוך מרווח הבדיקה של החיישן (בדרך כלל 60 שניות; חיישני סטטוס של תבניות עד 300 שניות). כדי לא לחכות:
   בעץ או ברשימת ההתקנים, קליק ימני על התקן ← "הרצת כל החיישנים עכשיו", או בחיישן בודד "הרצה עכשיו".
   (אין עדיין פעולה כזו לקבוצה שלמה.)
6. **שינוי מאוחר יותר:** עריכת הפרופיל (למשל Community חדש) חלה מיד על כל ההתקנים שמשתמשים בו, בבדיקה הבאה.
   פרופיל שנמצא בשימוש אי אפשר למחוק; "בשימוש אצל" מראה איפה.

</div>

## Edge cases

- **Poller cache invalidation.** The poller has no credential cache: `build_context` resolves credentials from the
  database at every check, so a profile edit applies at each sensor's next run. Caches that **do** depend on
  credentials and must be refreshed on `connection_profile` changes: the ingest attribution / trap v3 users and
  communities (`ingest.mark_sources_dirty`), config-backup eligibility (`configbackup.mark_dirty`), new-device
  runner (`newdev.mark_dirty`). The SNMP v3 engine cache is keyed by a fingerprint of user, levels, protocols
  **and keys**, so changed v3 keys get a fresh engine automatically (old ones age out by LRU). A running config
  backup session or a running discovery scan keeps the credentials it started with.
- **Config backup with SSH profiles.** Eligibility (`no_credentials`), `load_contexts`, the device `ssh/test`
  (partial override merged over the profile-aware effective credentials) and the backup session all use the
  resolver. An unreadable SSH profile makes its devices ineligible, not "auth failed". Host keys stay per device
  (pinned by backups / device tests, never by a profile test).
- **Export / backup / restore.** `pg_dump` and `ops/restore.sh` include the tables; a restore with another
  `secret.key` shows `credentials_unreadable` on the profile and fixing the profile fixes every user of it;
  `netmon secrets rekey` covers profiles; plaintext tooling never writes profile plaintext. There is no other
  export of credentials (by design).
- **Audit entries.** Profile create / update / delete / test, assignments as group/device change keys, copy as
  `reason: "copy_from_profile"`. Only while the `audit`
  feature is on, like every other audit row. Secrets only as `"***"`.
- **Kind mismatch.** Rejected by the API (`422 profile_kind_mismatch`) and impossible in the database (composite
  FK). `kind` cannot be changed after create (a profile in use would otherwise silently become a mismatch); create a
  new profile instead.
- **Renames.** References are by id, so a rename changes no behaviour; uniqueness is per kind, case-insensitive (a
  case-only rename of the same row is allowed). Audit rows keep the old name as text; the UI refreshes via
  `config.changed` (`connection_profile`). Discovery results show the current name.
- **Moves.** A moved device in mode `inherit` follows its new ancestors' profile at once (tree-move preview lists
  it); its own reference moves with it.
- **Deleted group / device** cascades its assignment rows; the profile becomes deletable when nothing else uses it.
- **Switching manual → profile keeps the manual data dormant** (owner decision Q2); switching back to manual with
  `{"K_profile_id": null}` restores it; inherit discards it (the UI confirms).
- **Discovery with a port override** never links a profile whose own port differs (the device would not work).
- **Profile edited between scan and import:** the device is still linked (reference semantics: the admin's latest
  profile values apply); only a deleted profile falls back to a manual copy of what answered.
- **Old clients** that always send `snmp_credentials` turn a profile node into inherit/manual on save (api.md
  compatibility notes); the 1.6 UI sends a kind's credentials only when the user changed it.

## Owner decisions

- **Q1 — Container rollback to 1.5 without downgrading the database: option (a), accepted.** The supported rollback
  after profiles are in use is the pre-deploy dump restore or `netmon migrate` down to the previous head, whose
  downgrade copies every profile into manual credentials. An image-only rollback ignores the new tables: a node in
  mode `profile` behaves there as its column says — with dormant manual data (Q2) it uses that data, without it as
  `inherit`. No hidden copy is written on profile edits.
- **Q2 — A node's manual credentials when a profile is chosen: kept dormant** (changed from the recommendation).
  The column keeps the data (encrypted, never returned, unused while the reference exists, covered by rekey);
  `{"K_profile_id": null}` restores it ("switch back to the saved manual settings"), `K_credentials: {…}` replaces
  it, `K_credentials: null` (inherit) discards it (the UI confirms), `copy-from-profile` overwrites it. Responses
  carry `K_has_saved_manual`. Every switch is audited.
- **Q3 — Discovery: SNMP profiles and typed SNMP credentials as designed (both always available); plus an opt-in
  SSH login check** (reverses the recommendation): `ssh_profile_ids` (≤ 3) and typed `ssh_credentials` in the scan
  options; login and disconnect only; import offers "save the SSH settings that worked" (unticked by default
  since the 1.6.0 owner decision; see Key decisions 9), assigning the profile or saving typed credentials as manual. No SSH profile is assigned to a
  group by discovery. Operational risk (account lockout / security alerts after failed logins) documented and
  shown in the UI.
