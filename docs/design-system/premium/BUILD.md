# Premium design — build instructions for the frontend developer

This is the approved visual direction for NetworkMonitor. It replaces the look in `docs/design-system/pages/`
(the older slate mockups). Rules from `docs/design-system/README.md` about wording, i18n and accessibility still
apply. Where they disagree on look (colour, type, layout), **this document and `prototype.html` win**.

Direction in one line: **clean premium, Apple / Tesla-app feel** — calm surfaces, big type, few colours, status
colour only where something is wrong, light and dark equally polished, Hebrew RTL first.

> **Note:** `prototype.html` is no longer kept on this branch (it is a static mockup, never shipped, and code
> scanning flagged its inline scripts). The design is implemented; the prototype itself stays on the design
> branch `claude/epic-davinci-xuv2w1` for reference: `git show origin/claude/epic-davinci-xuv2w1:docs/design-system/premium/prototype.html`.

## 1. What is in this folder

| File | What it is |
| --- | --- |
| `prototype.html` | A single self-contained, clickable prototype of **every screen**. Open it in a browser. ☰ (top bar) opens the screen menu; ☾ toggles light/dark; the back button pops like iOS. A fake incident (fw-edge-01 WAN2 down) fires ~5 s after load and recovers ~18 s later, so you can see live state changes. |
| `BUILD.md` | This file. |

The prototype is **reference only**: plain HTML/CSS/JS with demo data. Do not ship it. Rebuild with the
existing stack (React 18 + TS + Vite + Tailwind + react-query + i18next, see `frontend/package.json`). Read its
`<style>` block for exact sizes, radii and spacing — every number you need is there.

**"תצוגה (אב-טיפוס)" switch:** many screens have a small segmented control at the top left labelled
"תצוגה (אב-טיפוס)". It only flips the prototype between states (normal / empty / error / busy / wizard step…)
so you can see each one. **It is not part of the product — do not build it.** Each state it shows is one you
must build.

## 2. Screen map (prototype → route → file)

| Prototype screen (menu) | Route | File in `frontend/src/pages/` |
| --- | --- | --- |
| סקירה (home) | `/` | `Dashboard.tsx` |
| בעיות (problems) | `/events?state=open` or a dashboard panel | `EventsPage.tsx` |
| התקן (device) | `/devices/:id` | `DevicePage.tsx` |
| קבוצות (group) | `/groups/:id` | `GroupPage.tsx` |
| חיישן (sensor) | `/sensors/:id` | `SensorPage.tsx` |
| אירועים | `/events` | `EventsPage.tsx` |
| לוגים (Syslog / Traps / unknown sources / rules) | `/logs` | `LogsPage.tsx` |
| מפה | `/topology` | `TopologyPage.tsx` |
| תעבורה | `/flows` | `FlowsPage.tsx` |
| איתור MAC/IP | `/find` | `FindPage.tsx` |
| התקנים חדשים | `/new-devices` | `NewDevicesPage.tsx` |
| גיבויים + השוואה | `/config-backups` | `ConfigBackupsPage.tsx` |
| גילוי + סריקה | `/discovery`, `/discovery/scans/:id` | `DiscoveryPage.tsx`, `ScanPage.tsx` |
| תבניות | `/templates` | `TemplatesPage.tsx` |
| תחזוקה | `/maintenance` | `MaintenancePage.tsx` |
| דוחות + דוח | `/reports`, `/reports/runs/:id` | `ReportsPage.tsx`, `ReportRunPage.tsx` |
| התראות | `/notifications` | `NotificationsPage.tsx` |
| הגדרות | `/settings` | `SettingsPage.tsx` |
| משתמשים | `/users` | `UsersPage.tsx` |
| ביקורת | `/audit` | `AuditPage.tsx` |
| חשבון | `/account` | `AccountPage.tsx` |
| כניסה | `/login` | `Login.tsx`, `AuthShell.tsx` |
| התקנה | `/setup` | `Setup.tsx` |
| מסך קיר (NOC) | kiosk | `KioskPage.tsx` |
| אישור (chat ack, phone) | `/ack` | `AckPage.tsx` |

Screens added after the first prototype (menu sections in brackets):

| Prototype screen (menu) | Route | File / component |
| --- | --- | --- |
| שאל את הרשת [העוזר] | `/assistant`, `/assistant/c/:id` | `AssistantPage.tsx`, `assistant/AnswerView.tsx` |
| שימוש בעוזר [העוזר] | `/assistant/usage` | `AssistantUsagePage.tsx` |
| טיוטת כלל התראה [העוזר] | in the new-rule form: Notifications → rule, Logs → כללים ("תאר את הכלל…") | `assistant/RuleDraft.tsx` |
| טיוטת תבנית מהתקן [העוזר] | Templates → "טיוטת תבנית מהתקן" | `assistant/TemplateDraftWizard.tsx` |
| אירועים מקובצים [ניטור] | `/events` → tab "אירועים מקובצים" | `EventsPage.tsx`, `assistant/Incidents.tsx` |
| אירוע מקובץ (open from the list or the home widget) | `/incidents/:id` | `IncidentPage.tsx` |
| home widget "אירועים מקובצים פתוחים" | `/` | `Dashboard.tsx`, `assistant/Incidents.tsx` |
| הסבר (side panel / sheet) — button on sensor, incident, log lines | component, no route | `assistant/ExplainButton.tsx` |
| core-sw-01 → לשונית פורטים (table, LAG, columns, CSV, connected-device kind) | `/devices/:id` | `device/PortsTab.tsx`, `PortList.tsx`, `PortModal.tsx` |
| core-sw-01 → לשונית חיישנים (impact tags + bulk dialog) | `/devices/:id` | `SensorTable.tsx`, `SensorImpact.tsx` |
| החלת תבנית על התקן [ציוד] | from device/group: "החלת תבנית…" | `Templates.tsx`, `ApplyPorts.tsx` |
| חיישן עם מיפוי ערכים [ציוד] | `/sensors/:id` (lookup sensor) | `SensorPage.tsx`, value-map editor |
| הגדרות → חיפוש, כללי (אבטחה), העוזר, AWS | `/settings` | `SettingsPage.tsx`, `SettingsSearch.tsx`, `SecuritySettings.tsx`, `assistant/AssistantSettingsTab.tsx`, `assistant/ConnectionSettings.tsx` |
| חיבור AWS [ניהול] | `/settings` → AWS → connection | `assistant/ConnectionSettings.tsx` |
| החשבון שלי → התחברויות | `/account` | `AccountPage.tsx` |
| לוגים → תקציר | `/logs` tab; full view `/assistant/digests`, `/assistant/digests/:day` | `LogsPage.tsx`, `DigestPage.tsx`, `assistant/Digest.tsx` |
| גיבויים → השוואה עם תגי סיכון | `/config-backups` | `ConfigBackupsPage.tsx`, `assistant/DiffExplain.tsx` |
| תפריט הקשר (right click on device / group) + progress toast | component | `tree/TreeContextMenu.tsx`, `ContextMenu.tsx`, `toast.tsx` |
| home → "עריכת מפה" + map not set up yet | `/` | `Dashboard.tsx` |
| אסימוני API [ניהול] | `/tokens` | `TokensPage.tsx`, `TokensTable.tsx` |

The brief named the chat route `/ask`; the router in `main` uses `/assistant` (and `/assistant/c/:id`). Keep the
router's routes.

## 3. Build order

1. **Tokens + fonts (one PR).** Copy the `:root` block from the prototype into `src/index.css` (light on `:root`,
   dark under `prefers-color-scheme: dark` guarded by `:root:not([data-theme="light"])`, and again under
   `:root[data-theme="dark"]`). Map them in `tailwind.config` (`bg`, `card`, `ink`, `ink2`, `up`, `warn`,
   `down`, `unreach`, `paused` …). Load **Heebo** (Hebrew/body) and **Inter** (numbers, names, English) — Heebo
   for `--f`, Inter for `--fn`. Theme toggle writes `data-theme` on `<html>` and remembers it.
2. **Shell (one PR).** Top bar (logo, Dynamic Island in the middle, search / theme / ☰ / avatar), the ☰ sheet
   menu (sections: ניטור, ציוד, תפעול, ניהול, מסכים מיוחדים), iOS-style back button, page header
   (`.pg-h`: small back link, 44–56px title, grey subline, actions on the left). Page transitions: slide 30px +
   fade, 240–520ms, easing `cubic-bezier(.32,.72,0,1)`; honour `prefers-reduced-motion`.
3. **Shared components (one PR).** Grouped list (`.group` + `.row`: 52–60px rows, icon tile, title + subline,
   value, chevron), cards, segmented control, Apple switch, chips, tags, inputs, key-value list, sheet, toast,
   line chart / bars (ECharts is fine, style it flat: no grid noise, 2px line, soft area fill).
4. **Home** (section 4), **Device + switch panel** (section 5), then the remaining pages in the order of the
   table above. One PR per two or three pages.

## 4. Home screen

Top to bottom:

- Site picker chip, then the headline: **"N דורשים טיפול"** (N in `--down`) or **"הכול תקין"** when N = 0.
  Grey subline: sensors / devices count.
- **Network map hero.** The network as a tree, not decoration: `אינטרנט → חומת אש → ליבה →` the groups
  (גישה, שרתים, WiFi, חשמל/UPS, סניפים). Each node is a 64px rounded tile (radius 20, card background,
  soft shadow) with an icon, the name under it, and one short line:
  - healthy → the count ("14 מתגים"), muted;
  - problem → the problem in words ("אפליינק נפל", "דיסק מלא") plus a 2px status ring and an 8px soft halo
    in the status colour; icon takes the status colour.
  - Links: 1.5px hairlines. Healthy links carry slow moving green dots (traffic). A link to a broken group is a
    red dashed line; draw broken links first so healthy lines sit on top.
  - Clicking a node opens that group / the device / the problems list.
  - Mobile: tiles 46px, labels 11px, same layout.
  - Data: build the tree from the group hierarchy + topology (LLDP/CDP); node state = worst state inside it.
- Quick actions row (round 56px buttons with label): תקלות, בדיקה עכשיו, תחזוקה, איתור, התקנים.
- Counters row: תקלה / אזהרה / לא נגיש / תקין, big numbers in status colours.
- Two columns: "דורשים טיפול" list and "הרשת" (links to sections).

**Do not** put 3D hardware, rings/dials or decorative visuals on the home screen. The user rejected all of them.

## 5. Switch front panel (device page + Find page)

A straight-on SVG of the real front panel, no tilt, no 3D. Port to `components/SwitchPanel.tsx`
(take `device()` from the prototype script as the reference implementation).

- Body: dark brushed gradient, rack ears with screws, name (Inter 600, uppercase) and model on the left,
  vents and SYS/PWR LEDs on the right. Members of a stack are drawn one under another.
- **Ports (the important part):** no LEDs above ports. Each port is a rounded square (in a 1000-wide viewBox:
  21×21, radius 2.6, 1.6 gap, extra 7 gap every 6 columns, two rows) that is
  - **filled and framed by state** (fill / 1.1px frame / icon colour):

    | State | Fill | Frame | Icon |
    | --- | --- | --- | --- |
    | up | `#1d6b30` | `#3fe066` | `#eafff0` |
    | down (bad) | `#7a1c17` | `#ff5a50` | `#ffecea` |
    | warning | `#7a4a08` | `#ffaa2b` | `#fff4e2` |
    | free / no link | `#0b0b0d` | `#3d3e42` | — (empty socket, no icon) |

  - **shows an icon of what is connected:** computer, IP phone, WiFi AP, printer, camera, server, uplink to a
    switch (arrow up). Icons are 24-grid line icons, stroke 2.3, drawn inside the port with 3 units padding.
    The paths are in `PT` in the prototype script.
  - Selected port: extra white 1.6px frame 2.4 units outside the port.
  - Hover: native tooltip `<title>` "port · type". Click selects the port and fills the port card
    (description, VLAN, PoE, last MAC, errors, actions: בדיקה עכשיו / היסטוריה / השתקה).
  - Under the panel: a legend of the icons and the four state swatches.
- Size: on desktop the panel stage breaks out of the page column (`margin-inline: calc(50% - 50vw + 40px)`), so
  the panel is ~1300px on a 1440 screen. On mobile the panel keeps a min width of 760px and **scrolls
  sideways inside its card** (direction LTR, starts at the name side); the page itself must never scroll sideways.
- **Data needed from the backend** (tell the backend agent): per port — oper status, admin status, error state,
  and the connected device type. Type comes from LLDP/CDP capabilities (phone, AP, bridge/switch, router) and,
  failing that, from the MAC OUI / DHCP fingerprint (printer, camera, VMware/server). Unknown → computer icon.
  If the API does not have it yet, show the state colours with no icon and keep the slot for it.

## 6. Other screens — key points

- **Topology:** nodes are the same quiet tiles as home (56px, icon, name pill under it), status as a coloured
  ring, the selected node as an inverted (ink) tile. No device pictures. Mobile: the map is wider than the
  screen and pans sideways inside its card, opened centred.
- **Group page:** device rows use the normal icon tile (switch / shield for firewalls) + status dot. No pictures.
- **Login / Setup:** two halves; art side is only the app mark (76px ink tile) + "Network Monitor" + one line.
  SSO buttons (Windows, Microsoft) above the username/password form.
- **Kiosk (wall):** black, huge clock, five big counters in status colours, then the incident list full width
  with durations. No hardware pictures.
- **Ack (phone):** the chat card mock as in the prototype.
- **Logs:** segmented control Syslog / Traps / מקורות לא מוכרים / חוקים.
- **Config backups:** list + side-by-side diff (green/red lines, monospace).

## 6b. Shell and screens added later

**Pinned menu.** The ☰ sheet has a lock button next to its title. Locked: the menu stays open as a fixed 320px
side panel on the **right** (RTL start), under the top bar, without scrim; rows lose their card background, the
current screen is highlighted, and the content area shrinks next to it (`padding-inline-start: 320px + 36px`).
Unlocked: the original slide-in sheet (now also from the right) with scrim. ☰ while locked unlocks it. Remember
the choice per user (local storage is fine). Bare screens (login, setup, kiosk, ack) never show it.

**New shared components** (all in the prototype `<style>` under "v2"):
- **Table** `.tbl` — 12.5px grey headers, 1px hairline rows, hover `--card2`, selected row `--chip`, numbers
  end-aligned in Inter. Lives inside a `.group` card. Wide tables scroll inside the card, never the page.
- **Sheet** (modal) — centred card, radius 28, blurred scrim, title 24px + round × button, actions bottom-start.
  Used for every create / confirm / bulk dialog.
- **Side panel** — 440px card floating on the left (RTL end), radius 28, for "הסבר". The same content can open
  as a sheet (log lines do this).
- **Context menu** — translucent blurred card, 34px rows with icon, separators, red destructive item last; opens
  at the cursor, closes on Escape / outside click / scroll.
- **Progress toast** — bottom-left card with title, `n / total` and a bar; turns green when done; cancel button.
- **Empty / error block** — 64px icon tile (error: red tint), title 22px, one line, one or two actions.
- **Banner** — inline 16px-radius notice tinted by status (warn = limits, down = failures).
- **Wizard steps** — pills with number → ✓ when done; done steps are clickable to go back.
- **Checkbox / radio** — 20px; checked = ink fill. **Option card** (`.opt`) for radio choices with a hint.
- **Citation** `[n]` — small chip in AI text; click opens a popover (type, name, detail, "פתיחה").
- **AI tag** — small "AI ✦" chip on anything generated, plus the fine print
  "נוצר ע״י AI — ייתכנו טעויות…".

**Screen notes**
- **שאל את הרשת:** history list on the right (search, "חדשה", grouped by day, kind under each title), chat in a
  centred 820px column. User bubble = ink pill at the end side. Answer: tool steps (spinning → ✓ with rows · ms),
  then text streaming with a caret, then source chips, tokens, copy, 👍/👎. While streaming the send button
  becomes a stop square; stopping keeps the partial answer with a notice. Composer is sticky at the bottom with
  the model picker (popover upwards) and "נותרו N שאלות להיום". States: empty (hero + 6 example cards), busy
  ("Amazon Bedrock עמוס — מנסה שוב…"), error (red banner + retry + to settings), cap reached (warn banner,
  composer disabled).
- **שימוש בעוזר:** month switcher, 4 tiles (questions, tokens with bar, ≈ cost, month-end projection — warn colour
  when > 100%), daily tokens bar chart (time runs left→right, today in ink), per-user table, per-capability
  share bars, feedback line. Near-cap banner when background AI is paused.
- **Incidents:** card = status icon tile, title "root — N devices", meta line (since, alerts grouped, root +
  confidence), member chips coloured by state. Incident page: AI summary (or "אין סיכום AI" + root candidates),
  timeline, members list with "שורש" / "התאושש" tags, note that there is no bulk ack.
- **Ports tab:** LAG chips (degraded LAG in warn), filter chips, "עמודות" popover with switches, CSV. Row click
  selects the port on the faceplate and fills the port card; the card ends with the connected-device chooser
  (אוטומטי showing what was detected, then 8 kinds). A manual kind shows "ידני" in the table and changes the
  icon on the faceplate.
- **Impact:** default (קריטי) shows no tag; "אזהרה בלבד" (warn) and "ללא השפעה" (grey) tags. Checkboxes on sensor
  rows → floating ink action bar ("N נבחרו · הגדרת השפעה… · ניקוי") → sheet with the 3 options and their hints.
- **Value map:** table value / MIB name / status picker / text / remove, a fixed "אחר" row, add + save,
  validation highlights the bad rows in red.
- **Settings:** search field with a dropdown of matches (matched text highlighted, path under each result,
  "requires feature" note); picking a result switches tab, scrolls and flashes the target. Tabs כללי / העוזר /
  AWS. Assistant tab sections: status + connection test (step list), connection with the "גישת AWS" block
  (connection picker, ישיר / Assume role with role ARN + ExternalId), chat models, data sharing (with an IP
  redaction preview), limits & cost, knowledge base, capability cards with switches and "ממתין — חסר: …" tags.
- **AWS connection:** auth segmented (Roles Anywhere / access key), certificate card (subject, issuer, validity,
  days-left tag, CSR sheet → PEM + download, upload sheet with drop zone), "מדיניות IAM צפויה" code blocks with
  copy, connection test steps הגדרות → הרשאות → זהות → קפיצה לחשבון אחר; a failed step shows the error and a hint.
- **Tokens:** table (name, prefix, role, state tag, expiry, last use, revoke). Create sheet → "האסימון נוצר" sheet
  with the warning that it is shown once, the token with copy, and a curl example. Revoke asks first.
- **Home:** "עריכת מפה" button (admins) opens a sheet: mode אוטומטי / עמוד שדרה, show "אינטרנט" switch, ordered
  backbone list (move up/down, remove, add). If the map was never set up, show only the main groups in a row with
  a quiet hint and a "הגדרת המפה" link — no lines.

## 7. Rules

- Status colour is used **only** for state (up / warning / down / unreachable / paused), never for decoration.
  Always pair it with a word or icon.
- Hebrew text in Heebo, numbers, host names, MACs, IPs in Inter / mono, isolated with
  `unicode-bidi: isolate` (or `dir="ltr"` spans) so they don't flip in RTL.
- Both themes must be checked for every screen. Dark is true black background with `#1c1c1e` cards.
- Mobile 390px: no horizontal page scroll; wide things (switch panel, topology) scroll inside their own card.
- Motion: one easing (`--ease`), nothing longer than ~1s, nothing looping except the traffic dots and the
  Dynamic Island pulse; everything off with `prefers-reduced-motion`.
- Strings via `t()` in `he.json` / `en.json`; the prototype copy is the Hebrew wording to use.

## 8. Done when

- Every state shown by the prototype's "תצוגה" switch exists in the app (empty, error, busy, cap…).
- Every route in section 2 matches the prototype in light and dark, at 1440px and 390px.
- No page scrolls sideways on mobile; `npm run lint`, `npm run build` and `npm test` pass.
- Playwright screenshots of every route in both themes are attached to the PR.
