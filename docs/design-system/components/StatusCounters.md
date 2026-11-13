# StatusCounters

Counter tiles per status plus a total, and the proportional status bar. Used on the Dashboard (sensors, devices), Group and Scan pages.

**Consumer provides:** counts per status. Order is always Down, Warning, Unreachable, Unknown, Paused, Up, Total.

- Tiles with a non-zero problem count (Down, Warning, Unreachable) go `.hot`: soft status ground, status-coloured number. Zero counts dim to `ink-subtle`.
- Numbers use `metric` (mono 28px). Captions use `label`.
- The bar's segments are flexed by count with 2px gaps, `radius-xs`.
