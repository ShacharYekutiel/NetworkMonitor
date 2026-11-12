# StatusBadge

The six sensor/device statuses, worst first: Down › Warning › Unreachable › Unknown › Up › Paused. Every status always carries its word **and** its glyph (✕ ⚠ link ? ✓ ‖), so it reads without colour.

**Consumer provides:** `status`. Variants: badge (`.nm-badge`), dot (`.nm-dot`, tree and legends), count pill (`.nm-pill`, numbers only).

- Badge = `status-*-soft` ground + `status-*` text; pill = solid `status-*` + `on-status`.
- A transition is two badges with an arrow: `Up → Down` (mirrors in RTL).
- Unreachable means "suppressed because the parent is down" — never alert on it, never colour it red.
- Down and Up differ in hue more than lightness: never rely on the dot alone in dense views — keep the word.
