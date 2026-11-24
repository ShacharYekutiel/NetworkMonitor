import { describe, expect, it } from 'vitest';
import type { Features } from '../api/types';
import {
  activeMenuKey,
  menuSections,
  readMenuTab,
  readPinned,
  writeMenuTab,
  writePinned,
} from './menu';
import { islandState } from '../components/shell/Island';
import { emptyCounts } from './status';

const ALL_ON = new Proxy({}, { get: () => true }) as Features;
const keys = (s: ReturnType<typeof menuSections>) => s.flatMap((x) => x.items.map((i) => i.key));

describe('premium menu', () => {
  it('admins with every feature see every screen, in four sections', () => {
    const s = menuSections({ isAdmin: true, features: ALL_ON });
    expect(s.map((x) => x.key)).toEqual(['monitor', 'equipment', 'operations', 'admin']);
    expect(keys(s)).toEqual([
      'dashboard',
      'assistant',
      'events',
      'logs',
      'map',
      'flows',
      'find',
      'newDevices',
      'backups',
      'discovery',
      'templates',
      'maintenance',
      'reports',
      'notifications',
      'settings',
      'users',
      'audit',
      'tokens',
      'account',
    ]);
  });

  it('viewers get no admin pages; disabled features hide their entry', () => {
    const k = keys(menuSections({ isAdmin: false, features: {} }));
    for (const x of ['assistant', 'discovery', 'notifications', 'settings', 'users', 'audit'])
      expect(k).not.toContain(x);
    for (const x of ['logs', 'flows', 'newDevices', 'backups', 'reports'])
      expect(k).not.toContain(x);
    expect(k).toContain('events');
    expect(k).toContain('templates');
  });

  it('highlights the entry of the current page (longest prefix)', () => {
    const s = menuSections({ isAdmin: true, features: ALL_ON });
    expect(activeMenuKey(s, '/')).toBe('dashboard');
    expect(activeMenuKey(s, '/assistant/usage')).toBe('assistant');
    expect(activeMenuKey(s, '/discovery/scans/4')).toBe('discovery');
    expect(activeMenuKey(s, '/devices/3')).toBeNull();
  });

  it('remembers the pin and the tab, and survives broken storage', () => {
    const m = new Map<string, string>();
    const st = {
      getItem: (k: string) => m.get(k) ?? null,
      setItem: (k: string, v: string) => void m.set(k, v),
    };
    expect(readPinned(st)).toBe(false);
    writePinned(true, st);
    expect(readPinned(st)).toBe(true);
    writeMenuTab('inventory', st);
    expect(readMenuTab(st)).toBe('inventory');
    const broken = {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      },
    };
    expect(readPinned(broken)).toBe(false);
    expect(readMenuTab(broken)).toBe('screens');
    expect(() => writePinned(true, broken)).not.toThrow();
  });
});

describe('dynamic island', () => {
  it('follows the counters and the live connection', () => {
    const c = emptyCounts();
    expect(islandState(c, 'connecting')).toBe('offline');
    expect(islandState(null, 'open')).toBe('loading');
    expect(islandState(c, 'open')).toBe('ok');
    expect(islandState({ ...c, unreachable: 1 }, 'open')).toBe('warn');
    expect(islandState({ ...c, warning: 2, down: 1 }, 'open')).toBe('alert');
  });
});
