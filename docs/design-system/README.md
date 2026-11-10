NetworkMonitor is a network monitoring console in the family of PRTG and HPE IMC: groups → devices → sensors, polled over Ping, SNMP, HTTP, TCP and DNS, with a switch front-panel view, LLDP/CDP topology, MAC/IP search, discovery, templates, maintenance windows and escalating notifications. The UI is bilingual — Hebrew (RTL) and English — and themed dark (NOC wall screens) and light. Every screen answers one question first: **what is broken right now, and where?**

## Content fundamentals

- **Status words are fixed.** Down, Warning, Unreachable, Unknown, Up, Paused — in Hebrew תקלה, אזהרה, לא נגיש, לא ידוע, תקין, מושהה. Never synonyms ("Critical", "Offline", "OK").
- **Severity order is fixed:** Down › Warning › Unreachable › Unknown › Up › Paused. Lists, counters, tree pills and bars always sort worst first.
- **Sentence case** for titles, buttons and labels: "Add device", "Apply template", "Save thresholds". No Title Case, no trailing periods on buttons.
- **Say what happened and what it means** in messages: "No reply (4/4 lost)", "In utilization 81% · above 80%", "Parent access-sw-12 is down". Values come with units; thresholds with the comparison.
- **Address the user plainly** ("Enter a MAC or an IPv4 address"); Hebrew uses the plural imperative (הזינו, התחברו). No exclamation marks, no emoji.
- **Technical identifiers stay LTR and mono** — IPs, MACs, OIDs, port names (`Gi1/0/17`), hostnames, times — including inside Hebrew sentences (wrap them in `<bdi>` / `dir="ltr"`).
- Separate facts with a middle dot: "4 groups · 38 devices · 412 sensors".

## Visual foundations

Calm, dense and professional: an operator looks at this all day. Personality comes from a few deliberate choices, not decoration.

**The bezel.** The header and the nav rail are `chrome` in **both** themes (`chrome-ink`, `chrome-muted`, `chrome-border`). The header carries the **health strip** — Down · Warn · Unreach · Paused · Up as a dot and a count — so the state of the network is visible from every page, plus MAC/IP search, a Live dot and a clock. The active rail item gets `nav-active` with its icon in `accent`.

**Colour.** Warm graphite neutrals (dark) and warm paper (light), never blue-grey. One brand colour, `accent` (a muted green), for the primary button, the active rail icon, focus, links (`accent-ink`), the active tab and the selected tree marker — sparingly. Status colours are only for status, each with a `-soft` ground: `status-up` (mint), `status-warning`, `status-down`, `status-unreachable`, `status-unknown`, `status-paused`. Status is never colour alone — always the word and the glyph.

**Type.** `sans` (Barlow; Hebrew falls back to Heebo) for all interface text, in sentence case — no uppercase labels. `display` (Barlow Condensed) only for numerals: counter tiles (`metric`), health cells (`metric-sm`), the header strip (`metric-xs`). `mono` (JetBrains Mono) for IPs, MACs, OIDs, times and numeric columns.

**Panels.** `surface` with a `border` hairline, `radius-lg` (4px), no shadow, header in `panel-title`. Counter tiles join into one strip divided by hairlines; tiles with problems take their `status-*-soft` ground and a coloured numeral. Status bars are plain segments with 2px gaps.

**Controls.** 34px buttons and inputs (26px small), `radius-md`. Inputs sit on `surface-sunken` with a `border-strong` outline; labels in `caption`. Segmented controls lift the selected option onto `surface-raised`. Focus is a 2px solid `focus` ring, 2px offset.

**Effects.** None: no glow, no gradients, no textures, no blinking. Only floating layers (menus, modals, toasts) get `shadow-pop`. Motion is limited to hover colour changes and spinners.

## Iconography

The product's own icon set (`assets/Icons`, from `icons.tsx`): 24×24, 2px round stroke, no fill, `currentColor`, inline SVG. Sizes 16 (default), 14 (buttons, rows), 20 (page titles, map nodes). Device class → icon: switch, router, wifi, server, nas, printer, desktop, question. Status glyphs: check (Up), alert (Warning), x (Down), link (Unreachable), question (Unknown), pause (Paused). Arrow-like icons (logout, chevrons) mirror in RTL. No emoji, no icon fonts.

The mark (`assets/Logos/netmon-mark.svg`) is the pulse-in-a-square favicon; it sits alone at 28px in the rail-width corner of the header, followed by the wordmark "Network Monitor" in `sans` 600 with "Monitor" in `chrome-muted`. Icons are drawn at stroke 1.75 (20px in the rail).

## Using the kit

`kit.css` is a static class kit (`nm-*`) written only in tokens (it also loads the Google Fonts): shell, buttons, forms, status, cards, tables, tabs, banners, modal, toast, chart, faceplate, topology and auth layouts. The component cards document each part; `pages/` holds every screen of the app composed from them:

| Page | Route | Who |
| --- | --- | --- |
| Login, Setup | `/login`, `/setup` | everyone / first run |
| Dashboard | `/` | everyone |
| Group | `/groups/:id` | everyone |
| Device — Ports, Device — Sensors | `/devices/:id?tab=ports|sensors` | everyone |
| Sensor | `/sensors/:id` | everyone |
| Events | `/events` | everyone |
| Topology (map) | `/topology` | everyone |
| Find MAC / IP | `/find` | everyone |
| Maintenance | `/maintenance` | everyone (edit: admin) |
| Templates | `/templates` | everyone (apply: admin) |
| Discovery, Scan | `/discovery`, `/discovery/scans/:id` | admin |
| Notifications | `/notifications` | admin |
| Settings | `/settings` | admin |
| Users | `/users` | admin |
| My account | `/account` | everyone |

The app itself is React + Tailwind: map each token to a Tailwind theme colour/spacing (or CSS variable) rather than hard-coding slate/sky classes, so both themes and RTL come from one place.
