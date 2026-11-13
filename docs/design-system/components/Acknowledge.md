# Acknowledge

Acknowledging a Down or Warning sensor silences its further problem, escalation and reminder notifications; the status itself is unchanged and the acknowledgement clears on the next status change. Recovery is still notified.

**Consumer provides:** the sensor, optional comment, and after acknowledgement who/when. Only for `down` and `warning`; admins only.

- Action: `secondary sm` button with the check glyph. Acknowledged state: `.nm-chip.ack` (`ack` on `ack-soft`) with "Acknowledged by {user} at {time}" and the comment in quotes.
- The dialog explains the effect in one paragraph before the comment field.
