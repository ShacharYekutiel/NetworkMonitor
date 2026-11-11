# FormControls

Field (label + control + hint/error), Input, Select, Textarea, Checkbox, Switch and the write-only Secret field.

**Consumer provides:** label, value, `required`, `hint`, `error`. IPs, OIDs, MACs, ports and numbers use the `mono` input and are always `dir="ltr"`, even in Hebrew.

- Controls are 32px, `radius-md`, `border-strong` outline on `surface`; focus replaces the outline with a 2px `focus` ring.
- Labels 12px 500 `ink-muted`; required star `status-down`; hints 12px `ink-subtle`; errors replace the hint in `status-down` and turn the outline red.
- Secrets (SNMP communities, SMTP and Telegram tokens) are write-only: show `••••` disabled and a Replace button — never the value.
- Forms lay out on a 2-column grid (`.nm-form`), full-width rows for URLs and textareas.
