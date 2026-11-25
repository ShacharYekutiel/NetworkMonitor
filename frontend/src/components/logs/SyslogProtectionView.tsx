import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../../api/endpoints';
import type {
  SyslogCleanupPreview,
  SyslogFilter,
  SyslogProtectionStatus,
  IngestCounters,
  TopTalker,
} from '../../api/types';
import { useTree } from '../../hooks/queries';
import { errorMessage } from '../../lib/errors';
import { formatDateTime, formatNumber, formatRelative } from '../../lib/format';
import { SEVERITIES } from '../../lib/logs';
import {
  emptyFilterForm,
  filterFormErrors,
  filterInput,
  formFromFilter,
  formFromTalker,
  hasErrors,
  type FilterForm,
} from '../../lib/syslogFilters';
import { allDevices } from '../../lib/tree';
import { useIngestStatus } from '../features/IngestStatus';
import { ChevronIcon, EditIcon, PlusIcon, TrashIcon } from '../icons';
import { Banner, ProgressCard } from '../kit';
import { ColumnsMenu, DataTable, labelOf, useDataTable, type DataColumn } from '../kitTable';
import { useToast } from '../toast';
import {
  Button,
  buttonClass,
  Card,
  Checkbox,
  ConfirmDialog,
  cx,
  Empty,
  ErrorBox,
  Field,
  Input,
  Loading,
  Modal,
  Select,
  Switch,
  TAG,
  TAG_TONE,
  Textarea,
} from '../ui';
import { SeverityBadge, SourceCell } from './common';

/** "Info and less important": the rule matches this severity and every less important one. */
function severityLabel(t: (k: string, o?: Record<string, unknown>) => string, s: number) {
  return t('syslogFilters.sevAndLess', { sev: t(`syslogSeverity.${s}`) });
}

/** One line of a rule's conditions ("program SNMP · Info and less important · from 10.0.0.0/8"). */
export function FilterSummary({ f }: { f: SyslogFilter }) {
  const { t } = useTranslation();
  const parts: React.ReactNode[] = [];
  const src = [...f.devices.map((d) => d.name), ...f.sources];
  if (src.length)
    parts.push(
      <span key="src">
        {t('syslogFilters.sum.from')} <bdi dir="auto">{src.join(', ')}</bdi>
      </span>,
    );
  if (f.program)
    parts.push(
      <span key="prog">
        {t(
          f.program_match === 'prefix'
            ? 'syslogFilters.sum.programPrefix'
            : 'syslogFilters.sum.program',
        )}{' '}
        <bdi dir="ltr" className="font-mono">
          {f.program}
        </bdi>
      </span>,
    );
  if (f.msgid)
    parts.push(
      <span key="msgid">
        MSGID{' '}
        <bdi dir="ltr" className="font-mono">
          {f.msgid}
        </bdi>
      </span>,
    );
  if (f.severity_min !== null)
    parts.push(<span key="sev">{severityLabel(t, f.severity_min)}</span>);
  if (f.message_contains)
    parts.push(
      <span key="msg">
        {t('syslogFilters.sum.contains')}{' '}
        <bdi dir="ltr" className="font-mono">
          “{f.message_contains}”
        </bdi>
      </span>,
    );
  return (
    <span className="text-[13px] text-ink-muted">
      {parts.map((p, i) => (
        <span key={i}>
          {i > 0 && ' · '}
          {p}
        </span>
      ))}
    </span>
  );
}

function ActionTag({ f }: { f: SyslogFilter }) {
  const { t } = useTranslation();
  return f.action === 'drop' ? (
    <span className={cx(TAG, TAG_TONE.down)}>{t('syslogFilters.action.drop')}</span>
  ) : (
    <span className={cx(TAG, TAG_TONE.accent)}>
      {t('syslogFilters.action.keepN', { n: f.keep_n })}
    </span>
  );
}

// ---------------------------------------------------------------- protection summary
function ProtectionSummary({
  p,
  c,
  since,
}: {
  p: SyslogProtectionStatus;
  c: IngestCounters;
  since: string;
}) {
  const { t } = useTranslation();
  type Src = SyslogProtectionStatus['top_sources'][number];
  const cols: DataColumn<Src>[] = [
    {
      id: 'source',
      label: t('syslogFilters.col.source'),
      minWidth: 140,
      cell: (s) => (
        <bdi dir="ltr" className="num">
          {s.source_ip}
        </bdi>
      ),
    },
    ...(['filter_rule', 'deduplicated', 'source_rate_limit', 'pruned'] as const).map((k) => ({
      id: k,
      label: t(`syslogFilters.reason.${k}`),
      numeric: true,
      width: 130,
      cell: (s: Src) => formatNumber(s[k], 0),
    })),
  ];
  const layout = useDataTable('syslog-protection-sources', cols);
  const tiles: { k: string; on: boolean; v: string; hint: string }[] = [
    {
      k: t('syslogFilters.dedup'),
      on: p.dedup_enabled,
      v: t('syslogFilters.dedupValue', { n: p.dedup_window_s }),
      hint: t('syslogFilters.dedupShort'),
    },
    {
      k: t('syslogFilters.rate'),
      on: p.rate_limit_enabled,
      v: t('syslogFilters.rateValue', { n: formatNumber(p.rate_limit_per_min, 0) }),
      hint: t('syslogFilters.rateShort'),
    },
  ];
  const counters: [string, number][] = [
    ['filter_rule', c.dropped.filter_rule ?? 0],
    ['deduplicated', c.deduplicated ?? 0],
    ['source_rate_limit', c.dropped.source_rate_limit ?? 0],
    ['pruned', c.pruned ?? 0],
  ];
  return (
    <Card
      title={t('syslogFilters.summaryTitle')}
      actions={
        <Link to="/settings?tab=ingest" className={buttonClass('secondary', 'sm')}>
          {t('syslogFilters.changeInSettings')}
        </Link>
      }
    >
      {/* grid-cols-1 = minmax(0, 1fr): an implicit `auto` track grew to the "by source" table's
          width at narrow widths and the card clipped the tiles and counters (production finding,
          1.5.0, ~590px Hebrew); now the table scrolls inside its own container. */}
      <div className="grid grid-cols-1 gap-4" data-testid="protection-summary">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {tiles.map((x) => (
            <div key={x.k} className="rounded-lg bg-surface-sunken px-4 py-3">
              <div className="flex items-center justify-between gap-2">
                <b className="font-semibold">{x.k}</b>
                <span className={cx(TAG, x.on ? TAG_TONE.up : TAG_TONE.neutral)}>
                  {x.on ? t('syslogFilters.on') : t('syslogFilters.off')}
                </span>
              </div>
              <div className="mt-1 text-[13.5px] text-ink-muted">
                {x.on ? x.v : t('syslogFilters.offHint')} · {x.hint}
              </div>
            </div>
          ))}
        </div>
        <dl className="m-0 grid grid-cols-2 gap-3 sm:grid-cols-4" data-testid="protection-counters">
          {counters.map(([k, n]) => (
            <div key={k} className="rounded-lg border border-line px-3 py-2" data-reason={k}>
              <dt className="text-[12.5px] text-ink-muted">{t(`syslogFilters.reason.${k}`)}</dt>
              <dd className="num m-0 text-xl font-semibold">{formatNumber(n, 0)}</dd>
            </div>
          ))}
        </dl>
        <p className="m-0 text-[12.5px] text-ink-muted">
          {t('ingest.countersSince', { time: formatDateTime(since) })}
        </p>
        {p.top_sources.length > 0 && (
          <div>
            <div className="mb-1 flex items-center justify-between">
              <h3 className="m-0 text-[15px] font-semibold">{t('syslogFilters.bySource')}</h3>
              <ColumnsMenu layout={layout} labelOf={(id) => labelOf(cols, id)} />
            </div>
            <DataTable
              layout={layout}
              columns={layout.columns}
              rows={p.top_sources}
              rowKey={(s) => s.source_ip}
              testId="protection-sources"
            />
          </div>
        )}
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------- rule editor
function FilterRuleModal({
  open,
  rule,
  initial,
  onClose,
  onSaved,
}: {
  open: boolean;
  rule: SyslogFilter | null;
  initial: FilterForm | null;
  onClose: () => void;
  onSaved: (f: SyslogFilter, created: boolean, matchChanged: boolean) => void;
}) {
  const { t } = useTranslation();
  const tree = useTree();
  const [f, setF] = useState<FilterForm>(emptyFilterForm);
  const [touched, setTouched] = useState(false);
  useEffect(() => {
    if (!open) return;
    setF(rule ? formFromFilter(rule) : (initial ?? emptyFilterForm()));
    setTouched(false);
  }, [open, rule, initial]);
  const devices = useMemo(
    () =>
      tree.data
        ? allDevices(tree.data.groups)
            .map((x) => x.device)
            .sort((a, b) => a.name.localeCompare(b.name))
        : [],
    [tree.data],
  );
  const errs = filterFormErrors(f);
  const set = (p: Partial<FilterForm>) => setF((x) => ({ ...x, ...p }));
  const save = useMutation({
    mutationFn: () =>
      rule
        ? endpoints.updateSyslogFilter(rule.id, filterInput(f))
        : endpoints.createSyslogFilter(filterInput(f)),
    onSuccess: (saved) => {
      const before = rule
        ? JSON.stringify({ ...filterInput(formFromFilter(rule)), name: '', enabled: true })
        : '';
      const after = JSON.stringify({ ...filterInput(f), name: '', enabled: true });
      onSaved(saved, !rule, !rule || before !== after);
    },
  });
  const bad = (on: boolean | undefined, msg: string) =>
    on && touched ? <span className="text-status-down">{msg}</span> : undefined;
  return (
    <Modal
      open={open}
      wide
      title={rule ? t('syslogFilters.editTitle') : t('syslogFilters.addTitle')}
      onClose={onClose}
      testId="syslog-filter-editor"
      footer={
        <>
          <Button
            variant="primary"
            busy={save.isPending}
            onClick={() => {
              setTouched(true);
              if (!hasErrors(errs)) save.mutate();
            }}
          >
            {t('common.save')}
          </Button>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
        </>
      }
    >
      <div className="grid gap-4">
        <p className="m-0 text-[13.5px] text-ink-muted">{t('syslogFilters.editorIntro')}</p>
        <Field label={t('syslogFilters.name')} hint={bad(errs.name, t('syslogFilters.nameErr'))}>
          <Input
            value={f.name}
            maxLength={100}
            onChange={(e) => set({ name: e.target.value })}
            data-testid="filter-name"
          />
        </Field>
        <fieldset className="grid gap-3 rounded-lg border border-line px-4 py-3.5">
          <legend className="px-1 text-xs font-semibold text-ink-muted">
            {t('syslogFilters.conditions')}
          </legend>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label={t('syslogFilters.device')} hint={t('syslogFilters.sourceHint')}>
              <Select
                value={f.deviceId === null ? '' : String(f.deviceId)}
                onChange={(e) =>
                  set({ deviceId: e.target.value === '' ? null : Number(e.target.value) })
                }
                data-testid="filter-device"
              >
                <option value="">{t('syslogFilters.anyDevice')}</option>
                {devices.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}
                  </option>
                ))}
              </Select>
            </Field>
            <Field
              label={t('syslogFilters.sources')}
              hint={
                bad(
                  !!errs.sources?.length,
                  t('syslogFilters.sourcesErr', { list: (errs.sources ?? []).join(', ') }),
                ) ?? t('syslogFilters.sourcesHint')
              }
            >
              <Textarea
                rows={2}
                value={f.sources}
                onChange={(e) => set({ sources: e.target.value })}
                dir="ltr"
                className="font-mono"
                data-testid="filter-sources"
              />
            </Field>
            <Field label={t('syslogFilters.program')} hint={t('syslogFilters.programHint')}>
              <div className="flex gap-2">
                <Input
                  value={f.program}
                  maxLength={128}
                  onChange={(e) => set({ program: e.target.value })}
                  dir="ltr"
                  className="flex-1 font-mono"
                  data-testid="filter-program"
                />
                <Select
                  value={f.programMatch}
                  onChange={(e) => set({ programMatch: e.target.value as 'exact' | 'prefix' })}
                  aria-label={t('syslogFilters.programMatch')}
                  className="w-36"
                >
                  <option value="exact">{t('syslogFilters.match.exact')}</option>
                  <option value="prefix">{t('syslogFilters.match.prefix')}</option>
                </Select>
              </div>
            </Field>
            <Field label={t('syslogFilters.msgid')} hint={t('syslogFilters.msgidHint')}>
              <Input
                value={f.msgid}
                maxLength={64}
                onChange={(e) => set({ msgid: e.target.value })}
                dir="ltr"
                className="font-mono"
              />
            </Field>
            <Field label={t('syslogFilters.severity')} hint={t('syslogFilters.severityHint')}>
              <Select
                value={f.severityMin === null ? '' : String(f.severityMin)}
                onChange={(e) =>
                  set({ severityMin: e.target.value === '' ? null : Number(e.target.value) })
                }
                data-testid="filter-severity"
              >
                <option value="">{t('syslogFilters.anySeverity')}</option>
                {SEVERITIES.map((s) => (
                  <option key={s} value={s}>
                    {severityLabel(t, s)}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label={t('syslogFilters.contains')} hint={t('syslogFilters.containsHint')}>
              <Input
                value={f.contains}
                maxLength={200}
                onChange={(e) => set({ contains: e.target.value })}
                dir="ltr"
                className="font-mono"
                data-testid="filter-contains"
              />
            </Field>
          </div>
          {touched && errs.noCondition && (
            <p className="m-0 text-[13px] text-status-down">{t('syslogFilters.noCondition')}</p>
          )}
        </fieldset>
        <fieldset className="grid gap-2">
          <legend className="mb-1 text-xs font-semibold text-ink-muted">
            {t('syslogFilters.actionLabel')}
          </legend>
          {(['drop', 'keep'] as const).map((a) => (
            <label key={a} className="flex cursor-pointer items-start gap-2 text-sm">
              <input
                type="radio"
                name="syslog-filter-action"
                value={a}
                checked={f.action === a}
                onChange={() => set({ action: a })}
                className="mt-0.5 h-4 w-4 border-line-strong text-accent-ink"
              />
              <span className="grid gap-1">
                <span className="font-medium">{t(`syslogFilters.action.${a}Long`)}</span>
                <span className="text-xs text-ink-muted">{t(`syslogFilters.action.${a}Hint`)}</span>
                {a === 'keep' && f.action === 'keep' && (
                  <span className="flex items-center gap-2">
                    <Input
                      type="number"
                      min={1}
                      max={100000}
                      value={f.keepN}
                      onChange={(e) => set({ keepN: e.target.value })}
                      dir="ltr"
                      className="w-28"
                      aria-label={t('syslogFilters.keepN')}
                      data-testid="filter-keep-n"
                    />
                    <span className="text-xs text-ink-muted">{t('syslogFilters.keepNHint')}</span>
                    {bad(errs.keepN, t('syslogFilters.keepNErr'))}
                  </span>
                )}
              </span>
            </label>
          ))}
        </fieldset>
        <Checkbox
          label={t('syslogFilters.enabled')}
          checked={f.enabled}
          onChange={(v) => set({ enabled: v })}
        />
        <ErrorBox error={save.error} />
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- cleanup offer
function CleanupDialog({
  rule,
  preview,
  onClose,
  onStarted,
}: {
  rule: SyslogFilter | null;
  preview: SyslogCleanupPreview | null;
  onClose: () => void;
  onStarted: () => void;
}) {
  const { t } = useTranslation();
  const run = useMutation({
    mutationFn: () => endpoints.syslogCleanup(rule!.id, preview!.until, preview!.count),
    onSuccess: onStarted,
  });
  const n = preview?.count ?? 0;
  return (
    <Modal
      open={!!rule && !!preview}
      title={t('syslogFilters.cleanup.title')}
      onClose={onClose}
      testId="syslog-cleanup"
      footer={
        <>
          <Button
            variant="danger"
            busy={run.isPending}
            onClick={() => run.mutate()}
            data-testid="cleanup-confirm"
          >
            {t('syslogFilters.cleanup.confirm')}
          </Button>
          <Button onClick={onClose}>{t('syslogFilters.cleanup.keep')}</Button>
        </>
      }
    >
      {rule && preview && (
        <div className="grid gap-3 text-[14.5px]">
          <div className="rounded-lg bg-surface-sunken px-4 py-3">
            <div className="text-[13px] text-ink-muted">
              {preview.action === 'keep'
                ? t('syslogFilters.cleanup.matchKeep', { name: rule.name, keep: preview.keep_n })
                : t('syslogFilters.cleanup.matchDrop', { name: rule.name })}
            </div>
            <div className="num text-[28px] font-semibold" data-testid="cleanup-count">
              {formatNumber(n, 0)}
            </div>
          </div>
          <p className="m-0">{t('syslogFilters.cleanup.question')}</p>
          <p className="m-0 text-[13px] text-ink-muted">
            {t('syslogFilters.cleanup.note', { time: formatDateTime(preview.until) })}
          </p>
          <ErrorBox error={run.error} />
        </div>
      )}
    </Modal>
  );
}

// ---------------------------------------------------------------- top talkers
function TopTalkersCard({ onCreate }: { onCreate: (x: TopTalker) => void }) {
  const { t } = useTranslation();
  const q = useQuery({
    queryKey: qk.topTalkers(24),
    queryFn: () => endpoints.topTalkers(24),
    refetchInterval: 60_000,
  });
  const total = q.data?.total_rows ?? 0;
  const cols: DataColumn<TopTalker>[] = [
    {
      id: 'source',
      label: t('syslogFilters.col.source'),
      minWidth: 140,
      cell: (x) => <SourceCell r={x} />,
    },
    {
      id: 'program',
      label: t('logs.col.program'),
      minWidth: 100,
      cell: (x) => (
        <bdi dir="ltr" className="font-mono">
          {x.program || '—'}
        </bdi>
      ),
    },
    {
      id: 'severity',
      label: t('logs.col.severity'),
      width: 120,
      cell: (x) => <SeverityBadge severity={x.severity} />,
    },
    {
      id: 'rows',
      label: t('syslogFilters.col.rows'),
      numeric: true,
      width: 110,
      cell: (x) => formatNumber(x.rows, 0),
    },
    {
      id: 'share',
      label: t('syslogFilters.col.share'),
      numeric: true,
      width: 100,
      cell: (x) => `${formatNumber(total ? (x.rows / total) * 100 : 0, 1)}%`,
    },
    {
      id: 'last',
      label: t('syslogFilters.col.lastSeen'),
      width: 130,
      title: (x) => formatDateTime(x.last_seen),
      cell: (x) => (x.last_seen ? formatRelative(x.last_seen) : '—'),
    },
    {
      id: 'actions',
      label: t('tableCols.actions'),
      pin: 'end',
      fixed: true,
      width: 150,
      cell: (x) => (
        <Button size="sm" onClick={() => onCreate(x)} data-testid="talker-create-rule">
          {t('syslogFilters.createFromTalker')}
        </Button>
      ),
    },
  ];
  const layout = useDataTable('syslog-top-talkers', cols);
  return (
    <Card
      title={t('syslogFilters.talkersTitle')}
      actions={<ColumnsMenu layout={layout} labelOf={(id) => labelOf(cols, id)} />}
      bodyClassName="p-0"
    >
      <p className="m-0 px-[22px] pb-3 text-[13.5px] text-ink-muted">
        {q.data
          ? t('syslogFilters.talkersIntro', {
              rows: formatNumber(q.data.total_rows, 0),
              messages: formatNumber(q.data.total_messages, 0),
            })
          : t('syslogFilters.talkersHint')}
      </p>
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} className="m-3" />
      {q.data && !q.data.items.length && <Empty>{t('syslogFilters.talkersNone')}</Empty>}
      {q.data && q.data.items.length > 0 && (
        <DataTable
          layout={layout}
          columns={layout.columns}
          rows={q.data.items}
          rowKey={(x) => `${x.source_ip}|${x.device_id}|${x.program}|${x.severity}`}
          testId="top-talkers"
        />
      )}
    </Card>
  );
}

// ---------------------------------------------------------------- the view
/** [syslog-protection] Logs → Flood protection (admins): dedup / rate-limit state and counters,
 * the ordered filter rules with their hit counters, top talkers and the cleanup offer. */
export function SyslogProtectionView() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const status = useIngestStatus(10_000);
  const [polling, setPolling] = useState(false);
  const filters = useQuery({
    queryKey: qk.syslogFilters,
    queryFn: endpoints.syslogFilters,
    refetchInterval: polling ? 1500 : 15_000,
  });
  const job = filters.data?.cleanup ?? null;
  useEffect(() => {
    const running = job?.state === 'running';
    if (polling && !running && job) {
      if (job.state === 'done')
        toast.push('success', t('syslogFilters.cleanup.done', { n: formatNumber(job.deleted, 0) }));
      else toast.push('error', t('syslogFilters.cleanup.failed', { error: job.error ?? '' }));
      void qc.invalidateQueries({ queryKey: ['syslog'] });
      void qc.invalidateQueries({ queryKey: ['syslog-top-talkers'] });
    }
    setPolling(running);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.state, job?.deleted]);
  const [editing, setEditing] = useState<SyslogFilter | 'new' | null>(null);
  const [draft, setDraft] = useState<FilterForm | null>(null);
  const [deleting, setDeleting] = useState<SyslogFilter | null>(null);
  const [offer, setOffer] = useState<{ rule: SyslogFilter; preview: SyslogCleanupPreview } | null>(
    null,
  );
  const items = filters.data?.items ?? [];
  const invalidate = () => void qc.invalidateQueries({ queryKey: qk.syslogFilters });
  const patch = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      endpoints.updateSyslogFilter(id, { enabled }),
    onSettled: invalidate,
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  const order = useMutation({
    mutationFn: (ids: number[]) => endpoints.orderSyslogFilters(ids),
    onSettled: invalidate,
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  const del = useMutation({
    mutationFn: (f: SyslogFilter) => endpoints.deleteSyslogFilter(f.id),
    onSuccess: () => {
      invalidate();
      setDeleting(null);
    },
  });
  const preview = useMutation({
    mutationFn: (f: SyslogFilter) => endpoints.syslogCleanupPreview(f.id),
    onSuccess: (p, f) => {
      if (p.count > 0) setOffer({ rule: f, preview: p });
      else toast.push('success', t('syslogFilters.cleanup.nothing'));
    },
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  const move = (i: number, d: number) => {
    const ids = items.map((x) => x.id);
    const j = i + d;
    if (j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    order.mutate(ids);
  };
  const cols: DataColumn<SyslogFilter>[] = [
    {
      id: 'order',
      label: t('syslogFilters.col.order'),
      fixed: true,
      width: 76,
      cell: (f) => {
        const i = items.indexOf(f);
        return (
          <span className="inline-flex items-center gap-1">
            <span className="num w-5 text-ink-subtle">{i + 1}</span>
            <span className="flex flex-col">
              <button
                type="button"
                className="text-ink-subtle hover:text-ink disabled:opacity-30"
                disabled={i === 0 || order.isPending}
                onClick={() => move(i, -1)}
                aria-label={t('syslogFilters.moveUp', { name: f.name })}
              >
                <ChevronIcon size={12} className="-rotate-90" />
              </button>
              <button
                type="button"
                className="text-ink-subtle hover:text-ink disabled:opacity-30"
                disabled={i === items.length - 1 || order.isPending}
                onClick={() => move(i, 1)}
                aria-label={t('syslogFilters.moveDown', { name: f.name })}
              >
                <ChevronIcon size={12} className="rotate-90" />
              </button>
            </span>
          </span>
        );
      },
    },
    {
      id: 'rule',
      label: t('syslogFilters.col.rule'),
      minWidth: 260,
      wrap: true,
      cell: (f) => (
        <span className={cx('grid gap-0.5', !f.enabled && 'opacity-60')}>
          <span className="font-medium">
            <bdi dir="auto">{f.name}</bdi>
          </span>
          <FilterSummary f={f} />
        </span>
      ),
    },
    {
      id: 'action',
      label: t('syslogFilters.col.action'),
      width: 150,
      cell: (f) => <ActionTag f={f} />,
    },
    {
      id: 'hits',
      label: t('syslogFilters.col.hits'),
      numeric: true,
      width: 110,
      title: (f) => t('syslogFilters.hitsSince', { time: formatDateTime(f.hits_since) }),
      cell: (f) => <span data-testid="filter-hits">{formatNumber(f.hits, 0)}</span>,
    },
    {
      id: 'last',
      label: t('syslogFilters.col.lastHit'),
      width: 130,
      title: (f) => formatDateTime(f.last_hit_at),
      cell: (f) => (f.last_hit_at ? formatRelative(f.last_hit_at) : t('syslogFilters.never')),
    },
    {
      id: 'enabled',
      label: t('common.enabled'),
      fixed: true,
      width: 90,
      cell: (f) => (
        <Switch
          checked={f.enabled}
          onChange={(v) => patch.mutate({ id: f.id, enabled: v })}
          label={t('syslogFilters.enableNamed', { name: f.name })}
          disabled={patch.isPending}
        />
      ),
    },
    {
      id: 'actions',
      label: t('tableCols.actions'),
      pin: 'end',
      fixed: true,
      width: 190,
      cell: (f) => (
        <span className="inline-flex gap-1">
          <Button
            size="sm"
            onClick={() => preview.mutate(f)}
            busy={preview.isPending && preview.variables?.id === f.id}
            disabled={job?.state === 'running'}
            data-testid="filter-cleanup"
          >
            {t('syslogFilters.cleanupBtn')}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            aria-label={t('common.edit')}
            onClick={() => setEditing(f)}
          >
            <EditIcon size={14} />
          </Button>
          <Button
            size="sm"
            variant="ghost"
            aria-label={t('common.delete')}
            onClick={() => setDeleting(f)}
          >
            <TrashIcon size={14} />
          </Button>
        </span>
      ),
    },
  ];
  const layout = useDataTable('syslog-filters', cols);
  const st = status.data;
  return (
    <div className="space-y-4" data-testid="syslog-protection-view">
      {st && (
        <ProtectionSummary
          p={st.syslog_protection}
          c={st.counters.syslog}
          since={st.counters_since}
        />
      )}
      <Card
        title={t('syslogFilters.title')}
        actions={
          <>
            <ColumnsMenu layout={layout} labelOf={(id) => labelOf(cols, id)} />
            <Button
              variant="primary"
              size="sm"
              onClick={() => {
                setDraft(null);
                setEditing('new');
              }}
              data-testid="filter-add"
            >
              <PlusIcon size={13} /> {t('syslogFilters.add')}
            </Button>
          </>
        }
        bodyClassName="p-0"
      >
        <p className="m-0 px-[22px] pb-3 text-[13.5px] text-ink-muted">
          {t('syslogFilters.intro')}
        </p>
        {job?.state === 'failed' && (
          <Banner
            tone="down"
            className="mx-[22px] mb-3"
            title={t('syslogFilters.cleanup.failedTitle')}
          >
            {job.error}
          </Banner>
        )}
        {filters.isLoading && <Loading />}
        <ErrorBox error={filters.error} className="m-3" />
        {filters.data && !items.length && <Empty>{t('syslogFilters.none')}</Empty>}
        {items.length > 0 && (
          <DataTable
            layout={layout}
            columns={layout.columns}
            rows={items}
            rowKey={(f) => f.id}
            rowProps={(f) => ({ 'data-filter-id': f.id })}
            testId="syslog-filters"
          />
        )}
      </Card>
      <TopTalkersCard
        onCreate={(x) => {
          const who = x.device_name ?? x.source_ip;
          setDraft(
            formFromTalker(
              x,
              t('syslogFilters.talkerName', {
                program: x.program || '—',
                sev: t(`syslogSeverity.${x.severity}`),
                source: who,
              }).slice(0, 100),
            ),
          );
          setEditing('new');
        }}
      />
      <FilterRuleModal
        open={editing !== null}
        rule={editing && editing !== 'new' ? editing : null}
        initial={draft}
        onClose={() => setEditing(null)}
        onSaved={(saved, _created, matchChanged) => {
          setEditing(null);
          setDraft(null);
          invalidate();
          toast.push('success', t('common.saved'));
          // offered after a create or a change of what the rule matches; never automatic
          if (matchChanged && job?.state !== 'running') preview.mutate(saved);
        }}
      />
      <CleanupDialog
        rule={offer?.rule ?? null}
        preview={offer?.preview ?? null}
        onClose={() => setOffer(null)}
        onStarted={() => {
          setOffer(null);
          setPolling(true);
          invalidate();
        }}
      />
      {job?.state === 'running' && (
        <ProgressCard
          title={t('syslogFilters.cleanup.running', { name: job.rule_name })}
          done={job.deleted}
          total={Math.max(job.expected, job.deleted)}
        />
      )}
      <ConfirmDialog
        open={!!deleting}
        title={t('syslogFilters.deleteTitle')}
        message={t('syslogFilters.deleteBody', { name: deleting?.name })}
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
