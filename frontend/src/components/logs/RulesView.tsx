import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { endpoints as ep, qk } from '../../api/endpoints';
import { LinkScopeRadio } from '../features/IngestSettings';
import { useSaveSettings } from '../features/common';
import { useTranslation } from 'react-i18next';
import { endpoints } from '../../api/endpoints';
import type { EventRule, EventRuleKind } from '../../api/types';
import { useAuth } from '../../hooks/auth';
import { useFeatures, FeatureOffNotice } from '../../hooks/features';
import { reorderPatches, sortRules } from '../../lib/eventRules';
import { errorMessage } from '../../lib/errors';
import { formatDateTime, formatRelative } from '../../lib/format';
import { ChevronIcon, EditIcon, PlusIcon, TrashIcon } from '../icons';
import { useToast } from '../toast';
import { Button, Card, ConfirmDialog, cx, Empty, ErrorBox, Loading, Select } from '../ui';
import { BuiltinBadge, RuleEditorModal } from './RuleEditor';
import { useEventRules } from './rules';

function ActionBadge({ r }: { r: EventRule }) {
  const { t } = useTranslation();
  const cls =
    r.action === 'down'
      ? 'bg-status-down-soft text-status-down'
      : r.action === 'warning'
        ? 'bg-status-warning-soft text-status-warning'
        : 'bg-surface-sunken text-ink-muted';
  return (
    <span className={cx('rounded px-1.5 py-0.5 text-[11px] font-medium', cls)}>
      {t(`eventRules.actions.${r.action}`)}
    </span>
  );
}

/** Admin: link-down/flapping port scope (settings `event_linkdown_scope`), saved at once. */
function LinkScopeCard() {
  const { t } = useTranslation();
  const q = useQuery({ queryKey: qk.settings, queryFn: ep.settings });
  const save = useSaveSettings();
  if (!q.data) return null;
  return (
    <Card title={t('ingestSettings.linkScope.title')}>
      <LinkScopeRadio
        value={q.data.event_linkdown_scope ?? 'important'}
        onChange={(v) => save.mutate({ event_linkdown_scope: v })}
      />
      <ErrorBox error={save.error} className="mt-2" />
    </Card>
  );
}

/** [2b-3] Ordered rule list: drag (or ↑/↓) to reorder → priority, enable toggle, editor. */
export function RulesView() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const { features } = useFeatures();
  const qc = useQueryClient();
  const toast = useToast();
  const rules = useEventRules();
  const [kind, setKind] = useState<EventRuleKind | ''>('');
  const [editing, setEditing] = useState<EventRule | 'new' | null>(null);
  const [deleting, setDeleting] = useState<EventRule | null>(null);
  const [drag, setDrag] = useState<number | null>(null);
  const [over, setOver] = useState<number | null>(null);

  const all = sortRules(rules.data ?? []);
  const list = kind ? all.filter((r) => r.kind === kind) : all;

  const patch = useMutation({
    mutationFn: async (ps: { id: number; priority?: number; enabled?: boolean }[]) => {
      for (const p of ps) {
        const { id, ...body } = p;
        await endpoints.updateEventRule(id, body);
      }
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: ['event-rules'] }),
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  const del = useMutation({
    mutationFn: (r: EventRule) => endpoints.deleteEventRule(r.id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['event-rules'] });
      setDeleting(null);
    },
  });

  /** Moves within the visible list; priorities are computed over the visible (kind) list. */
  const move = (from: number, to: number) => {
    const ps = reorderPatches(list, from, to);
    if (ps.length) patch.mutate(ps);
  };

  return (
    <div className="space-y-3" data-testid="rules-view">
      {!features.event_alerts && <FeatureOffNotice feature="event_alerts" />}
      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={kind}
          onChange={(e) => setKind(e.target.value as EventRuleKind | '')}
          className="w-40"
          aria-label={t('eventRules.kind')}
        >
          <option value="">{t('eventRules.allKinds')}</option>
          <option value="syslog">{t('eventRules.kinds.syslog')}</option>
          <option value="trap">{t('eventRules.kinds.trap')}</option>
        </Select>
        <p className="flex-1 text-xs text-ink-muted">{t('eventRules.orderHint')}</p>
        {isAdmin && (
          <Button variant="primary" onClick={() => setEditing('new')}>
            <PlusIcon size={14} /> {t('eventRules.add')}
          </Button>
        )}
      </div>
      <Card bodyClassName="p-0">
        {rules.isLoading && <Loading />}
        <ErrorBox error={rules.error} className="m-3" />
        {rules.data && !list.length && <Empty>{t('eventRules.none')}</Empty>}
        <ul className="divide-y divide-line">
          {list.map((r, i) => (
            <li
              key={r.id}
              draggable={isAdmin}
              onDragStart={() => setDrag(i)}
              onDragOver={(e) => {
                if (drag === null) return;
                e.preventDefault();
                setOver(i);
              }}
              onDragEnd={() => {
                setDrag(null);
                setOver(null);
              }}
              onDrop={(e) => {
                e.preventDefault();
                if (drag !== null) move(drag, i);
                setDrag(null);
                setOver(null);
              }}
              className={cx(
                'flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-sm',
                !r.enabled && 'opacity-60',
                over === i && drag !== null && drag !== i && 'bg-accent-soft',
                isAdmin && 'cursor-grab',
              )}
              data-rule-id={r.id}
            >
              {isAdmin && (
                <span className="flex flex-col">
                  <button
                    type="button"
                    className="text-ink-subtle hover:text-ink disabled:opacity-30"
                    disabled={i === 0 || patch.isPending}
                    onClick={() => move(i, i - 1)}
                    aria-label={t('eventRules.moveUp')}
                  >
                    <ChevronIcon size={12} className="-rotate-90" />
                  </button>
                  <button
                    type="button"
                    className="text-ink-subtle hover:text-ink disabled:opacity-30"
                    disabled={i === list.length - 1 || patch.isPending}
                    onClick={() => move(i, i + 1)}
                    aria-label={t('eventRules.moveDown')}
                  >
                    <ChevronIcon size={12} className="rotate-90" />
                  </button>
                </span>
              )}
              <span
                className="w-12 text-xs tabular-nums text-ink-subtle"
                title={t('eventRules.priority')}
              >
                {r.priority}
              </span>
              <label className="inline-flex items-center" title={t('common.enabled')}>
                <input
                  type="checkbox"
                  className="h-4 w-4 rounded border-line-strong text-accent-ink"
                  checked={r.enabled}
                  disabled={!isAdmin || patch.isPending}
                  onChange={(e) => patch.mutate([{ id: r.id, enabled: e.target.checked }])}
                  aria-label={t('eventRules.enableNamed', { name: r.name })}
                />
              </label>
              <span className="min-w-0 flex-1">
                <span className="font-medium">{r.name}</span> {r.builtin && <BuiltinBadge />}
                <span className="ms-2 text-xs text-ink-muted">
                  {t(`eventRules.kinds.${r.kind}`)} · {t(`maintenance.scopes.${r.scope}`)}
                  {r.scope_name && `: ${r.scope_name}`}
                </span>
              </span>
              <ActionBadge r={r} />
              <span
                className={cx(
                  'text-xs tabular-nums',
                  r.active_conditions ? 'font-semibold text-status-warning' : 'text-ink-subtle',
                )}
              >
                {t('eventRules.activeConditions', { n: r.active_conditions })}
              </span>
              <span
                className="w-28 text-xs text-ink-muted"
                title={formatDateTime(r.last_matched_at)}
              >
                {r.last_matched_at
                  ? formatRelative(r.last_matched_at)
                  : t('eventRules.neverMatched')}
              </span>
              {isAdmin && (
                <span className="flex">
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={t('common.edit')}
                    onClick={() => setEditing(r)}
                  >
                    <EditIcon size={14} />
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={t('common.delete')}
                    disabled={!!r.builtin}
                    title={r.builtin ? t('eventRules.builtinNoDelete') : undefined}
                    onClick={() => setDeleting(r)}
                  >
                    <TrashIcon size={14} />
                  </Button>
                </span>
              )}
            </li>
          ))}
        </ul>
      </Card>
      {isAdmin && (
        <RuleEditorModal
          open={editing !== null}
          rule={editing && editing !== 'new' ? editing : null}
          onClose={() => setEditing(null)}
        />
      )}
      {isAdmin && <LinkScopeCard />}
      <ConfirmDialog
        open={!!deleting}
        title={t('eventRules.deleteTitle')}
        message={t('common.deleteConfirm', { name: deleting?.name })}
        onConfirm={() => deleting && del.mutate(deleting)}
        onClose={() => {
          setDeleting(null);
          del.reset();
        }}
        busy={del.isPending}
        error={del.error}
      />
    </div>
  );
}
