# Modal

Dialogs for create/edit forms (group, device, sensor, user, maintenance window, channel, rule) and confirmations (delete). Centred on an `overlay` scrim; Escape and the close button dismiss; focus is trapped.

**Consumer provides:** title, body, footer actions (cancel `ghost`, confirm `primary` or `danger`).

- Panel `surface-raised`, `radius-lg`, `shadow-pop`, max 520px (forms up to 720px). Footer on `surface-sunken`.
- Delete confirmations name the object and what goes with it, and use a `danger` button whose label repeats the verb.
