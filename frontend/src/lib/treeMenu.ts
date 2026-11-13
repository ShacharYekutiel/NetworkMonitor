// [3b] Right-click context menu of the sidebar device tree: which actions an item offers, where the
// menu goes on screen, and its keyboard handling. Pure helpers (unit-tested in treeMenu.test.ts).
import type { Features, Status, TreeDevice, TreeGroup, TreeSensor } from '../api/types';
import { canAcknowledge, canRun, severity } from './status';
import { allDevices } from './tree';

export type TreeMenuTarget =
  | { kind: 'group'; group: TreeGroup }
  | { kind: 'device'; device: TreeDevice }
  | { kind: 'sensor'; sensor: TreeSensor; device: TreeDevice };

export type TreeMenuAction =
  | 'open'
  | 'openNewTab'
  | 'addDevice'
  | 'addSubgroup'
  | 'pauseAll'
  | 'resumeAll'
  | 'maintenance'
  | 'discovery'
  | 'addSensor'
  | 'applyTemplate'
  | 'runAll'
  | 'pause'
  | 'resume'
  | 'tabPorts'
  | 'tabTimeline'
  | 'tabConfig'
  | 'explain'
  | 'copyIp'
  | 'copyName'
  | 'runNow'
  | 'acknowledge'
  | 'edit'
  | 'delete';

export type TreeMenuItem = {
  type: 'item';
  action: TreeMenuAction;
  /** Stable key (an action can appear more than once, e.g. one "Run discovery" per profile). */
  key: string;
  danger?: boolean;
  disabled?: boolean;
  /** discovery: the profile to run; explain: the sensor to explain */
  arg?: number;
  /** label interpolation (discovery: the profile name) */
  vars?: Record<string, string>;
};
export type TreeMenuEntry = TreeMenuItem | { type: 'separator'; key: string };

export type TreeMenuContext = {
  isAdmin: boolean;
  features: Pick<Features, 'timeline' | 'config_backup' | 'assistant' | 'ai_explain_alert'>;
  /** Sensor types that need SNMP (a device with one of them has a Ports tab). */
  snmpTypes: ReadonlySet<string>;
  /** Passive (event) sensor types cannot be run on demand. */
  isPassive: (type: string) => boolean;
  /** Discovery profiles whose target group is the group of the menu (admins). */
  discoveryProfiles?: { id: number; name: string }[];
};

/** Statuses worth an AI explanation (same as the Explain button of the sensor page). */
export function explainable(s: Status): boolean {
  return s === 'down' || s === 'warning' || s === 'unreachable';
}

/** The sensor an "Explain with AI" on a device explains: its worst problem sensor, else null. */
export function explainSensorOf(device: TreeDevice): TreeSensor | null {
  let best: TreeSensor | null = null;
  for (const s of device.sensors) {
    if (!explainable(s.status)) continue;
    if (!best || severity(s.status) > severity(best.status)) best = s;
  }
  return best;
}

/** Sensors of the device that "Run all sensors now" runs (active, not paused). */
export function runnableSensors(
  device: TreeDevice,
  isPassive: (type: string) => boolean,
): TreeSensor[] {
  if (!device.enabled) return [];
  return device.sensors.filter((s) => !isPassive(s.type) && canRun(s));
}

const item = (
  action: TreeMenuAction,
  extra: Partial<Omit<TreeMenuItem, 'type' | 'action'>> = {},
): TreeMenuItem => ({ type: 'item', action, key: extra.key ?? action, ...extra });

/** Joins the non-empty sections with separators. */
function sections(...groups: (TreeMenuItem | false | null | undefined)[][]): TreeMenuEntry[] {
  const out: TreeMenuEntry[] = [];
  for (const g of groups) {
    const items = g.filter((x): x is TreeMenuItem => !!x);
    if (!items.length) continue;
    if (out.length) out.push({ type: 'separator', key: `sep${out.length}` });
    out.push(...items);
  }
  return out;
}

function aiExplainOn(ctx: TreeMenuContext): boolean {
  return ctx.isAdmin && ctx.features.assistant && ctx.features.ai_explain_alert;
}

/** The entries of the context menu of a tree item, for this user and these feature toggles. */
export function treeMenuItems(target: TreeMenuTarget, ctx: TreeMenuContext): TreeMenuEntry[] {
  const admin = ctx.isAdmin;
  if (target.kind === 'group') {
    const devices = allDevices([target.group]).map((x) => x.device);
    const profiles = admin ? (ctx.discoveryProfiles ?? []) : [];
    return sections(
      [item('open')],
      admin ? [item('addDevice'), item('addSubgroup')] : [],
      admin
        ? [
            devices.some((d) => d.enabled) && item('pauseAll'),
            devices.some((d) => !d.enabled) && item('resumeAll'),
            item('maintenance'),
            ...profiles.map((p) =>
              item('discovery', { key: `discovery${p.id}`, arg: p.id, vars: { name: p.name } }),
            ),
          ]
        : [],
      admin ? [item('edit'), item('delete', { danger: true })] : [],
    );
  }
  if (target.kind === 'device') {
    const d = target.device;
    const hasPorts = d.sensors.some((s) => ctx.snmpTypes.has(s.type));
    const explain = aiExplainOn(ctx) ? explainSensorOf(d) : null;
    return sections(
      [
        item('open'),
        item('openNewTab'),
        hasPorts && item('tabPorts'),
        ctx.features.timeline && item('tabTimeline'),
        ctx.features.config_backup && item('tabConfig'),
      ],
      admin ? [item('addSensor'), item('applyTemplate')] : [],
      admin
        ? [
            item('runAll', { disabled: runnableSensors(d, ctx.isPassive).length === 0 }),
            item(d.enabled ? 'pause' : 'resume'),
            item('maintenance'),
          ]
        : [],
      [explain && item('explain', { arg: explain.id })],
      [item('copyIp'), item('copyName')],
      admin ? [item('edit'), item('delete', { danger: true })] : [],
    );
  }
  const s = target.sensor;
  const passive = ctx.isPassive(s.type);
  return sections(
    [item('open')],
    admin
      ? [
          !passive && item('runNow', { disabled: !canRun(s) || !target.device.enabled }),
          item(s.enabled ? 'pause' : 'resume'),
          canAcknowledge(s) && !s.acknowledged && item('acknowledge'),
        ]
      : [],
    [aiExplainOn(ctx) && explainable(s.status) && item('explain', { arg: s.id })],
    admin ? [item('edit'), item('delete', { danger: true })] : [],
  );
}

/** i18n key of an entry's label ("Pause all" etc.; the menu's own label names the item). */
export function treeMenuLabelKey(action: TreeMenuAction): string {
  return `treeMenu.action.${action}`;
}

// ---------------------------------------------------------------------------------------------
// Positioning

export type Point = { x: number; y: number };
export type Size = { width: number; height: number };

/**
 * Top-left corner of a menu opened at `anchor`. In LTR the menu grows to the right of the pointer,
 * in RTL to its left (mirrored); it flips to the other side when it would overflow, and is finally
 * clamped into the viewport with `margin`.
 */
export function placeMenu(
  anchor: Point,
  menu: Size,
  viewport: Size,
  rtl: boolean,
  margin = 4,
): { left: number; top: number } {
  const maxLeft = viewport.width - margin - menu.width;
  const maxTop = viewport.height - margin - menu.height;
  let left: number;
  if (!rtl) {
    left = anchor.x;
    if (left > maxLeft) left = anchor.x - menu.width;
  } else {
    left = anchor.x - menu.width;
    if (left < margin) left = anchor.x;
  }
  let top = anchor.y;
  if (top > maxTop) top = anchor.y - menu.height;
  left = Math.max(margin, Math.min(left, maxLeft));
  top = Math.max(margin, Math.min(top, maxTop));
  return { left, top };
}

/** Where a menu opened from the keyboard goes: under the item, at its inline start. */
export function keyboardAnchor(
  rect: { left: number; right: number; bottom: number },
  rtl: boolean,
): Point {
  return { x: rtl ? rect.right : rect.left, y: rect.bottom };
}

// ---------------------------------------------------------------------------------------------
// Keyboard

/** The keys that open the menu of the focused tree item: the ContextMenu key and Shift+F10. */
export function isMenuKey(e: { key: string; shiftKey: boolean }): boolean {
  return e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10');
}

/**
 * The menu item to focus after `key` (ArrowDown/ArrowUp wrap, Home/End, or a printable character
 * for type-ahead on the labels). Disabled items are skipped. Returns `current` when nothing moves.
 */
export function nextMenuIndex(
  items: { label: string; disabled?: boolean }[],
  current: number,
  key: string,
): number {
  const enabled = items.map((x, i) => (x.disabled ? -1 : i)).filter((i) => i >= 0);
  if (!enabled.length) return current;
  const pos = enabled.indexOf(current);
  switch (key) {
    case 'ArrowDown':
      return pos < 0 ? enabled[0] : enabled[(pos + 1) % enabled.length];
    case 'ArrowUp':
      return pos < 0
        ? enabled[enabled.length - 1]
        : enabled[(pos - 1 + enabled.length) % enabled.length];
    case 'Home':
      return enabled[0];
    case 'End':
      return enabled[enabled.length - 1];
  }
  if (key.length === 1 && key.trim()) {
    const ch = key.toLocaleLowerCase();
    const start = pos < 0 ? 0 : pos + 1;
    for (let n = 0; n < enabled.length; n++) {
      const i = enabled[(start + n) % enabled.length];
      if (items[i].label.trim().toLocaleLowerCase().startsWith(ch)) return i;
    }
  }
  return current;
}
