# PageHeader

Breadcrumbs, the page title (with its type icon and status badge), a one-line subtitle or a meta row, and the page actions on the end side.

**Consumer provides:** `crumbs` (Dashboard › group path › device › sensor), `title`, optional `icon`, `status`, `subtitle` or `meta` items, and `actions` (primary first, at most one primary).

- Title uses `title` (20/28 600); subtitle `small` in `ink-muted`; meta values in `ink`, IPs and times in `mono`.
- Actions wrap under the title on narrow screens. Destructive actions are icon-only secondary buttons at the end (Delete), confirmed in a Modal.
