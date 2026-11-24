// [tree-move] Moving devices and groups between groups: the tree's multi-selection, native HTML5
// drag and drop, the "Move to…" group picker and the confirmation. Nothing moves before the user
// confirms; the server checks everything again (POST /api/tree/move, all or nothing).
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { endpoints, qk } from '../../api/endpoints';
import type { TreeGroup } from '../../api/types';
import { isApiError } from '../../api/client';
import { useAuth } from '../../hooks/auth';
import { useTree } from '../../hooks/queries';
import { errorMessage } from '../../lib/errors';
import {
  dropCheck,
  indexForMove,
  moveBody,
  moveCounts,
  normalizeMove,
  pickerOptions,
  refKey,
  refName,
  type DropCheck,
  type MoveIndex,
  type MoveRef,
} from '../../lib/treeMove';
import { FolderIcon, ServerIcon } from '../icons';
import { RADIO } from '../kit';
import { useToast } from '../toast';
import { Button, cx, Input, Modal } from '../ui';

/** The drag in progress (dragged in this window; other drags are ignored). */
export type TreeDrag = { refs: MoveRef[]; over: string | null; ok: boolean };

export type TreeMoveApi = {
  /** the user may move items (admins); otherwise no selection, drag or "Move to…" */
  canMove: boolean;
  selection: ReadonlySet<string>;
  anchor: string | null;
  setSelection: (sel: Set<string>, anchor: string | null) => void;
  clearSelection: () => void;
  drag: TreeDrag | null;
  beginDrag: (refs: MoveRef[], e: React.DragEvent<HTMLElement>) => void;
  endDrag: () => void;
  /** the drag is over `key` (a row or the top-level zone); returns whether a drop is allowed */
  dragOver: (key: string, target: number | null) => DropCheck;
  dragLeave: (key: string) => void;
  /** a drop: opens the confirmation */
  drop: (target: number | null) => void;
  /** "Move to…": the group picker, then the confirmation */
  openPicker: (refs: MoveRef[]) => void;
  /** the tree shows the target after a move (expands it and its ancestors) */
  setRevealer: (fn: ((groupIds: number[]) => void) | null) => void;
};

const Ctx = createContext<TreeMoveApi | null>(null);

export function useTreeMove(): TreeMoveApi | null {
  return useContext(Ctx);
}

type Step =
  { kind: 'pick'; refs: MoveRef[] } | { kind: 'confirm'; refs: MoveRef[]; target: number | null };

const MIME = 'application/x-netmon-tree';

/** "3 devices and 1 group" (Hebrew: "3 התקנים וקבוצה אחת"). */
export function describeItems(t: TFunction, refs: MoveRef[]): string {
  const { devices, groups } = moveCounts(refs);
  const parts = [
    devices ? t('treeMove.devices', { count: devices }) : '',
    groups ? t('treeMove.groups', { count: groups }) : '',
  ].filter(Boolean);
  if (parts.length < 2) return parts[0] ?? '';
  return t(/^\d/.test(parts[1]) ? 'treeMove.andNum' : 'treeMove.and', {
    a: parts[0],
    b: parts[1],
  });
}

function targetName(t: TFunction, ix: MoveIndex, target: number | null): string {
  return target === null
    ? t('treeMove.topLevelTarget')
    : (ix.groups.get(target)?.name ?? `#${target}`);
}

/** The move's error in words (a name clash names the group). */
function moveError(t: TFunction, e: unknown, ix: MoveIndex): string {
  if (isApiError(e) && e.code === 'conflict') {
    const gid = e.details?.find((d) => typeof d.group_id === 'number')?.group_id;
    if (gid !== undefined)
      return t('treeMove.errors.nameTaken', { name: ix.groups.get(gid)?.name ?? `#${gid}` });
  }
  if (isApiError(e) && e.code === 'validation_error')
    return `${t('treeMove.errors.changed')} ${errorMessage(t, e)}`;
  return errorMessage(t, e, { deviceName: (id) => ix.devices.get(id)?.device.name });
}

export function TreeMoveProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const tree = useTree();
  const qc = useQueryClient();
  const toast = useToast();
  const [selection, setSel] = useState<ReadonlySet<string>>(() => new Set());
  const [anchor, setAnchor] = useState<string | null>(null);
  const [drag, setDrag] = useState<TreeDrag | null>(null);
  const [step, setStep] = useState<Step | null>(null);
  const revealer = useRef<((groupIds: number[]) => void) | null>(null);
  const dragRef = useRef<TreeDrag | null>(null);
  dragRef.current = drag;
  const canMove = isAdmin;

  const groups = tree.data?.groups;
  const ix = useMemo(() => indexForMove(groups ?? []), [groups]);
  const ixRef = useRef(ix);
  ixRef.current = ix;

  // Rows that no longer exist (deleted, or the user lost the role) leave the selection.
  useEffect(() => {
    if (!canMove) {
      if (selection.size) setSel(new Set());
      return;
    }
    const alive = [...selection].filter((k) =>
      k[0] === 'g' ? ix.groups.has(Number(k.slice(1))) : ix.devices.has(Number(k.slice(1))),
    );
    if (alive.length !== selection.size) setSel(new Set(alive));
  }, [ix, canMove, selection]);

  // Escape clears the selection (when no dialog or menu takes it first).
  useEffect(() => {
    if (!selection.size) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return;
      if (document.querySelector('[role="dialog"][aria-modal="true"], [role="menu"]')) return;
      setSel(new Set());
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [selection.size]);

  const setSelection = useCallback((sel: Set<string>, a: string | null) => {
    setSel(sel);
    setAnchor(a);
  }, []);
  const clearSelection = useCallback(() => setSel(new Set()), []);

  const beginDrag = useCallback(
    (refs: MoveRef[], e: React.DragEvent<HTMLElement>) => {
      const items = normalizeMove(ixRef.current, refs);
      if (!canMove || !items.length) return;
      const dt = e.dataTransfer;
      dt.effectAllowed = 'move';
      dt.setData(MIME, JSON.stringify(items));
      dt.setData('text/plain', items.map((r) => refName(ixRef.current, r)).join('\n'));
      if (items.length > 1) {
        // a chip "3 devices" under the pointer instead of the one row the drag started on
        const chip = document.createElement('div');
        chip.textContent = describeItems(t, items);
        chip.dir = document.documentElement.dir || 'ltr';
        chip.style.cssText =
          'position:fixed;top:-100px;left:-100px;padding:6px 12px;border-radius:999px;' +
          'font:600 13px system-ui,sans-serif;background:var(--accent);color:var(--on-accent);' +
          'white-space:nowrap';
        document.body.appendChild(chip);
        dt.setDragImage(chip, 12, 14);
        setTimeout(() => chip.remove(), 0);
      }
      setDrag({ refs: items, over: null, ok: false });
    },
    [canMove, t],
  );
  const endDrag = useCallback(() => setDrag(null), []);

  const dragOver = useCallback((key: string, target: number | null): DropCheck => {
    const d = dragRef.current;
    if (!d) return 'empty';
    const check = dropCheck(ixRef.current, d.refs, target);
    const ok = check === 'ok';
    if (d.over !== key || d.ok !== ok) setDrag({ ...d, over: key, ok });
    return check;
  }, []);
  const dragLeave = useCallback((key: string) => {
    const d = dragRef.current;
    if (d && d.over === key) setDrag({ ...d, over: null, ok: false });
  }, []);
  const drop = useCallback((target: number | null) => {
    const d = dragRef.current;
    setDrag(null);
    if (!d || dropCheck(ixRef.current, d.refs, target) !== 'ok') return;
    setStep({ kind: 'confirm', refs: d.refs, target });
  }, []);

  const openPicker = useCallback(
    (refs: MoveRef[]) => {
      const items = normalizeMove(ixRef.current, refs);
      if (canMove && items.length) setStep({ kind: 'pick', refs: items });
    },
    [canMove],
  );
  const setRevealer = useCallback((fn: ((groupIds: number[]) => void) | null) => {
    revealer.current = fn;
  }, []);

  const move = useMutation({
    mutationFn: (s: { refs: MoveRef[]; target: number | null }) =>
      endpoints.treeMove(moveBody(ixRef.current, s.refs, s.target)),
    onSuccess: (r, s) => {
      void qc.invalidateQueries({ queryKey: qk.tree });
      void qc.invalidateQueries({ queryKey: qk.dashboard });
      void qc.invalidateQueries({ queryKey: ['devices'] });
      void qc.invalidateQueries({ queryKey: ['device'] });
      void qc.invalidateQueries({ queryKey: ['groups'] });
      void qc.invalidateQueries({ queryKey: ['group'] });
      void qc.invalidateQueries({ queryKey: ['overview-map'] });
      if (r.target_group_id !== null) revealer.current?.([r.target_group_id]);
      setSel(new Set());
      setStep(null);
      toast.push('success', t('treeMove.done', { target: targetName(t, ixRef.current, s.target) }));
    },
  });

  const value = useMemo<TreeMoveApi>(
    () => ({
      canMove,
      selection,
      anchor,
      setSelection,
      clearSelection,
      drag,
      beginDrag,
      endDrag,
      dragOver,
      dragLeave,
      drop,
      openPicker,
      setRevealer,
    }),
    [
      canMove,
      selection,
      anchor,
      setSelection,
      clearSelection,
      drag,
      beginDrag,
      endDrag,
      dragOver,
      dragLeave,
      drop,
      openPicker,
      setRevealer,
    ],
  );

  const close = () => {
    move.reset();
    setStep(null);
  };

  return (
    <Ctx.Provider value={value}>
      {children}
      {step?.kind === 'pick' && groups && (
        <MovePicker
          groups={groups}
          ix={ix}
          refs={step.refs}
          onClose={close}
          onPick={(target) => setStep({ kind: 'confirm', refs: step.refs, target })}
        />
      )}
      {step?.kind === 'confirm' && (
        <MoveConfirm
          ix={ix}
          refs={step.refs}
          target={step.target}
          busy={move.isPending}
          error={move.error ? moveError(t, move.error, ix) : null}
          onClose={close}
          onConfirm={() => move.mutate({ refs: step.refs, target: step.target })}
        />
      )}
    </Ctx.Provider>
  );
}

const MAX_LISTED = 8;

function MoveConfirm({
  ix,
  refs,
  target,
  busy,
  error,
  onClose,
  onConfirm,
}: {
  ix: MoveIndex;
  refs: MoveRef[];
  target: number | null;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const { t } = useTranslation();
  const items = normalizeMove(ix, refs);
  const listed = items.slice(0, MAX_LISTED);
  return (
    <Modal
      open
      aboveMenu
      testId="move-confirm"
      title={t('treeMove.confirmTitle', {
        what: describeItems(t, items),
        target: targetName(t, ix, target),
      })}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button variant="primary" busy={busy} onClick={onConfirm} data-testid="move-confirm-ok">
            {t('treeMove.move')}
          </Button>
        </>
      }
    >
      <ul className="m-0 grid list-none gap-1 p-0 text-sm" data-testid="move-confirm-items">
        {listed.map((r) => (
          <li key={refKey(r)} className="flex min-w-0 items-center gap-2">
            {r.kind === 'group' ? (
              <FolderIcon size={14} className="shrink-0 text-ink-subtle" />
            ) : (
              <ServerIcon size={14} className="shrink-0 text-ink-subtle" />
            )}
            <bdi className="min-w-0 truncate font-medium">{refName(ix, r)}</bdi>
          </li>
        ))}
        {items.length > listed.length && (
          <li className="text-ink-muted">
            {t('treeMove.andMore', { count: items.length - listed.length })}
          </li>
        )}
      </ul>
      <p className="m-0 mt-3 text-[13.5px] text-ink-muted" data-testid="move-confirm-hint">
        {t('treeMove.inheritHint')}
      </p>
      {error && (
        <div
          role="alert"
          className="mt-3 rounded-lg bg-status-down-soft px-4 py-3 text-[14.5px] text-ink"
        >
          {error}
        </div>
      )}
    </Modal>
  );
}

function MovePicker({
  groups,
  ix,
  refs,
  onClose,
  onPick,
}: {
  groups: TreeGroup[];
  ix: MoveIndex;
  refs: MoveRef[];
  onClose: () => void;
  onPick: (target: number | null) => void;
}) {
  const { t } = useTranslation();
  const [q, setQ] = useState('');
  const [picked, setPicked] = useState<number | null | undefined>(undefined);
  const options = useMemo(() => pickerOptions(groups, ix, refs, q), [groups, ix, refs, q]);
  const valid = picked !== undefined && options.some((o) => o.id === picked && o.check === 'ok');
  const note = (c: DropCheck) =>
    c === 'same'
      ? t('treeMove.note.same')
      : c === 'self' || c === 'cycle'
        ? t('treeMove.note.inside')
        : '';
  return (
    <Modal
      open
      aboveMenu
      testId="move-picker"
      title={t('treeMove.pickTitle', { what: describeItems(t, refs) })}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button
            variant="primary"
            disabled={!valid}
            onClick={() => valid && onPick(picked ?? null)}
            data-testid="move-picker-next"
          >
            {t('treeMove.next')}
          </Button>
        </>
      }
    >
      <Input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder={t('treeMove.findGroup')}
        aria-label={t('treeMove.findGroup')}
        className="mb-3 h-9 text-sm"
      />
      <div
        role="radiogroup"
        aria-label={t('treeMove.targetGroup')}
        className="grid max-h-[50vh] gap-0.5 overflow-y-auto"
        data-testid="move-picker-list"
      >
        {!options.length && <p className="m-0 p-2 text-sm text-ink-muted">{t('tree.noMatch')}</p>}
        {options.map((o) => {
          const ok = o.check === 'ok';
          const label = o.id === null ? t('treeMove.topLevel') : o.name;
          return (
            <label
              key={o.id ?? 'root'}
              data-testid="move-picker-option"
              data-disabled={!ok || undefined}
              className={cx(
                'flex min-h-[36px] items-center gap-2.5 rounded-[10px] pe-2 text-sm',
                ok ? 'cursor-pointer hover:bg-surface-hover' : 'cursor-not-allowed text-ink-subtle',
                picked === o.id && ok && 'bg-nav-active font-semibold',
              )}
              style={{ paddingInlineStart: `${o.depth * 16 + 8}px` }}
            >
              <input
                type="radio"
                className={cx(RADIO, 'disabled:cursor-not-allowed disabled:opacity-40')}
                name="move-target"
                aria-label={label}
                checked={picked === o.id}
                disabled={!ok}
                onChange={() => setPicked(o.id)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && ok) {
                    e.preventDefault();
                    onPick(o.id);
                  }
                }}
              />
              <FolderIcon size={14} className="shrink-0 text-ink-subtle" />
              <bdi className="min-w-0 flex-1 truncate">{label}</bdi>
              {!ok && note(o.check) && (
                <span className="shrink-0 text-xs text-ink-subtle">{note(o.check)}</span>
              )}
            </label>
          );
        })}
      </div>
    </Modal>
  );
}
