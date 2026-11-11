# Button

Four variants in two sizes. `primary` (one per view: Add device, Save, Start scan), `secondary` (everything else), `danger` (only inside a delete confirmation), `ghost` (Reset, Clear filters, toolbar icons).

**Consumer provides:** label, optional leading icon (14px), `size` md (32px) or sm (26px, inside tables and card headers), `busy` (spinner replaces nothing; the button disables).

- Radius `radius-md`; text 13px 500. Icon-only buttons are square (`.icon`) and need an `aria-label`.
- Links that look like buttons use the same classes on `<a>` — never nest a button in a link.
- Focus: 2px `focus` ring, 2px offset.
