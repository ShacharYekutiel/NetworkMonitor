# AppShell

The frame of every signed-in page: a 48px header (menu toggle, brand mark, MAC/IP search, live-connection indicator, language and theme switches, user menu), a 272px sidebar (primary nav, then the device tree) and a scrolling main area. Shown in Hebrew: the whole shell mirrors under `dir="rtl"` because every rule uses logical properties (`inset-inline-start`, `border-inline-end`, `margin-inline-start`).

**Consumer provides:** the current route (to mark the active nav item with `.on`), the user and role (admin-only items: Discovery, Notifications, Settings, Users), the live state (`.nm-live` / `.nm-live.off`) and the page content.

- Header is `chrome`; sidebar is `sidebar`; content sits on `bg` with `space-6` padding (16px below 1024px).
- The active nav item uses `nav-active` / `nav-active-ink`. The Events item may carry a Down count pill.
- Below 1024px the sidebar becomes a drawer over an `overlay` scrim; on desktop the menu button collapses it (remembered per browser).
- A global maintenance banner (`.nm-banner.global`) sits above the page content, never above the header.
