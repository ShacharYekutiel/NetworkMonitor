# DataTable

Dense, sortable tables for sensors, devices, events, ports, scan hosts, users. Optional toolbar (filter input, selects, end-aligned actions), optional bulk-selection bar, optional pager.

**Consumer provides:** columns (with numeric ones marked `.n` — mono, end-aligned), rows, row actions, selection, sort and page state.

- Header row `surface-sunken`, 12px 600 `ink-muted`. Rows ≥ `row-height` (36px), `border` hairlines, hover `surface-hover`, selected `accent-soft`.
- The name cell is a link in `accent-ink` with the type on a second line (`.sub`). Times, IPs, MACs, values are mono.
- Row actions are `ghost sm` icon buttons in the last column; problem rows get an Ack button first.
- Sparklines (`.nm-spark`, 72×18) show the last hour in `chart-1`.
