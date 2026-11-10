# ToastAndMenu

Toasts confirm an action that has no visible result in place (Saved, Sensor resumed, Topology refresh started) or report a failed one; they stack bottom-end and leave after 4 s. Menus hold overflow and user actions.

**Consumer provides:** toast tone + text; menu items (icon, label, danger).

- Both use `surface-raised` + `shadow-pop`. Toast icon in the tone colour. Menu items 13px, hover `surface-hover`, destructive items `status-down`.
