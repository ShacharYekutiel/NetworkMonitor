# TopologyNode

Nodes and links of the Network map. A node is a 44px tile with the device-class icon, a status-coloured outline and corner dot, and its name label; unmanaged LLDP/CDP neighbours are dashed ghosts. Links are coloured by state with optional traffic labels.

**Consumer provides:** nodes (device, class, status, position) and links (endpoints, local/remote port, state up / down / stale, utilisation).

- Link up `status-up`, down dashed `status-down`, stale dotted `ink-subtle`. Traffic label: mono 10px on a `surface` chip.
- The canvas is `surface-sunken` with a dot grid at 16px; selection is a `focus` outline.
