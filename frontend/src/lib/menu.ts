// Premium shell: the sections of the ☰ menu (docs/design-system/premium/BUILD.md § 3.2) and the
// remembered "pinned" choice. Pure, so the visibility rules are unit-tested.
import type { Features } from '../api/types';
import { ingestOn } from './features';

export type MenuIconKey =
  | 'home'
  | 'assistant'
  | 'events'
  | 'logs'
  | 'map'
  | 'flows'
  | 'find'
  | 'newDevices'
  | 'backups'
  | 'discovery'
  | 'templates'
  | 'maintenance'
  | 'reports'
  | 'notifications'
  | 'settings'
  | 'users'
  | 'audit'
  | 'tokens'
  | 'account';

export type MenuItem = {
  key: string;
  to: string;
  /** i18n key of the label (also the accessible name of the link) */
  label: string;
  icon: MenuIconKey;
  /** show the open "down" count next to it */
  badge?: 'down';
};
export type MenuSection = { key: string; items: MenuItem[] };

type Ctx = { isAdmin: boolean; features: Partial<Features> };

/** Sections and their entries for this user; disabled features and admin-only pages are left out. */
export function menuSections({ isAdmin, features: f }: Ctx): MenuSection[] {
  const on = (x: boolean | undefined) => !!x;
  const sections: { key: string; items: (MenuItem | false)[] }[] = [
    {
      key: 'monitor',
      items: [
        { key: 'dashboard', to: '/', label: 'nav.dashboard', icon: 'home' },
        isAdmin &&
          on(f.assistant) && {
            key: 'assistant',
            to: '/assistant',
            label: 'nav.assistant',
            icon: 'assistant',
          },
        { key: 'events', to: '/events', label: 'nav.events', icon: 'events', badge: 'down' },
        ingestOn(f as Features) && { key: 'logs', to: '/logs', label: 'nav.logs', icon: 'logs' },
        { key: 'map', to: '/topology', label: 'nav.map', icon: 'map' },
        on(f.flows) && { key: 'flows', to: '/flows', label: 'nav.flows', icon: 'flows' },
        { key: 'find', to: '/find', label: 'nav.find', icon: 'find' },
      ],
    },
    {
      key: 'equipment',
      items: [
        on(f.new_device_alerts) && {
          key: 'newDevices',
          to: '/new-devices',
          label: 'nav.newDevices',
          icon: 'newDevices',
        },
        on(f.config_backup) && {
          key: 'backups',
          to: '/config-backups',
          label: 'nav.backups',
          icon: 'backups',
        },
        isAdmin && {
          key: 'discovery',
          to: '/discovery',
          label: 'nav.discovery',
          icon: 'discovery',
        },
        { key: 'templates', to: '/templates', label: 'nav.templates', icon: 'templates' },
      ],
    },
    {
      key: 'operations',
      items: [
        { key: 'maintenance', to: '/maintenance', label: 'nav.maintenance', icon: 'maintenance' },
        on(f.reports) && { key: 'reports', to: '/reports', label: 'nav.reports', icon: 'reports' },
        isAdmin && {
          key: 'notifications',
          to: '/notifications',
          label: 'nav.notifications',
          icon: 'notifications',
        },
      ],
    },
    {
      key: 'admin',
      items: [
        isAdmin && { key: 'settings', to: '/settings', label: 'nav.settings', icon: 'settings' },
        isAdmin && { key: 'users', to: '/users', label: 'nav.users', icon: 'users' },
        isAdmin && on(f.audit) && { key: 'audit', to: '/audit', label: 'nav.audit', icon: 'audit' },
        { key: 'tokens', to: '/tokens', label: 'nav.tokens', icon: 'tokens' },
        { key: 'account', to: '/account', label: 'nav.account', icon: 'account' },
      ],
    },
  ];
  return sections
    .map((s) => ({ key: s.key, items: s.items.filter((i): i is MenuItem => !!i) }))
    .filter((s) => s.items.length > 0);
}

/** The entry that matches the current path (the longest `to` that prefixes it). */
export function activeMenuKey(sections: MenuSection[], pathname: string): string | null {
  let best: MenuItem | null = null;
  for (const s of sections)
    for (const i of s.items) {
      const hit =
        i.to === '/' ? pathname === '/' : pathname === i.to || pathname.startsWith(`${i.to}/`);
      if (hit && (!best || i.to.length > best.to.length)) best = i;
    }
  return best?.key ?? null;
}

export const MENU_PIN_KEY = 'netmon.menu.pinned';
export const MENU_TAB_KEY = 'netmon.menu.tab';
export type MenuTab = 'screens' | 'inventory';

/** Remembered per browser; storage may be unavailable (private mode, blocked). */
export function readPinned(storage: Pick<Storage, 'getItem'> | undefined = safeStorage()): boolean {
  try {
    return storage?.getItem(MENU_PIN_KEY) === '1';
  } catch {
    return false;
  }
}

export function writePinned(
  pinned: boolean,
  storage: Pick<Storage, 'setItem'> | undefined = safeStorage(),
): void {
  try {
    storage?.setItem(MENU_PIN_KEY, pinned ? '1' : '0');
  } catch {
    /* ignore */
  }
}

export function readMenuTab(
  storage: Pick<Storage, 'getItem'> | undefined = safeStorage(),
): MenuTab {
  try {
    return storage?.getItem(MENU_TAB_KEY) === 'inventory' ? 'inventory' : 'screens';
  } catch {
    return 'screens';
  }
}

export function writeMenuTab(
  tab: MenuTab,
  storage: Pick<Storage, 'setItem'> | undefined = safeStorage(),
): void {
  try {
    storage?.setItem(MENU_TAB_KEY, tab);
  } catch {
    /* ignore */
  }
}

function safeStorage(): Storage | undefined {
  try {
    return typeof localStorage === 'undefined' ? undefined : localStorage;
  } catch {
    return undefined;
  }
}
