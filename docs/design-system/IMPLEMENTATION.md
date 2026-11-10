# Implementing the design system in `frontend/`

Handoff for the frontend developer. The visual design of NetworkMonitor lives in this folder; the app in
`frontend/` does not use it yet. This document says what to read, in what order to build, and when it is done.

## What is in this folder

| File | What it is | How to use it |
| --- | --- | --- |
| `README.md` | The design rules (wording, colour, type, panels, controls, icons) | Read first. Rules win over the mockups when they disagree. |
| `tokens.json` | Source of truth for every colour (dark + light), font, spacing, radius, shadow, layout size | Never hard-code a value that exists here. |
| `tokens.css` | `tokens.json` as CSS variables: light on `:root`, dark on `:root.dark` | Copy into the app (step 1). Regenerate if `tokens.json` changes. |
| `kit.css` | Static CSS (`nm-*` classes) that the mockups are drawn with | **Reference only.** Read it for exact sizes/paddings; do not ship it — rebuild with Tailwind. |
| `pages/*.html` | Static mockup of every screen (18) | Open in a browser. Dark by default; add `#light` to the URL for the light theme. |
| `components/*.html` + `*.md` | Each building block with its usage notes | Same: open the `.html`, follow the `.md`. |
| `assets/` | The logo mark and the 47 icons (same as `frontend/src/components/icons.tsx`) | Icons already exist in code; nothing to import. |

Mockup data (device names, numbers) is illustrative. Strings in the mockups follow `en.json`/`he.json`; keep using
`t()` and the existing keys.

## Build order

### 1. Foundation (one PR)

1. Copy `docs/design-system/tokens.css` to `frontend/src/styles/tokens.css` and import it at the top of
   `frontend/src/index.css`. Keep the existing `dark` class on `<html>` (from `hooks/theme.tsx`); the tokens switch on it.
2. In `frontend/tailwind.config.ts` map the tokens to Tailwind names (values are `var(--…)`), keep `darkMode: 'class'`:
   - `colors`: `bg`, `surface` (`DEFAULT`, `raised`, `sunken`, `hover`), `line` (`DEFAULT` = `--border`, `strong`),
     `ink` (`DEFAULT`, `muted`, `subtle`), `chrome` (`DEFAULT`, `ink`, `muted`, `field`, `border`),
     `accent` (`DEFAULT`, `hover`, `ink`, `soft`, `on` = `--on-accent`), `status` (`up`, `up-soft`, `warning`, …,
     `paused-soft`), `ack`, `ack-soft`, `danger`, `on-danger`, `on-status`, `overlay`, `chart-1..4`, `chart-grid`,
     `port-empty`, `port-disabled`.
   - `fontFamily`: `sans`, `display`, `mono` from the `--font-*` variables.
   - `borderRadius`: `xs`, `sm`, `md`, `lg`, `full` from the `--radius-*` variables.
   - `boxShadow.pop` = `var(--shadow-pop)`; `spacing`/`width` for `header-height`, `rail-width`, `tree-width`.
   - Because the values are variables, opacity modifiers (`bg-accent/50`) do not work — use the `-soft` tokens.
   - Since every token already has a dark value, components need **no `dark:` variants** for colour any more.
3. Fonts: Barlow, Barlow Condensed, Heebo, JetBrains Mono. The app is installed on-prem and may have no internet, so
   bundle them with `@fontsource/*` packages (weights 400/500/600; Barlow Condensed 600 only) instead of Google Fonts.
4. Replace the hard-coded colours in the shared primitives so every page picks up the new look:
   - `lib/status.ts` — `STATUS_DOT`, `STATUS_TEXT` (and any other per-status class maps) → `bg-status-*`, `text-status-*`,
     soft grounds `bg-status-*-soft`.
   - `components/ui.tsx` — `Button` variants, inputs, `Field`, `Card`, `Modal`, badges/tags, `Spinner`, `ErrorBox`.
   - `components/Status.tsx`, `toast.tsx`, `Pagination.tsx`, `Breadcrumbs.tsx`.
   - Search the tree for `slate-`, `sky-`, `gray-`, `green-6`, `amber-`, `red-6`, `violet-`, `emerald-` and replace each with a token.

### 2. Shell (one PR) — `components/Layout.tsx`, `Tree.tsx`, `Controls.tsx`, `Maintenance.tsx`

Follow `pages/Dashboard.html` (RTL) and `components/AppShell.*`:
- Header bar in `chrome` (dark in both themes), 52px: mark · wordmark · **health strip** · MAC/IP search · Live dot +
  clock · language · theme · user. The health strip shows sensor counts per status — reuse the existing dashboard query /
  live cache; it needs no new API.
- Nav rail (76px, icon + short label, `chrome`) replaces the nav list; the device tree moves to a 248px "Inventory"
  panel. Short rail labels need new i18n keys (`nav.short.*`) in **both** `he.json` and `en.json`.
- Below 1024px the Inventory panel becomes a drawer (as the sidebar is today).

### 3. Pages (one PR per row or two)

| Mockup | Code |
| --- | --- |
| `Login.html`, `Setup.html` | `pages/Login.tsx`, `pages/Setup.tsx`, `pages/AuthShell.tsx` |
| `Dashboard.html` | `pages/Dashboard.tsx` |
| `Group.html` | `pages/GroupPage.tsx` |
| `Device.html`, `DeviceSensors.html` | `pages/DevicePage.tsx`, `components/ports/*`, `SensorTable.tsx`, `SensorRowActions.tsx` |
| `Sensor.html` | `pages/SensorPage.tsx`, `HistoryChart.tsx`, `ThresholdEditor.tsx`, `CheckResultView.tsx` |
| `Events.html` | `pages/EventsPage.tsx`, `EventList.tsx` |
| `Topology.html` | `pages/TopologyPage.tsx`, `TopologyChart.tsx` |
| `Find.html` | `pages/FindPage.tsx` |
| `Maintenance.html` | `pages/MaintenancePage.tsx`, `Maintenance.tsx` |
| `Discovery.html`, `Scan.html` | `pages/DiscoveryPage.tsx`, `pages/ScanPage.tsx`, `components/discovery/*` |
| `Templates.html` | `pages/TemplatesPage.tsx`, `Templates.tsx` |
| `Notifications.html` | `pages/NotificationsPage.tsx` |
| `Settings.html` | `pages/SettingsPage.tsx` |
| `Users.html`, `Account.html` | `pages/UsersPage.tsx`, `pages/AccountPage.tsx` |

ECharts (history chart, topology) cannot read CSS variables directly: read them once with
`getComputedStyle(document.documentElement)` and rebuild the chart options when the theme changes.

## Rules

- Visual change only: no behaviour, API or data changes; keep every `data-testid` and accessible name the tests use.
- RTL rules in `docs/conventions.md` still apply (logical properties; IPs/MACs/times in `dir="ltr"`). The faceplate and
  chart axes stay LTR.
- Status always shows its word and glyph, never colour alone.
- No glow, gradients, textures, uppercase labels or panel numbering — see "Effects" in `README.md`.

## Done when

- `npm run build` passes with zero TypeScript errors and `npm test` passes.
- Every page matches its mockup in dark and light, in English and in Hebrew (RTL).
- No hard-coded colour classes remain (`grep -rE "(slate|sky|gray|zinc)-[0-9]" frontend/src` returns nothing).
