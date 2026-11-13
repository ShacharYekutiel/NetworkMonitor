# Card

The only container: a `surface` panel with a `border` hairline, `radius-lg`, no shadow. Header (title + count or status, end-aligned actions), body, optional footer.

**Consumer provides:** `title`, optional header `end` content (links, segmented control, small buttons), body, and footer text. Use `flush` bodies for tables and lists so rows run edge to edge.

- Header 44px min, `heading` style; body padding `space-4`; footer 12px `ink-subtle`.
- Definition lists inside cards use `.nm-kv` (labels `ink-muted`, values `ink`, mono for technical values).
- Empty state: `.nm-empty` centred text; the all-clear state uses `.nm-empty.ok` in `status-up`.
