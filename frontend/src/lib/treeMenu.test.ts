import { describe, expect, it } from 'vitest';
import type { Status, TreeDevice, TreeGroup, TreeSensor } from '../api/types';
import { isEventSensor } from '../components/EventConditions';
import {
  explainSensorOf,
  isMenuKey,
  keyboardAnchor,
  nextMenuIndex,
  placeMenu,
  runnableSensors,
  treeMenuItems,
  treeMenuLabelKey,
  type TreeMenuContext,
  type TreeMenuEntry,
  type TreeMenuTarget,
} from './treeMenu';
import en from '../i18n/locales/en.json';
import he from '../i18n/locales/he.json';

const counts = { up: 0, warning: 0, down: 0, paused: 0, unknown: 0, unreachable: 0, total: 0 };

function sensor(id: number, status: Status = 'up', extra: Partial<TreeSensor> = {}): TreeSensor {
  return {
    id,
    name: `s${id}`,
    type: 'ping',
    enabled: status !== 'paused',
    status,
    status_message: '',
    primary_channel: 'rtt',
    primary_value: 1,
    primary_unit: 'ms',
    last_check_at: null,
    acknowledged: false,
    ...extra,
  };
}
function device(id: number, sensors: TreeSensor[], extra: Partial<TreeDevice> = {}): TreeDevice {
  return {
    id,
    name: `dev${id}`,
    host: `10.0.0.${id}`,
    enabled: true,
    status: 'up',
    status_counts: counts,
    in_maintenance: false,
    parent_device_id: null,
    sensors,
    ...extra,
  };
}
function group(id: number, devices: TreeDevice[], children: TreeGroup[] = []): TreeGroup {
  return {
    id,
    name: `g${id}`,
    parent_id: null,
    status: 'up',
    status_counts: counts,
    children,
    devices,
  };
}

const OFF = { timeline: false, config_backup: false, assistant: false, ai_explain_alert: false };
const ON = { timeline: true, config_backup: true, assistant: true, ai_explain_alert: true };

function ctx(over: Partial<TreeMenuContext> = {}): TreeMenuContext {
  return {
    isAdmin: true,
    features: OFF,
    snmpTypes: new Set(['snmp_interface', 'snmp_cpu']),
    isPassive: (type) => isEventSensor({ type: type as TreeSensor['type'] }),
    discoveryProfiles: [],
    ...over,
  };
}

/** Actions in order, with "|" for separators. */
function shape(entries: TreeMenuEntry[]): string[] {
  return entries.map((e) => (e.type === 'separator' ? '|' : e.action));
}
const actions = (entries: TreeMenuEntry[]) => shape(entries).filter((x) => x !== '|');
const find = (entries: TreeMenuEntry[], action: string) =>
  entries.find((e) => e.type === 'item' && e.action === action) as
    Extract<TreeMenuEntry, { type: 'item' }> | undefined;

describe('tree menu items: group', () => {
  const g = group(
    1,
    [device(1, [sensor(1)]), device(2, [], { enabled: false })],
    [group(2, [device(3, [])])],
  );
  const target: TreeMenuTarget = { kind: 'group', group: g };

  it('admin: sections in order, delete is danger, discovery per profile of the group', () => {
    const items = treeMenuItems(
      target,
      ctx({ discoveryProfiles: [{ id: 7, name: 'Office LAN' }] }),
    );
    expect(shape(items)).toEqual([
      'open',
      '|',
      'addDevice',
      'addSubgroup',
      '|',
      'pauseAll',
      'resumeAll',
      'maintenance',
      'discovery',
      '|',
      'edit',
      'delete',
    ]);
    expect(find(items, 'delete')?.danger).toBe(true);
    expect(find(items, 'discovery')).toMatchObject({ arg: 7, vars: { name: 'Office LAN' } });
  });

  it('pause/resume all follow the enabled state of every device in the subtree', () => {
    const allOn = group(1, [device(1, [])], [group(2, [device(2, [])])]);
    expect(actions(treeMenuItems({ kind: 'group', group: allOn }, ctx()))).toContain('pauseAll');
    expect(actions(treeMenuItems({ kind: 'group', group: allOn }, ctx()))).not.toContain(
      'resumeAll',
    );
    const nested = group(1, [], [group(2, [device(2, [], { enabled: false })])]);
    const a = actions(treeMenuItems({ kind: 'group', group: nested }, ctx()));
    expect(a).toContain('resumeAll');
    expect(a).not.toContain('pauseAll');
    const empty = actions(treeMenuItems({ kind: 'group', group: group(9, []) }, ctx()));
    expect(empty).not.toContain('pauseAll');
    expect(empty).not.toContain('resumeAll');
  });

  it('no discovery item without a profile for the group', () => {
    expect(actions(treeMenuItems(target, ctx()))).not.toContain('discovery');
  });

  it('viewer: only Open', () => {
    const items = treeMenuItems(
      target,
      ctx({ isAdmin: false, features: ON, discoveryProfiles: [{ id: 7, name: 'x' }] }),
    );
    expect(shape(items)).toEqual(['open']);
  });
});

describe('tree menu items: device', () => {
  const healthy = device(5, [sensor(1), sensor(2, 'up', { type: 'snmp_interface' })]);

  it('admin, features off: no tab / explain items; copy and danger delete present', () => {
    const items = treeMenuItems({ kind: 'device', device: healthy }, ctx());
    expect(shape(items)).toEqual([
      'open',
      'openNewTab',
      'tabPorts',
      '|',
      'addSensor',
      'applyTemplate',
      '|',
      'runAll',
      'pause',
      'maintenance',
      '|',
      'copyIp',
      'copyName',
      '|',
      'edit',
      'delete',
    ]);
    expect(find(items, 'delete')?.danger).toBe(true);
    expect(find(items, 'runAll')?.disabled).toBe(false);
  });

  it('Ports only with an SNMP sensor; Timeline / Configuration follow their features', () => {
    const plain = device(6, [sensor(1)]);
    expect(actions(treeMenuItems({ kind: 'device', device: plain }, ctx()))).not.toContain(
      'tabPorts',
    );
    const a = actions(
      treeMenuItems(
        { kind: 'device', device: plain },
        ctx({ features: { ...OFF, timeline: true, config_backup: true } }),
      ),
    );
    expect(a).toContain('tabTimeline');
    expect(a).toContain('tabConfig');
  });

  it('a disabled device offers Resume; Run all is disabled', () => {
    const items = treeMenuItems(
      { kind: 'device', device: device(7, [sensor(1, 'paused')], { enabled: false }) },
      ctx(),
    );
    expect(actions(items)).toContain('resume');
    expect(actions(items)).not.toContain('pause');
    expect(find(items, 'runAll')?.disabled).toBe(true);
  });

  it('Run all is disabled when only passive sensors exist', () => {
    const items = treeMenuItems(
      { kind: 'device', device: device(8, [sensor(1, 'up', { type: 'syslog' })]) },
      ctx(),
    );
    expect(find(items, 'runAll')?.disabled).toBe(true);
  });

  it('Explain: admin + assistant + explain feature + a problem sensor (the worst one)', () => {
    const sick = device(9, [sensor(1, 'warning'), sensor(2, 'down'), sensor(3)]);
    const items = treeMenuItems({ kind: 'device', device: sick }, ctx({ features: ON }));
    expect(find(items, 'explain')?.arg).toBe(2);
    // healthy device: no explain
    expect(
      actions(treeMenuItems({ kind: 'device', device: healthy }, ctx({ features: ON }))),
    ).not.toContain('explain');
    // feature combinations
    for (const f of [
      { ...ON, assistant: false },
      { ...ON, ai_explain_alert: false },
    ])
      expect(
        actions(treeMenuItems({ kind: 'device', device: sick }, ctx({ features: f }))),
      ).not.toContain('explain');
    // viewers never
    expect(
      actions(
        treeMenuItems({ kind: 'device', device: sick }, ctx({ isAdmin: false, features: ON })),
      ),
    ).not.toContain('explain');
  });

  it('viewer: navigation and copy only', () => {
    const items = treeMenuItems(
      { kind: 'device', device: healthy },
      ctx({ isAdmin: false, features: ON }),
    );
    expect(shape(items)).toEqual([
      'open',
      'openNewTab',
      'tabPorts',
      'tabTimeline',
      'tabConfig',
      '|',
      'copyIp',
      'copyName',
    ]);
  });
});

describe('tree menu items: sensor', () => {
  const d = device(1, []);
  const of = (s: TreeSensor, c = ctx()) =>
    treeMenuItems({ kind: 'sensor', sensor: s, device: d }, c);

  it('admin, healthy sensor', () => {
    expect(shape(of(sensor(1)))).toEqual(['open', '|', 'runNow', 'pause', '|', 'edit', 'delete']);
  });

  it('a problem sensor can be acknowledged once; explain needs the features', () => {
    expect(actions(of(sensor(1, 'down')))).toContain('acknowledge');
    expect(actions(of(sensor(1, 'warning')))).toContain('acknowledge');
    expect(actions(of(sensor(1, 'down', { acknowledged: true })))).not.toContain('acknowledge');
    expect(actions(of(sensor(1, 'unreachable')))).not.toContain('acknowledge');
    expect(actions(of(sensor(1, 'down')))).not.toContain('explain');
    expect(shape(of(sensor(1, 'down'), ctx({ features: ON })))).toEqual([
      'open',
      '|',
      'runNow',
      'pause',
      'acknowledge',
      '|',
      'explain',
      '|',
      'edit',
      'delete',
    ]);
    expect(find(of(sensor(4, 'unreachable'), ctx({ features: ON })), 'explain')?.arg).toBe(4);
  });

  it('paused sensor: Resume, Run now disabled; passive sensor: no Run now', () => {
    const paused = of(sensor(1, 'paused'));
    expect(actions(paused)).toContain('resume');
    expect(find(paused, 'runNow')?.disabled).toBe(true);
    expect(actions(of(sensor(1, 'up', { type: 'trap' })))).not.toContain('runNow');
  });

  it('Run now is disabled while the device is disabled', () => {
    const items = treeMenuItems(
      { kind: 'sensor', sensor: sensor(1), device: device(1, [], { enabled: false }) },
      ctx(),
    );
    expect(find(items, 'runNow')?.disabled).toBe(true);
  });

  it('viewer: Open only, even for a problem with every feature on', () => {
    expect(shape(of(sensor(1, 'down'), ctx({ isAdmin: false, features: ON })))).toEqual(['open']);
  });
});

describe('tree menu helpers', () => {
  it('every action has an en + he label', () => {
    const all = [
      ...treeMenuItems(
        { kind: 'group', group: group(1, [device(1, []), device(2, [], { enabled: false })]) },
        ctx({ discoveryProfiles: [{ id: 1, name: 'p' }] }),
      ),
      ...treeMenuItems(
        {
          kind: 'device',
          device: device(1, [sensor(1, 'down', { type: 'snmp_cpu' })]),
        },
        ctx({ features: ON }),
      ),
      ...treeMenuItems(
        { kind: 'device', device: device(2, [], { enabled: false }) },
        ctx({ features: ON }),
      ),
      ...treeMenuItems(
        { kind: 'sensor', sensor: sensor(1, 'down'), device: device(1, []) },
        ctx({ features: ON }),
      ),
    ];
    const get = (o: unknown, k: string) =>
      k.split('.').reduce<unknown>((x, p) => (x as Record<string, unknown>)?.[p], o);
    for (const e of all) {
      if (e.type !== 'item') continue;
      const key = treeMenuLabelKey(e.action);
      expect(get(en, key), key).toBeTypeOf('string');
      expect(get(he, key), key).toBeTypeOf('string');
    }
  });

  it('explainSensorOf picks the worst problem, ignoring up / paused / unknown', () => {
    expect(explainSensorOf(device(1, [sensor(1), sensor(2, 'paused'), sensor(3, 'unknown')]))).toBe(
      null,
    );
    expect(explainSensorOf(device(1, [sensor(1, 'unreachable'), sensor(2, 'warning')]))?.id).toBe(
      2,
    );
  });

  it('runnableSensors skips passive and paused sensors and disabled devices', () => {
    const isPassive = (t: string) => t === 'trap';
    const d = device(1, [sensor(1), sensor(2, 'paused'), sensor(3, 'up', { type: 'trap' })]);
    expect(runnableSensors(d, isPassive).map((s) => s.id)).toEqual([1]);
    expect(runnableSensors({ ...d, enabled: false }, isPassive)).toEqual([]);
  });

  it('isMenuKey: ContextMenu key and Shift+F10 only', () => {
    expect(isMenuKey({ key: 'ContextMenu', shiftKey: false })).toBe(true);
    expect(isMenuKey({ key: 'F10', shiftKey: true })).toBe(true);
    expect(isMenuKey({ key: 'F10', shiftKey: false })).toBe(false);
    expect(isMenuKey({ key: 'Enter', shiftKey: true })).toBe(false);
  });

  it('nextMenuIndex: wraps, skips disabled items, Home/End, type-ahead', () => {
    const items = [
      { label: 'Open' },
      { label: 'Pause', disabled: true },
      { label: 'Edit…' },
      { label: 'Delete…' },
    ];
    expect(nextMenuIndex(items, 0, 'ArrowDown')).toBe(2);
    expect(nextMenuIndex(items, 3, 'ArrowDown')).toBe(0);
    expect(nextMenuIndex(items, 0, 'ArrowUp')).toBe(3);
    expect(nextMenuIndex(items, 2, 'Home')).toBe(0);
    expect(nextMenuIndex(items, 0, 'End')).toBe(3);
    expect(nextMenuIndex(items, -1, 'Home')).toBe(0);
    expect(nextMenuIndex(items, 0, 'd')).toBe(3);
    expect(nextMenuIndex(items, 0, 'E')).toBe(2);
    expect(nextMenuIndex(items, 0, 'p')).toBe(0); // only a disabled match
    expect(nextMenuIndex(items, 2, 'x')).toBe(2);
    expect(nextMenuIndex([{ label: 'a', disabled: true }], -1, 'Home')).toBe(-1);
  });
});

describe('placeMenu (RTL-aware, inside the viewport)', () => {
  const vp = { width: 1000, height: 800 };
  const m = { width: 200, height: 300 };

  it('LTR: grows right/down from the pointer, flips left/up near the edges', () => {
    expect(placeMenu({ x: 100, y: 100 }, m, vp, false)).toEqual({ left: 100, top: 100 });
    expect(placeMenu({ x: 900, y: 100 }, m, vp, false)).toEqual({ left: 700, top: 100 });
    expect(placeMenu({ x: 100, y: 700 }, m, vp, false)).toEqual({ left: 100, top: 400 });
  });

  it('RTL: mirrored, grows left from the pointer, flips right near the start edge', () => {
    expect(placeMenu({ x: 900, y: 100 }, m, vp, true)).toEqual({ left: 700, top: 100 });
    expect(placeMenu({ x: 100, y: 100 }, m, vp, true)).toEqual({ left: 100, top: 100 });
  });

  it('clamps a menu larger than the room on either side into the viewport', () => {
    const narrow = { width: 300, height: 400 };
    const big = { width: 250, height: 500 };
    expect(placeMenu({ x: 150, y: 200 }, big, narrow, false)).toEqual({ left: 4, top: 4 });
    // RTL keeps the menu against the end (right) edge of the viewport
    expect(placeMenu({ x: 150, y: 200 }, big, narrow, true)).toEqual({ left: 46, top: 4 });
    expect(placeMenu({ x: 0, y: 0 }, m, vp, true)).toEqual({ left: 4, top: 4 });
  });

  it('keyboardAnchor: under the item at its inline start', () => {
    const r = { left: 10, right: 210, bottom: 40 };
    expect(keyboardAnchor(r, false)).toEqual({ x: 10, y: 40 });
    expect(keyboardAnchor(r, true)).toEqual({ x: 210, y: 40 });
  });
});
