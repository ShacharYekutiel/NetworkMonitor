# HistoryChart

Line chart of a sensor's channels over 1 hour / 24 hours / 7 days / 30 days, with warning and down threshold lines, a hover cursor and tooltip, and shaded gaps where the sensor was down or had no data.

**Consumer provides:** series (channel name, unit, points), thresholds per channel, range, and the selected point on hover.

- Series colours in order `chart-1` … `chart-4`; the first series gets a 14% area fill. Gridlines `chart-grid`; axis labels `data-xs` in `ink-subtle`.
- Threshold lines are dashed `status-warning` and `status-down` and are named in the legend with their values.
- Tooltip (`.nm-tip`) uses `surface-raised` + `shadow-pop`, mono values.
