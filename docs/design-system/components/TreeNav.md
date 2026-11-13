# TreeNav

The sidebar's device tree: groups (folder) contain subgroups and devices (icon by device class: switch, router, wifi, server, nas, printer, desktop, question). Each row shows count pills for its problems only (Down, Warning, Unreachable), worst first; healthy rows show nothing.

**Consumer provides:** the tree, expanded ids (remembered per browser), the selected node, and the filter text from the search box above.

- Row: `.nm-node`, 13px `small`, 6px gap, `radius-sm`; hover `surface-hover`; selected `.on` = `nav-active`.
- Indent 14px per level. Chevrons rotate 90° when open and mirror in RTL.
- Never show Up counts in the tree — it is a problem finder, not an inventory.
