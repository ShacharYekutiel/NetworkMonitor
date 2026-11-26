// [3b] Right-click / long-press / Shift+F10 context menu of the sidebar device tree. Reuses the
// existing endpoints and dialogs; the tree refreshes through query invalidation and the WS cache.
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { useLocation, useNavigate } from 'react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../../api/endpoints';
import type { Device, Group, Sensor, TreeDevice, TreeGroup, TreeSensor } from '../../api/types';
import { useAuth } from '../../hooks/auth';
import { useFeatures } from '../../hooks/features';
import { useSensorTypes, useTree } from '../../hooks/queries';
import { errorMessage } from '../../lib/errors';
import { allDevices } from '../../lib/tree';
import { refKey, selectedRefs } from '../../lib/treeMove';
import {
  isMenuKey,
  keyboardAnchor,
  runnableSensors,
  treeMenuItems,
  treeMenuLabelKey,
  type Point,
  type TreeMenuAction,
  type TreeMenuItem,
  type TreeMenuTarget,
} from '../../lib/treeMenu';
import { AckDialog } from '../Acknowledge';
import { ExplainDrawer } from '../assistant/ExplainButton';
import { DeviceFormModal, GroupFormModal } from '../EntityForms';
import { isEventSensor } from '../EventConditions';
import {
  CheckIcon,
  ClockIcon,
  CopyIcon,
  EditIcon,
  LayersIcon,
  LinkIcon,
  PauseIcon,
  PlayIcon,
  PlusIcon,
  PortIcon,
  RadarIcon,
  RefreshIcon,
  SparkIcon,
  TrashIcon,
  WrenchIcon,
  BackupIcon,
  ChevronIcon,
  MoveIcon,
} from '../icons';
import { MaintenanceWindowModal } from '../Maintenance';
import { AddSensorModal, EditSensorModal } from '../SensorForms';
import { ApplyTemplateModal } from '../Templates';
import { useToast } from '../toast';
import { ConfirmDialog, cx } from '../ui';
import { ContextMenu, type CloseReason, type ContextMenuEntry } from './ContextMenu';
import { TreeMoveProvider, useTreeMove } from './TreeMove';

type OpenFn = (target: TreeMenuTarget, anchor: Point, opener: HTMLElement | null) => void;
/** A group / device / sensor named by id (rows outside the tree: the menu finds it in the tree). */
export type EntityRef = { kind: 'group' | 'device' | 'sensor'; id: number };
type TreeMenuApi = {
  open: OpenFn;
  isOpen: () => boolean;
  /** the tree node of an entity shown elsewhere (null while the tree is loading / unknown id) */
  resolve: (ref: EntityRef) => TreeMenuTarget | null;
};
const Ctx = createContext<TreeMenuApi | null>(null);

/** Opens the tree context menu (null outside the provider: the tree then has no menu). */
export function useTreeMenu(): TreeMenuApi | null {
  return useContext(Ctx);
}

const LONG_PRESS_MS = 500;
const MOVE_TOLERANCE = 10;

function documentRtl(): boolean {
  return document.documentElement.dir === 'rtl';
}

/**
 * Where the browser's own menu must stay: inside form fields and editable text, and on a text
 * selection the pointer is on (copy / search).
 */
export function keepsNativeMenu(target: EventTarget | null, x?: number, y?: number): boolean {
  const el = target instanceof Element ? target : null;
  if (el?.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"]'))
    return true;
  const sel = typeof window !== 'undefined' ? window.getSelection?.() : null;
  if (!sel || sel.isCollapsed || !sel.rangeCount || !el) return false;
  const range = sel.getRangeAt(0);
  if (!range.intersectsNode(el)) return false;
  if (x === undefined || y === undefined || typeof range.getClientRects !== 'function') return true;
  return Array.from(range.getClientRects()).some(
    (r) => x >= r.left && x <= r.right && y >= r.top && y <= r.bottom,
  );
}

type MenuProps = {
  'aria-keyshortcuts'?: string;
  onContextMenu?: (e: React.MouseEvent<HTMLElement>) => void;
  onKeyDown?: (e: React.KeyboardEvent<HTMLElement>) => void;
  onTouchStart?: (e: React.TouchEvent<HTMLElement>) => void;
  onTouchMove?: (e: React.TouchEvent<HTMLElement>) => void;
  onTouchEnd?: (e: React.TouchEvent<HTMLElement>) => void;
  onTouchCancel?: () => void;
  onClick?: (e: React.MouseEvent<HTMLElement>) => void;
};

/**
 * Handlers that open the context menu: right click (the browser menu is prevented on menu items
 * only), a ~500 ms long press on touch screens, and the ContextMenu key / Shift+F10 while the item
 * (or a link / button inside a row) has focus (the menu then opens under the item). One factory
 * serves any number of rows (one touch at a time).
 */
function useMenuFactory(): ((get: () => TreeMenuTarget | null) => MenuProps) | null {
  const api = useTreeMenu();
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const touchStart = useRef<Point | null>(null);
  const longPressed = useRef(false);
  const openedAt = useRef(0);
  if (!api) return null;
  /** `viaOther`: opened by the keyboard / a long press, for which browsers also fire contextmenu */
  const fire = (target: TreeMenuTarget, anchor: Point, el: HTMLElement, viaOther = false) => {
    openedAt.current = viaOther ? Date.now() : 0;
    api.open(target, anchor, el);
  };
  const cancel = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  };
  return (get) => ({
    'aria-keyshortcuts': 'Shift+F10',
    onContextMenu: (e) => {
      const pointer = e.clientX !== 0 || e.clientY !== 0;
      if (keepsNativeMenu(e.target, pointer ? e.clientX : undefined, e.clientY)) return;
      // the browser's own contextmenu that follows a keyboard / long-press opening: keep that menu
      if (api.isOpen() && Date.now() - openedAt.current < 800) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      const target = get();
      if (!target) return; // not known yet: the browser menu stays
      e.preventDefault();
      e.stopPropagation(); // an inner item (a device link in a sensor row) wins over the row
      const el = e.currentTarget;
      const focused = e.target instanceof HTMLElement ? e.target : el;
      fire(
        target,
        pointer
          ? { x: e.clientX, y: e.clientY }
          : keyboardAnchor(el.getBoundingClientRect(), documentRtl()),
        focused.closest<HTMLElement>('a, button, [tabindex]') ?? el,
      );
    },
    onKeyDown: (e) => {
      if (!isMenuKey(e) || keepsNativeMenu(e.target)) return;
      const target = get();
      if (!target) return;
      e.preventDefault();
      e.stopPropagation();
      const el = e.currentTarget;
      const focused = e.target instanceof HTMLElement ? e.target : el;
      fire(target, keyboardAnchor(el.getBoundingClientRect(), documentRtl()), focused, true);
    },
    onTouchStart: (e) => {
      cancel();
      longPressed.current = false;
      if (e.touches.length !== 1 || keepsNativeMenu(e.target)) return;
      const p = { x: e.touches[0].clientX, y: e.touches[0].clientY };
      const el = e.currentTarget;
      e.stopPropagation();
      touchStart.current = p;
      timer.current = setTimeout(() => {
        timer.current = null;
        const target = get();
        if (!target) return;
        longPressed.current = true;
        fire(target, p, el, true);
      }, LONG_PRESS_MS);
    },
    onTouchMove: (e) => {
      const p = touchStart.current;
      const tch = e.touches[0];
      if (
        p &&
        tch &&
        (Math.abs(tch.clientX - p.x) > MOVE_TOLERANCE ||
          Math.abs(tch.clientY - p.y) > MOVE_TOLERANCE)
      )
        cancel();
    },
    onTouchEnd: (e) => {
      cancel();
      // no click / compatibility mouse events after a long press (they would navigate or close)
      if (longPressed.current && e.cancelable) e.preventDefault();
    },
    onTouchCancel: cancel,
    onClick: (e) => {
      if (longPressed.current) {
        longPressed.current = false;
        e.preventDefault();
      }
    },
  });
}

/** The context-menu handlers of a tree item (spread on the item). */
export function useTreeItemMenu(target: TreeMenuTarget): MenuProps {
  const factory = useMenuFactory();
  return factory ? factory(() => target) : {};
}

/**
 * The same context menu for groups / devices / sensors shown as rows, cards or nodes anywhere
 * (home, group, device, events, find): `menu({ kind: 'device', id })` gives the handlers to spread
 * on the row. Usable inside loops; without the provider (kiosk) it gives nothing.
 */
export function useEntityMenu(): (ref: EntityRef | null | undefined) => MenuProps {
  const factory = useMenuFactory();
  const api = useTreeMenu();
  return (ref) => (factory && api && ref ? factory(() => api.resolve(ref)) : {});
}

type Dialog =
  | { kind: 'addDevice'; groupId: number }
  | { kind: 'addSubgroup'; groupId: number }
  | { kind: 'editGroup'; groupId: number }
  | { kind: 'deleteGroup'; groupId: number; name: string; count: number; parentId: number | null }
  | { kind: 'maintenance'; scope: 'group' | 'device'; id: number; name: string }
  | { kind: 'addSensor'; deviceId: number }
  | { kind: 'applyTemplate'; deviceId: number }
  | { kind: 'editDevice'; deviceId: number }
  | { kind: 'deleteDevice'; deviceId: number; name: string; sensorIds: number[] }
  | { kind: 'editSensor'; sensorId: number }
  | { kind: 'deleteSensor'; sensorId: number; name: string; deviceId: number }
  | { kind: 'ack'; sensor: { id: number; name: string; device_name: string } }
  | { kind: 'explain'; sensorId: number };

type Bulk = { label: string; done: number; total: number };

const ICONS: Partial<Record<TreeMenuAction, ReactNode>> = {
  openNewTab: <LinkIcon size={14} />,
  addDevice: <PlusIcon size={14} />,
  addSubgroup: <PlusIcon size={14} />,
  addSensor: <PlusIcon size={14} />,
  pauseAll: <PauseIcon size={14} />,
  resumeAll: <PlayIcon size={14} className="rtl:rotate-180" />,
  pause: <PauseIcon size={14} />,
  resume: <PlayIcon size={14} className="rtl:rotate-180" />,
  maintenance: <WrenchIcon size={14} />,
  discovery: <RadarIcon size={14} />,
  applyTemplate: <LayersIcon size={14} />,
  runAll: <RefreshIcon size={14} />,
  runNow: <RefreshIcon size={14} />,
  tabPorts: <PortIcon size={14} />,
  tabTimeline: <ClockIcon size={14} />,
  tabConfig: <BackupIcon size={14} />,
  explain: <SparkIcon size={14} className="text-accent-ink" />,
  copyIp: <CopyIcon size={14} />,
  copyName: <CopyIcon size={14} />,
  acknowledge: <CheckIcon size={14} />,
  moveTo: <MoveIcon size={14} className="rtl:-scale-x-100" />,
  moveSelected: <MoveIcon size={14} className="rtl:-scale-x-100" />,
  edit: <EditIcon size={14} />,
  delete: <TrashIcon size={14} />,
  open: <ChevronIcon size={14} className="rtl:rotate-180" />,
};

function targetName(t: TreeMenuTarget): string {
  return t.kind === 'group' ? t.group.name : t.kind === 'device' ? t.device.name : t.sensor.name;
}

function targetPath(t: TreeMenuTarget): string {
  return t.kind === 'group'
    ? `/groups/${t.group.id}`
    : t.kind === 'device'
      ? `/devices/${t.device.id}`
      : `/sensors/${t.sensor.id}`;
}

async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  // Fallback for non-secure origins (plain-HTTP installs): a hidden textarea + execCommand.
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  const ok = document.execCommand('copy');
  ta.remove();
  if (!ok) throw new Error('copy failed');
}

/** Renders `children` with the full entity once it is loaded (shares the page queries' cache). */
function WithDevice({ id, children }: { id: number; children: (d: Device) => ReactNode }) {
  const q = useQuery({ queryKey: qk.device(id), queryFn: () => endpoints.device(id) });
  return q.data ? <>{children(q.data)}</> : null;
}
function WithGroup({ id, children }: { id: number; children: (g: Group) => ReactNode }) {
  const q = useQuery({ queryKey: qk.group(id), queryFn: () => endpoints.group(id) });
  return q.data ? <>{children(q.data)}</> : null;
}
function WithSensor({ id, children }: { id: number; children: (s: Sensor) => ReactNode }) {
  const q = useQuery({ queryKey: qk.sensor(id), queryFn: () => endpoints.sensor(id) });
  return q.data ? <>{children(q.data)}</> : null;
}

const isPassive = (type: string) => isEventSensor({ type: type as Sensor['type'] });

type TreeIndex = {
  groups: Map<number, TreeGroup>;
  devices: Map<number, TreeDevice>;
  sensors: Map<number, { sensor: TreeSensor; device: TreeDevice }>;
};

function indexTree(groups: TreeGroup[]): TreeIndex {
  const ix: TreeIndex = { groups: new Map(), devices: new Map(), sensors: new Map() };
  const walk = (g: TreeGroup) => {
    ix.groups.set(g.id, g);
    for (const d of g.devices) {
      ix.devices.set(d.id, d);
      for (const s of d.sensors) ix.sensors.set(s.id, { sensor: s, device: d });
    }
    g.children.forEach(walk);
  };
  groups.forEach(walk);
  return ix;
}

function lookup(ix: TreeIndex, ref: EntityRef): TreeMenuTarget | null {
  if (ref.kind === 'group') {
    const group = ix.groups.get(ref.id);
    return group ? { kind: 'group', group } : null;
  }
  if (ref.kind === 'device') {
    const device = ix.devices.get(ref.id);
    return device ? { kind: 'device', device } : null;
  }
  const hit = ix.sensors.get(ref.id);
  return hit ? { kind: 'sensor', sensor: hit.sensor, device: hit.device } : null;
}

type MenuState = { target: TreeMenuTarget; anchor: Point; opener: HTMLElement | null; n: number };

/** The tree context menu and [tree-move] moving (selection, drag and drop, "Move to…"). */
export function TreeMenuProvider({ children }: { children: ReactNode }) {
  return (
    <TreeMoveProvider>
      <TreeMenuProviderInner>{children}</TreeMenuProviderInner>
    </TreeMoveProvider>
  );
}

function TreeMenuProviderInner({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const mover = useTreeMove();
  const { isAdmin } = useAuth();
  const { features } = useFeatures();
  const sensorTypes = useSensorTypes();
  const navigate = useNavigate();
  const location = useLocation();
  const qc = useQueryClient();
  const toast = useToast();
  const [menu, setMenu] = useState<MenuState | null>(null);
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [bulk, setBulk] = useState<Bulk | null>(null);
  const counter = useRef(0);
  const menuOpen = useRef(false);
  menuOpen.current = menu !== null;

  const open = useCallback<OpenFn>((target, anchor, opener) => {
    counter.current += 1;
    setMenu({ target, anchor, opener, n: counter.current });
  }, []);
  // Rows outside the tree name an entity by id; the menu acts on its tree node (same actions).
  const tree = useTree();
  const index = useRef<TreeIndex | null>(null);
  const indexed = useRef<unknown>(null);
  const resolve = useCallback(
    (ref: EntityRef): TreeMenuTarget | null => {
      const data = qc.getQueryData<{ groups: TreeGroup[] }>(qk.tree) ?? tree.data;
      if (!data) return null;
      if (indexed.current !== data) {
        index.current = indexTree(data.groups);
        indexed.current = data;
      }
      return lookup(index.current!, ref);
    },
    [qc, tree.data],
  );
  const ctxValue = useMemo<TreeMenuApi>(
    () => ({ open, isOpen: () => menuOpen.current, resolve }),
    [open, resolve],
  );

  // Discovery profiles of the group (admins; only fetched while a group menu is open).
  const groupId = menu?.target.kind === 'group' ? menu.target.group.id : null;
  const profiles = useQuery({
    queryKey: qk.discoveryProfiles,
    queryFn: endpoints.discoveryProfiles,
    enabled: isAdmin && groupId !== null,
    staleTime: 30_000,
  });

  const snmpTypes = useMemo(
    () => new Set(sensorTypes.data?.filter((x) => x.requires_snmp).map((x) => x.type) ?? []),
    [sensorTypes.data],
  );

  const selection = mover?.selection;
  const items = useMemo(() => {
    if (!menu) return [];
    const tg = menu.target;
    const key =
      tg.kind === 'group'
        ? refKey({ kind: 'group', id: tg.group.id })
        : tg.kind === 'device'
          ? refKey({ kind: 'device', id: tg.device.id })
          : null;
    return treeMenuItems(tg, {
      canMove: !!mover?.canMove,
      selectedCount: key && selection?.has(key) ? selection.size : 0,
      isAdmin,
      features,
      snmpTypes,
      isPassive,
      discoveryProfiles:
        groupId !== null
          ? (profiles.data?.items ?? [])
              .filter((p) => p.group_id === groupId)
              .map((p) => ({ id: p.id, name: p.name }))
          : [],
    });
  }, [menu, isAdmin, features, snmpTypes, profiles.data, groupId, mover?.canMove, selection]);

  const entries: ContextMenuEntry[] = useMemo(
    () =>
      items.map((e) =>
        e.type === 'separator'
          ? e
          : {
              type: 'item',
              key: e.key,
              label: t(treeMenuLabelKey(e.action), e.vars ?? {}),
              icon: ICONS[e.action],
              danger: e.danger,
              disabled: e.disabled,
              title:
                e.disabled && (e.action === 'runNow' || e.action === 'runAll')
                  ? t('sensors.runPausedHint')
                  : undefined,
            },
      ),
    [items, t],
  );

  const close = useCallback(
    (reason: CloseReason) => {
      const opener = menu?.opener;
      setMenu(null);
      if (reason !== 'outside' && opener && document.contains(opener)) opener.focus();
    },
    [menu],
  );

  const invalidateTree = () => {
    void qc.invalidateQueries({ queryKey: qk.tree });
    void qc.invalidateQueries({ queryKey: qk.dashboard });
  };

  // --- single actions -------------------------------------------------------------------------
  const deviceEnabled = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      endpoints.updateDevice(id, { enabled }),
    onSuccess: (d) => {
      qc.setQueryData(qk.device(d.id), d);
      void qc.invalidateQueries({ queryKey: ['sensors'] });
      invalidateTree();
      toast.push('success', d.enabled ? t('treeMenu.done.resumed') : t('treeMenu.done.paused'));
    },
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  const sensorEnabled = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      endpoints.updateSensor(id, { enabled }),
    onSuccess: (s) => {
      qc.setQueryData(qk.sensor(s.id), s);
      void qc.invalidateQueries({ queryKey: ['sensors'] });
      invalidateTree();
      toast.push('success', s.enabled ? t('sensors.resumed') : t('sensors.paused'));
    },
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  const runSensor = useMutation({
    mutationFn: ({ id }: { id: number; name: string }) => endpoints.runSensor(id),
    onSuccess: (r, v) => {
      void qc.invalidateQueries({ queryKey: qk.sensor(v.id) });
      void qc.invalidateQueries({ queryKey: ['history', v.id] });
      toast.push(
        r.ok ? 'success' : 'error',
        `${v.name}: ${t(`status.${r.status}`)}${r.message ? ` — ${r.message}` : ''}`,
      );
    },
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  const runDiscovery = useMutation({
    mutationFn: (profileId: number) => endpoints.runDiscoveryProfile(profileId),
    onSuccess: (scan) => {
      void qc.invalidateQueries({ queryKey: ['discoveryScans'] });
      toast.push('success', t('treeMenu.done.discovery'));
      navigate(`/discovery/scans/${scan.id}`);
    },
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });

  /** Sequential calls with a progress panel; one summary toast at the end. */
  const runBulk = async (
    label: string,
    ids: number[],
    call: (id: number) => Promise<unknown>,
    doneKey: string,
  ) => {
    if (!ids.length) return;
    let ok = 0;
    let firstError: unknown = null;
    setBulk({ label, done: 0, total: ids.length });
    for (let i = 0; i < ids.length; i++) {
      try {
        await call(ids[i]);
        ok++;
      } catch (e) {
        firstError ??= e;
      }
      setBulk({ label, done: i + 1, total: ids.length });
    }
    setBulk(null);
    void qc.invalidateQueries({ queryKey: ['sensors'] });
    void qc.invalidateQueries({ queryKey: ['device'] });
    invalidateTree();
    toast.push(firstError ? 'error' : 'success', t(doneKey, { ok, total: ids.length }));
    if (firstError) toast.push('error', errorMessage(t, firstError));
  };

  const select = (key: string) => {
    if (!menu) return;
    const entry = items.find((x): x is TreeMenuItem => x.type === 'item' && x.key === key);
    if (!entry || entry.disabled) return;
    const target = menu.target;
    close('select');
    run(entry, target);
  };

  const run = (entry: TreeMenuItem, target: TreeMenuTarget) => {
    const a = entry.action;
    if (a === 'open') return navigate(targetPath(target));
    if (a === 'moveSelected' && mover) return mover.openPicker(selectedRefs(mover.selection));
    if (a === 'moveTo' && mover && target.kind !== 'sensor')
      return mover.openPicker([
        target.kind === 'group'
          ? { kind: 'group', id: target.group.id }
          : { kind: 'device', id: target.device.id },
      ]);
    if (target.kind === 'group') {
      const g = target.group;
      switch (a) {
        case 'addDevice':
          return setDialog({ kind: 'addDevice', groupId: g.id });
        case 'addSubgroup':
          return setDialog({ kind: 'addSubgroup', groupId: g.id });
        case 'pauseAll':
        case 'resumeAll': {
          const enable = a === 'resumeAll';
          const ids = allDevices([g])
            .map((x) => x.device)
            .filter((d) => d.enabled !== enable)
            .map((d) => d.id);
          void runBulk(
            t(enable ? 'treeMenu.progress.resuming' : 'treeMenu.progress.pausing'),
            ids,
            (id) => endpoints.updateDevice(id, { enabled: enable }),
            enable ? 'treeMenu.done.resumedAll' : 'treeMenu.done.pausedAll',
          );
          return;
        }
        case 'maintenance':
          return setDialog({ kind: 'maintenance', scope: 'group', id: g.id, name: g.name });
        case 'discovery':
          if (entry.arg !== undefined) runDiscovery.mutate(entry.arg);
          return;
        case 'edit':
          return setDialog({ kind: 'editGroup', groupId: g.id });
        case 'delete':
          return setDialog({
            kind: 'deleteGroup',
            groupId: g.id,
            name: g.name,
            count: g.status_counts.total,
            parentId: g.parent_id,
          });
      }
      return;
    }
    if (target.kind === 'device') {
      const d = target.device;
      switch (a) {
        case 'openNewTab':
          window.open(targetPath(target), '_blank', 'noopener');
          return;
        case 'tabPorts':
          return navigate(`/devices/${d.id}?tab=ports`);
        case 'tabTimeline':
          return navigate(`/devices/${d.id}?tab=timeline`);
        case 'tabConfig':
          return navigate(`/devices/${d.id}?tab=config`);
        case 'addSensor':
          return setDialog({ kind: 'addSensor', deviceId: d.id });
        case 'applyTemplate':
          return setDialog({ kind: 'applyTemplate', deviceId: d.id });
        case 'runAll': {
          const list = runnableSensors(d, isPassive);
          void runBulk(
            t('treeMenu.progress.running'),
            list.map((s) => s.id),
            (id) => endpoints.runSensor(id),
            'treeMenu.done.ranAll',
          );
          return;
        }
        case 'pause':
        case 'resume':
          return deviceEnabled.mutate({ id: d.id, enabled: a === 'resume' });
        case 'maintenance':
          return setDialog({ kind: 'maintenance', scope: 'device', id: d.id, name: d.name });
        case 'explain':
          if (entry.arg !== undefined) setDialog({ kind: 'explain', sensorId: entry.arg });
          return;
        case 'copyIp':
        case 'copyName':
          copyText(a === 'copyIp' ? d.host : d.name).then(
            () => toast.push('success', t('treeMenu.done.copied')),
            () => toast.push('error', t('treeMenu.done.copyFailed')),
          );
          return;
        case 'edit':
          return setDialog({ kind: 'editDevice', deviceId: d.id });
        case 'delete':
          return setDialog({
            kind: 'deleteDevice',
            deviceId: d.id,
            name: d.name,
            sensorIds: d.sensors.map((s) => s.id),
          });
      }
      return;
    }
    const s = target.sensor;
    switch (a) {
      case 'runNow':
        return runSensor.mutate({ id: s.id, name: s.name });
      case 'pause':
      case 'resume':
        return sensorEnabled.mutate({ id: s.id, enabled: a === 'resume' });
      case 'acknowledge':
        return setDialog({
          kind: 'ack',
          sensor: { id: s.id, name: s.name, device_name: target.device.name },
        });
      case 'explain':
        return setDialog({ kind: 'explain', sensorId: s.id });
      case 'edit':
        return setDialog({ kind: 'editSensor', sensorId: s.id });
      case 'delete':
        return setDialog({
          kind: 'deleteSensor',
          sensorId: s.id,
          name: s.name,
          deviceId: target.device.id,
        });
    }
  };

  // --- deletes (always confirmed) ---------------------------------------------------------------
  const del = useMutation({
    mutationFn: (d: Dialog) =>
      d.kind === 'deleteGroup'
        ? endpoints.deleteGroup(d.groupId)
        : d.kind === 'deleteDevice'
          ? endpoints.deleteDevice(d.deviceId)
          : d.kind === 'deleteSensor'
            ? endpoints.deleteSensor(d.sensorId)
            : Promise.resolve(),
    onSuccess: (_r, d) => {
      invalidateTree();
      void qc.invalidateQueries({ queryKey: ['sensors'] });
      void qc.invalidateQueries({ queryKey: ['devices'] });
      toast.push('success', t('common.deleted'));
      setDialog(null);
      const path = location.pathname;
      if (d.kind === 'deleteGroup') {
        qc.removeQueries({ queryKey: qk.group(d.groupId) });
        if (path === `/groups/${d.groupId}`) navigate(d.parentId ? `/groups/${d.parentId}` : '/');
      } else if (d.kind === 'deleteDevice') {
        qc.removeQueries({ queryKey: qk.device(d.deviceId) });
        if (
          path === `/devices/${d.deviceId}` ||
          d.sensorIds.some((id) => path === `/sensors/${id}`)
        )
          navigate('/');
      } else if (d.kind === 'deleteSensor') {
        qc.removeQueries({ queryKey: qk.sensor(d.sensorId) });
        if (path === `/sensors/${d.sensorId}`) navigate(`/devices/${d.deviceId}`);
      }
    },
  });

  const closeDialog = () => {
    del.reset();
    setDialog(null);
  };

  return (
    <Ctx.Provider value={ctxValue}>
      {children}
      {menu && (
        <ContextMenu
          key={menu.n}
          anchor={menu.anchor}
          label={t(`treeMenu.label.${menu.target.kind}`, { name: targetName(menu.target) })}
          entries={entries}
          onSelect={select}
          onClose={close}
          testId="tree-menu"
        />
      )}
      {bulk && (
        <div
          role="status"
          aria-live="polite"
          data-testid="tree-bulk-progress"
          // premium progress toast (prototype .ptoast): card at the bottom end, n / total, bar
          className="fixed bottom-6 end-6 z-[60] grid w-[340px] gap-2.5 rounded-[20px] bg-surface px-[18px] py-4 text-sm shadow-[0_18px_60px_rgba(0,0,0,0.2),0_0_0_1px_var(--border)]"
        >
          <div className="flex justify-between gap-2 font-medium">
            <span className="truncate">{bulk.label}</span>
            <span dir="ltr" className="num shrink-0 text-[13px] text-ink-muted">
              {bulk.done}/{bulk.total}
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-chip">
            <div
              className={cx(
                'h-full rounded-full transition-[width]',
                bulk.done >= bulk.total ? 'bg-status-up' : 'bg-ink',
              )}
              style={{ width: `${(bulk.done / bulk.total) * 100}%` }}
            />
          </div>
        </div>
      )}
      {dialog && <DialogView dialog={dialog} onClose={closeDialog} del={del} />}
    </Ctx.Provider>
  );
}

function DialogView({
  dialog: d,
  onClose,
  del,
}: {
  dialog: Dialog;
  onClose: () => void;
  del: { mutate: (d: Dialog) => void; isPending: boolean; error: unknown };
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  switch (d.kind) {
    case 'addDevice':
      return (
        <DeviceFormModal
          open
          onClose={onClose}
          groupId={d.groupId}
          onCreated={(x) => navigate(`/devices/${x.id}`)}
        />
      );
    case 'addSubgroup':
      return <GroupFormModal open onClose={onClose} parentId={d.groupId} />;
    case 'editGroup':
      return (
        <WithGroup id={d.groupId}>
          {(g) => <GroupFormModal open onClose={onClose} group={g} />}
        </WithGroup>
      );
    case 'maintenance':
      return (
        <MaintenanceWindowModal
          open
          onClose={onClose}
          defaults={
            d.scope === 'group'
              ? { scope: 'group', group_id: d.id, name: d.name }
              : { scope: 'device', device_id: d.id, name: d.name }
          }
        />
      );
    case 'addSensor':
      return (
        <WithDevice id={d.deviceId}>
          {(x) => <AddSensorModal open onClose={onClose} device={x} />}
        </WithDevice>
      );
    case 'applyTemplate':
      return (
        <WithDevice id={d.deviceId}>
          {(x) => <ApplyTemplateModal open onClose={onClose} device={x} />}
        </WithDevice>
      );
    case 'editDevice':
      return (
        <WithDevice id={d.deviceId}>
          {(x) => <DeviceFormModal open onClose={onClose} device={x} />}
        </WithDevice>
      );
    case 'editSensor':
      return (
        <WithSensor id={d.sensorId}>
          {(x) => <EditSensorModal open onClose={onClose} sensor={x} />}
        </WithSensor>
      );
    case 'ack':
      return <AckDialog sensor={d.sensor} onClose={onClose} />;
    case 'explain':
      return <ExplainDrawer sensorId={d.sensorId} onClose={onClose} />;
    case 'deleteGroup':
    case 'deleteDevice':
    case 'deleteSensor':
      return (
        <ConfirmDialog
          open
          title={t(
            d.kind === 'deleteGroup'
              ? 'groups.deleteTitle'
              : d.kind === 'deleteDevice'
                ? 'devices.deleteTitle'
                : 'sensors.deleteTitle',
          )}
          message={
            d.kind === 'deleteGroup'
              ? t('groups.deleteConfirm', { name: d.name, count: d.count })
              : d.kind === 'deleteDevice'
                ? t('devices.deleteConfirm', { name: d.name })
                : t('sensors.deleteConfirm', { name: d.name })
          }
          onConfirm={() => del.mutate(d)}
          onClose={onClose}
          busy={del.isPending}
          error={del.error}
        />
      );
  }
}
