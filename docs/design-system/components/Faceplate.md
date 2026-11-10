# Faceplate

The switch front panel on the Device › Ports tab: one chassis per stack member, ports laid out as on the hardware (odd on top, even below, groups of 12, SFP uplinks apart), each tile showing link state, alert, what is connected and how many MACs an uplink learned.

**Consumer provides:** members with their ports (name, state up / no link / disabled / absent, alert, connected kind client / ap / switch, trunk, MAC count), the selected port. Clicking a port opens its panel.

- Tiles: Connected `status-up`; Connected + alert `status-down`; Empty `port-empty`; Empty + alert = `port-empty` with a 2px `status-down` ring; Disabled `port-disabled` + ban glyph. Glyph colour `on-status`.
- The chassis is always LTR (it mirrors hardware), even inside a Hebrew page.
- A legend with every state sits under it. Port numbers use `data-xs`.
