import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type {
  DeviceClass,
  DeviceTemplate,
  DiscoveryResult,
  DiscoveryScan,
  ImportItem,
} from '../api/types';
import { DEVICE_CLASSES } from '../api/types';
import {
  DeviceClassBadge,
  DeviceClassIcon,
  ReachabilityChips,
  ScanStatusBadge,
  VendorBadge,
} from '../components/discovery/Badges';
import { GroupSelect } from '../components/EntityForms';
import {
  CheckIcon,
  ChevronIcon,
  SearchIcon,
  StopIcon,
  TrashIcon,
  XIcon,
} from '../components/icons';
import {
  ConfidenceChip,
  FingerprintView,
  fingerprintSummary,
  hasFingerprint,
} from '../components/discovery/Fingerprint';
import { SkippedList, TemplateSelect, useDeviceTemplates } from '../components/Templates';
import { useToast } from '../components/toast';
import {
  Button,
  CHIP,
  CHIP_TONE,
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
  Ltr,
  Modal,
  Notice,
  PageHeader,
  td,
  th,
  TruncLtr,
  Value,
} from '../components/ui';
import { useLiveState } from '../hooks/live';
import {
  filterResults,
  isExisting,
  NO_FILTERS,
  progressPct,
  resultDisplayName,
  withBuffered,
  type ResultFilters,
} from '../lib/discovery';
import { errorMessage } from '../lib/errors';
import { formatDateTime, formatNumber, formatRelative } from '../lib/format';
import { importRows, retryIds, templateName, type ImportRow } from '../lib/templates';

const POLL_MS = 2000;

function Stat({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[13px] font-medium text-ink-muted">{label}</div>
      <div
        className={cx(
          'mt-1 font-display text-[30px] font-semibold leading-8 tabular-nums',
          tone ?? 'text-ink',
        )}
      >
        {formatNumber(value, 0)}
      </div>
    </div>
  );
}

function ProgressCard({ scan, polling }: { scan: DiscoveryScan; polling: boolean }) {
  const { t } = useTranslation();
  const pct = progressPct(scan);
  const running = scan.status === 'running';
  return (
    <Card>
      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
          <span className="font-medium">
            {t('discovery.scanned', {
              scanned: formatNumber(scan.scanned_hosts, 0),
              total: formatNumber(scan.total_hosts, 0),
            })}
          </span>
          <span className="text-xs text-ink-muted">
            {running && polling && <span className="me-2">{t('discovery.polling')}</span>}
            <Value text={`${pct}%`} />
          </span>
        </div>
        <div
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={pct}
          aria-label={t('discovery.progress')}
          className="h-1.5 overflow-hidden rounded-sm bg-surface-sunken"
        >
          <div
            className={cx(
              'h-full rounded-sm transition-[width] duration-500',
              scan.status === 'failed'
                ? 'bg-danger'
                : scan.status === 'cancelled'
                  ? 'bg-status-paused'
                  : running
                    ? 'bg-accent'
                    : 'bg-status-up',
            )}
            style={{ width: `${scan.status === 'completed' ? 100 : pct}%` }}
          />
        </div>
        <div className="grid grid-cols-3 gap-3 sm:grid-cols-5">
          <Stat label={t('discovery.found')} value={scan.found_hosts} />
          <Stat
            label={t('discovery.new')}
            value={scan.new_hosts}
            tone={scan.new_hosts ? 'text-accent-ink' : undefined}
          />
          <Stat label={t('discovery.total')} value={scan.total_hosts} />
          <div className="col-span-3 min-w-0 text-xs text-ink-muted sm:col-span-2">
            <div title={formatDateTime(scan.started_at)}>
              {t('discovery.started')}: {formatRelative(scan.started_at)}
            </div>
            {scan.finished_at && (
              <div title={formatDateTime(scan.finished_at)}>
                {t('discovery.finished')}: {formatRelative(scan.finished_at)}
              </div>
            )}
          </div>
        </div>
        {scan.status === 'failed' && scan.error && (
          <ErrorBox error={new Error(`${t('discovery.status.failed')}: ${scan.error}`)} />
        )}
      </div>
    </Card>
  );
}

function FilterBar({
  f,
  onChange,
  counts,
}: {
  f: ResultFilters;
  onChange: (f: ResultFilters) => void;
  counts: Partial<Record<DeviceClass, number>>;
}) {
  const { t } = useTranslation();
  const toggleClass = (c: DeviceClass) =>
    onChange({
      ...f,
      classes: f.classes.includes(c) ? f.classes.filter((x) => x !== c) : [...f.classes, c],
    });
  const present = DEVICE_CLASSES.filter((c) => counts[c]);
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="relative w-full sm:w-64">
          <SearchIcon
            size={14}
            className="pointer-events-none absolute start-2.5 top-1/2 -translate-y-1/2 text-ink-subtle"
          />
          <Input
            className="ps-8"
            value={f.q}
            onChange={(e) => onChange({ ...f, q: e.target.value })}
            placeholder={t('discovery.searchResults')}
            aria-label={t('discovery.searchResults')}
          />
        </div>
        <Checkbox
          label={t('discovery.onlyNew')}
          checked={f.onlyNew}
          onChange={(v) => onChange({ ...f, onlyNew: v })}
        />
        <Checkbox
          label={t('discovery.hideExisting')}
          checked={f.hideExisting}
          onChange={(v) => onChange({ ...f, hideExisting: v })}
        />
      </div>
      {present.length > 0 && (
        <div className="flex flex-wrap gap-1.5" role="group" aria-label={t('discovery.class')}>
          {present.map((c) => {
            const on = f.classes.includes(c);
            return (
              <button
                key={c}
                type="button"
                aria-pressed={on}
                onClick={() => toggleClass(c)}
                className={cx(
                  'inline-flex h-[26px] items-center gap-1.5 rounded-sm border px-2 text-xs font-medium',
                  on
                    ? 'border-accent-ink bg-accent-soft text-accent-ink'
                    : 'border-line-strong bg-surface text-ink-muted hover:text-ink',
                )}
              >
                <DeviceClassIcon cls={c} size={12} />
                {t(`deviceClass.${c}`)}
                <span className="font-mono text-[11px] tabular-nums">{counts[c]}</span>
              </button>
            );
          })}
          {f.classes.length > 0 && (
            <button
              type="button"
              className="text-xs text-accent-ink hover:underline"
              onClick={() => onChange({ ...f, classes: [] })}
            >
              {t('common.clearFilters')}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

type RowEdit = { template?: string; name?: string };

function StateCell({ r }: { r: DiscoveryResult }) {
  const { t } = useTranslation();
  const deviceId = r.imported_device_id ?? r.existing_device_id;
  if (deviceId !== null)
    return (
      <Link
        to={`/devices/${deviceId}`}
        className={cx(CHIP, CHIP_TONE.neutral, 'text-ink hover:underline')}
      >
        {r.imported_device_id !== null ? t('discovery.imported') : t('discovery.exists')}
      </Link>
    );
  if (r.is_new)
    return (
      <span className={cx(CHIP, CHIP_TONE.accent, 'font-semibold')}>{t('discovery.newBadge')}</span>
    );
  return (
    <span className="whitespace-nowrap text-xs text-ink-muted">{t('discovery.seenBefore')}</span>
  );
}

function ResultRow({
  r,
  templates,
  selected,
  onSelect,
  edit,
  onEdit,
  disabled,
}: {
  r: DiscoveryResult;
  templates: DeviceTemplate[];
  selected: boolean;
  onSelect: (v: boolean) => void;
  edit: RowEdit;
  onEdit: (e: RowEdit) => void;
  disabled: boolean;
}) {
  const { t } = useTranslation();
  const existing = isExisting(r);
  const [fpOpen, setFpOpen] = useState(false);
  const fp = hasFingerprint(r.fingerprint) ? r.fingerprint : null;
  const summary = fingerprintSummary(r.fingerprint);
  return (
    <>
      <tr
        className={cx(
          'hover:bg-surface-hover',
          selected && 'bg-accent-soft',
          existing && 'text-ink-muted',
          fpOpen && '[&>td]:border-b-0',
        )}
      >
        <td className={td}>
          <input
            type="checkbox"
            className="h-4 w-4 disabled:opacity-40"
            aria-label={t('discovery.selectHost', { ip: r.ip })}
            checked={selected}
            disabled={existing || disabled}
            onChange={(e) => onSelect(e.target.checked)}
          />
        </td>
        <td className={`${td} whitespace-nowrap font-mono text-xs`}>
          <Ltr>{r.ip}</Ltr>
          {r.mac && (
            <div className="text-[11px] text-ink-muted">
              <Ltr>{r.mac}</Ltr>
            </div>
          )}
          {fp && (
            <button
              type="button"
              onClick={() => setFpOpen((o) => !o)}
              aria-expanded={fpOpen}
              className="mt-0.5 inline-flex items-center gap-0.5 font-sans text-[11px] text-accent-ink hover:underline"
            >
              <ChevronIcon size={10} className={fpOpen ? 'rotate-90' : 'rtl:rotate-180'} />
              {t('fingerprint.heading')}
            </button>
          )}
        </td>
        <td className={`${td} w-[15rem] min-w-[9rem] max-w-[15rem]`}>
          {existing ? (
            <bdi dir="auto" className="font-medium">
              {resultDisplayName(r)}
            </bdi>
          ) : (
            <Input
              className="py-1 text-xs"
              value={edit.name ?? ''}
              placeholder={resultDisplayName(r)}
              onChange={(e) => onEdit({ ...edit, name: e.target.value })}
              maxLength={128}
              dir="auto"
              aria-label={t('discovery.deviceName', { ip: r.ip })}
            />
          )}
          {r.sys_descr ? (
            <TruncLtr
              className="mt-0.5 max-w-[15rem] text-[11px] text-ink-muted"
              title={r.sys_descr}
            >
              {r.sys_descr}
            </TruncLtr>
          ) : (
            summary && (
              <div
                dir="auto"
                className="mt-0.5 max-w-[15rem] truncate text-[11px] text-ink-muted"
                title={summary}
              >
                {summary}
              </div>
            )
          )}
        </td>
        <td className={td}>
          <div className="flex flex-col items-start gap-1">
            <DeviceClassBadge cls={r.device_class} />
            {r.vendor && <VendorBadge vendor={r.vendor} />}
            {r.model && (
              <span className="font-mono text-[11px] text-ink-muted">
                <Ltr>{r.model}</Ltr>
              </span>
            )}
            <ConfidenceChip confidence={r.confidence} by={r.classified_by} />
          </div>
        </td>
        <td className={`${td} min-w-[10rem]`}>
          <ReachabilityChips r={r} />
        </td>
        <td className={td}>
          <StateCell r={r} />
        </td>
        <td className={`${td} min-w-[12rem]`}>
          {existing ? (
            <span className="text-xs">—</span>
          ) : (
            <TemplateSelect
              className="py-1 text-xs"
              templates={templates}
              suggested={r.suggested_template}
              value={edit.template ?? r.suggested_template}
              onChange={(v) => onEdit({ ...edit, template: v })}
              ariaLabel={t('discovery.templateFor', { ip: r.ip })}
            />
          )}
        </td>
      </tr>
      {fp && fpOpen && (
        <tr className={cx(selected && 'bg-accent-soft')}>
          <td className={td} />
          <td className={td} colSpan={6}>
            <FingerprintView fp={fp} />
          </td>
        </tr>
      )}
    </>
  );
}

/** Mobile layout: one compact card per host (no horizontal scrolling). */
function ResultCard({
  r,
  templates,
  selected,
  onSelect,
  edit,
  onEdit,
  disabled,
}: Parameters<typeof ResultRow>[0]) {
  const { t } = useTranslation();
  const existing = isExisting(r);
  return (
    <li className={cx('space-y-2 px-3 py-2.5', selected && 'bg-accent-soft')}>
      <div className="flex items-start gap-2">
        <input
          type="checkbox"
          className="mt-1 h-4 w-4 shrink-0 disabled:opacity-40"
          aria-label={t('discovery.selectHost', { ip: r.ip })}
          checked={selected}
          disabled={existing || disabled}
          onChange={(e) => onSelect(e.target.checked)}
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-mono text-sm font-medium">
              <Ltr>{r.ip}</Ltr>
            </span>
            <StateCell r={r} />
          </div>
          <div dir="auto" className="truncate text-left text-sm rtl:text-right">
            {resultDisplayName(r)}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-1">
            <DeviceClassBadge cls={r.device_class} />
            {r.vendor && <VendorBadge vendor={r.vendor} />}
            {r.model && (
              <span className="font-mono text-[11px] text-ink-muted">
                <Ltr>{r.model}</Ltr>
              </span>
            )}
            <ConfidenceChip confidence={r.confidence} by={r.classified_by} />
            <ReachabilityChips r={r} />
          </div>
          {hasFingerprint(r.fingerprint) && (
            <details className="mt-1.5 min-w-0">
              <summary className="cursor-pointer text-xs text-accent-ink">
                {t('fingerprint.heading')}
                {fingerprintSummary(r.fingerprint) && (
                  <span dir="auto" className="ms-1 text-ink-muted">
                    · {fingerprintSummary(r.fingerprint)}
                  </span>
                )}
              </summary>
              <div className="mt-1">
                <FingerprintView fp={r.fingerprint} />
              </div>
            </details>
          )}
        </div>
      </div>
      {!existing && (
        <div className="grid grid-cols-1 gap-2 ps-6">
          <TemplateSelect
            className="py-1 text-xs"
            templates={templates}
            suggested={r.suggested_template}
            value={edit.template ?? r.suggested_template}
            onChange={(v) => onEdit({ ...edit, template: v })}
            ariaLabel={t('discovery.templateFor', { ip: r.ip })}
          />
          <Input
            className="py-1 text-xs"
            value={edit.name ?? ''}
            placeholder={resultDisplayName(r)}
            onChange={(e) => onEdit({ ...edit, name: e.target.value })}
            maxLength={128}
            dir="auto"
            aria-label={t('discovery.deviceName', { ip: r.ip })}
          />
        </div>
      )}
    </li>
  );
}

function ImportSummary({
  rows,
  templates,
}: {
  rows: ImportRow[];
  templates: DeviceTemplate[] | undefined;
}) {
  const { t, i18n } = useTranslation();
  const created = rows.filter((r) => r.kind === 'created').length;
  return (
    <div className="space-y-3 text-sm" data-testid="import-summary">
      <Notice tone={created ? 'success' : 'info'}>
        {t('discovery.importSummary', { created, failed: rows.length - created })}
      </Notice>
      {retryIds(rows).length > 0 && (
        <p className="text-sm text-status-warning">{t('discovery.retrySelected')}</p>
      )}
      <ul className="divide-y divide-line">
        {rows.map((row) => (
          <li key={row.result_id} className="flex gap-2 py-2">
            {row.kind === 'created' ? (
              <CheckIcon size={16} className="mt-0.5 shrink-0 text-status-up" />
            ) : (
              <XIcon size={16} className="mt-0.5 shrink-0 text-status-down" />
            )}
            <div className="min-w-0 flex-1 space-y-1">
              <div className="flex flex-wrap items-baseline gap-x-2">
                <span className="font-mono text-xs text-ink-muted">
                  <Ltr>{row.ip}</Ltr>
                </span>
                {row.kind === 'created' && (
                  <>
                    <Link
                      to={`/devices/${row.device_id}`}
                      className="font-medium text-accent-ink hover:underline"
                    >
                      <bdi dir="auto">{row.device_name}</bdi>
                    </Link>
                    <span className="text-xs text-ink-muted">
                      {templateName(templates, row.template_id, i18n.language)} ·{' '}
                      {t('discovery.sensorsCreated', { count: row.sensors_created })}
                    </span>
                  </>
                )}
                {row.kind === 'error' && row.retry && (
                  <span className="text-status-warning">
                    {t('discovery.notImportedRetry')}
                    {' — '}
                    {row.timedOut ? t('discovery.importBudget') : t('errors.internal_error')}
                  </span>
                )}
                {row.kind === 'error' && !row.retry && (
                  <span className="text-status-down">
                    {t(`errors.${row.code}`, { defaultValue: row.message || row.code })}
                    {row.device_id !== null && (
                      <>
                        {' '}
                        <Link
                          to={`/devices/${row.device_id}`}
                          className="text-accent-ink hover:underline"
                        >
                          {t('discovery.openDevice')}
                        </Link>
                      </>
                    )}
                  </span>
                )}
                {row.kind === 'missing' && (
                  <span className="text-status-warning">{t('discovery.notImportedRetry')}</span>
                )}
              </div>
              {row.kind === 'created' && row.sensors_skipped.length > 0 && (
                <SkippedList skipped={row.sensors_skipped} compact />
              )}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function ScanPage() {
  const { id: idParam } = useParams();
  const id = Number(idParam);
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const ws = useLiveState();
  const templates = useDeviceTemplates();

  const scan = useQuery({
    queryKey: qk.discoveryScan(id),
    queryFn: () => endpoints.discoveryScan(id),
    // Fallback per api.md: poll every 2 s while running and the WebSocket is not open.
    refetchInterval: (q) => (q.state.data?.status === 'running' && ws !== 'open' ? POLL_MS : false),
  });
  const running = scan.data?.status === 'running';
  const results = useQuery({
    queryKey: qk.discoveryResults(id),
    queryFn: async () => withBuffered(id, await endpoints.discoveryResults(id)),
    enabled: !!scan.data,
    refetchInterval: running && ws !== 'open' ? POLL_MS : false,
  });

  const [filters, setFilters] = useState<ResultFilters>(NO_FILTERS);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [edits, setEdits] = useState<Record<number, RowEdit>>({});
  const [groupId, setGroupId] = useState<number | null>(null);
  const [summary, setSummary] = useState<ImportRow[] | null>(null);
  const [importFailed, setImportFailed] = useState<unknown>(null);
  const [cancelAsk, setCancelAsk] = useState(false);
  const [deleteAsk, setDeleteAsk] = useState(false);

  useEffect(() => {
    if (groupId === null && scan.data?.group_id != null) setGroupId(scan.data.group_id);
  }, [scan.data?.group_id, groupId]);

  const all = useMemo(() => results.data ?? [], [results.data]);
  const visible = useMemo(() => filterResults(all, filters), [all, filters]);
  const counts = useMemo(() => {
    const c: Partial<Record<DeviceClass, number>> = {};
    for (const r of all) c[r.device_class] = (c[r.device_class] ?? 0) + 1;
    return c;
  }, [all]);
  const selectable = visible.filter((r) => !isExisting(r));
  const allSelected = selectable.length > 0 && selectable.every((r) => selected.has(r.id));
  // Drop selections that became non-selectable (e.g. imported meanwhile).
  const chosen = all.filter((r) => selected.has(r.id) && !isExisting(r));

  const cancel = useMutation({
    mutationFn: () => endpoints.cancelScan(id),
    onSuccess: (s) => {
      // Authoritative only when it reports the end state; a "running" answer must never
      // overwrite the cache (discovery.finished or the refetch below settles it).
      if (s.status !== 'running') qc.setQueryData(qk.discoveryScan(id), s);
      else void qc.invalidateQueries({ queryKey: qk.discoveryScan(id) });
      void qc.invalidateQueries({ queryKey: qk.discoveryResults(id) });
      void qc.invalidateQueries({ queryKey: ['discoveryScans'] });
      setCancelAsk(false);
    },
    onError: (e) => {
      setCancelAsk(false);
      toast.push('error', errorMessage(t, e));
      void qc.invalidateQueries({ queryKey: qk.discoveryScan(id) });
    },
  });
  const remove = useMutation({
    mutationFn: () => endpoints.deleteScan(id),
    onSuccess: () => {
      qc.removeQueries({ queryKey: qk.discoveryScan(id) });
      qc.removeQueries({ queryKey: qk.discoveryResults(id) });
      void qc.invalidateQueries({ queryKey: ['discoveryScans'] });
      toast.push('success', t('common.deleted'));
      navigate('/discovery');
    },
  });
  const doImport = useMutation({
    mutationFn: async (items: ImportItem[]) => {
      const res = await endpoints.importResults(id, { group_id: groupId, items });
      return importRows(items, res, all);
    },
    onMutate: () => setImportFailed(null),
    onSuccess: (rows) => {
      setSummary(rows);
      // Keep only the hosts worth retrying selected (with their edits).
      const again = new Set(retryIds(rows));
      setSelected(again);
      setEdits((e) => Object.fromEntries(Object.entries(e).filter(([k]) => again.has(Number(k)))));
      void qc.invalidateQueries({ queryKey: qk.discoveryResults(id) });
      void qc.invalidateQueries({ queryKey: qk.tree });
      void qc.invalidateQueries({ queryKey: ['devices'] });
      void qc.invalidateQueries({ queryKey: qk.dashboard });
    },
    // The request itself failed (network, proxy 502/504, timeout): the backend may still have
    // imported some hosts. Refetch the results; rows that became existing drop out of the
    // selection automatically (`chosen` skips them), the rest stay selected for a retry.
    onError: (e) => {
      setImportFailed(e);
      void qc.invalidateQueries({ queryKey: qk.discoveryResults(id) });
      void qc.invalidateQueries({ queryKey: qk.tree });
      void qc.invalidateQueries({ queryKey: ['devices'] });
    },
  });

  if (scan.isLoading) return <Loading />;
  if (scan.error || !scan.data) return <ErrorBox error={scan.error} />;
  const s = scan.data;

  const startImport = () =>
    doImport.mutate(
      chosen.map((r) => ({
        result_id: r.id,
        template_id: edits[r.id]?.template ?? r.suggested_template,
        name: edits[r.id]?.name?.trim() || null,
      })),
    );
  const setRowSelected = (rid: number, v: boolean) =>
    setSelected((cur) => {
      const n = new Set(cur);
      if (v) n.add(rid);
      else n.delete(rid);
      return n;
    });
  const rowProps = (r: DiscoveryResult) => ({
    r,
    templates: templates.data ?? [],
    selected: selected.has(r.id),
    onSelect: (v: boolean) => setRowSelected(r.id, v),
    edit: edits[r.id] ?? {},
    onEdit: (e: RowEdit) => setEdits((x) => ({ ...x, [r.id]: e })),
    disabled: doImport.isPending,
  });

  return (
    <div className="space-y-4">
      <PageHeader
        breadcrumbs={
          <Link to="/discovery" className="hover:underline">
            {t('discovery.title')}
          </Link>
        }
        title={
          <>
            <span>{t('discovery.scanTitle', { id: s.id })}</span>
            <ScanStatusBadge status={s.status} />
          </>
        }
        subtitle={
          <span className="flex flex-wrap gap-x-3">
            <span className="font-mono text-xs">
              <Ltr>{s.targets.join(', ')}</Ltr>
            </span>
            <span>
              {s.profile_name ? <bdi dir="auto">{s.profile_name}</bdi> : t('discovery.manual')}
              {' · '}
              {t(`discovery.trigger.${s.trigger}`)}
              {s.created_by && (
                <>
                  {' · '}
                  <bdi dir="auto">{s.created_by}</bdi>
                </>
              )}
            </span>
          </span>
        }
        actions={
          running ? (
            <Button variant="danger" busy={cancel.isPending} onClick={() => setCancelAsk(true)}>
              <StopIcon size={14} /> {t('discovery.cancel')}
            </Button>
          ) : (
            <>
              <Link to="/discovery" className={buttonClass()}>
                {t('discovery.newScan')}
              </Link>
              <Button variant="danger" onClick={() => setDeleteAsk(true)}>
                <TrashIcon size={14} /> {t('common.delete')}
              </Button>
            </>
          )
        }
      />

      <ProgressCard scan={s} polling={ws !== 'open'} />

      <Card title={t('discovery.results', { count: all.length })} bodyClassName="p-0">
        <div className="space-y-3 border-b border-line p-3">
          <FilterBar f={filters} onChange={setFilters} counts={counts} />
        </div>
        {results.isLoading && <Loading />}
        <ErrorBox error={results.error} className="m-3" />
        {results.data && !all.length && (
          <Empty>{running ? t('discovery.waitingResults') : t('discovery.noResults')}</Empty>
        )}
        {all.length > 0 && !visible.length && <Empty>{t('discovery.noMatch')}</Empty>}
        {visible.length > 0 && (
          <>
            {/* Desktop / tablet: table */}
            <div className="hidden overflow-x-auto md:block">
              <table className="w-full text-sm">
                <thead>
                  <tr>
                    <th className={th}>
                      <input
                        type="checkbox"
                        className="h-4 w-4 disabled:opacity-40"
                        aria-label={t('common.selectAll')}
                        checked={allSelected}
                        disabled={!selectable.length || doImport.isPending}
                        onChange={(e) =>
                          setSelected((cur) => {
                            const n = new Set(cur);
                            for (const r of selectable) {
                              if (e.target.checked) n.add(r.id);
                              else n.delete(r.id);
                            }
                            return n;
                          })
                        }
                      />
                    </th>
                    <th className={th}>{t('discovery.ip')}</th>
                    <th className={th}>{t('common.name')}</th>
                    <th className={th}>{t('discovery.classVendor')}</th>
                    <th className={th}>{t('discovery.reachability')}</th>
                    <th className={th}>{t('common.status')}</th>
                    <th className={th}>{t('templates.template')}</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((r) => (
                    <ResultRow key={r.id} {...rowProps(r)} />
                  ))}
                </tbody>
              </table>
            </div>
            {/* Phone: cards */}
            <div className="md:hidden">
              <div className="border-b border-line px-3 py-2">
                <Checkbox
                  label={t('common.selectAll')}
                  checked={allSelected}
                  disabled={!selectable.length || doImport.isPending}
                  onChange={(v) =>
                    setSelected((cur) => {
                      const n = new Set(cur);
                      for (const r of selectable) {
                        if (v) n.add(r.id);
                        else n.delete(r.id);
                      }
                      return n;
                    })
                  }
                />
              </div>
              <ul className="divide-y divide-line">
                {visible.map((r) => (
                  <ResultCard key={r.id} {...rowProps(r)} />
                ))}
              </ul>
            </div>
          </>
        )}
        {all.length > 0 && (
          <div className="sticky bottom-0 flex flex-wrap items-end gap-3 border-t border-line bg-surface p-3">
            <Field label={t('discovery.importGroup')} className="w-full sm:w-64">
              <GroupSelect value={groupId} onChange={setGroupId} />
            </Field>
            <div className="flex flex-1 flex-wrap items-center justify-end gap-2">
              {running && (
                <span className="text-xs text-ink-muted">{t('discovery.importAfterScan')}</span>
              )}
              <Button
                variant="primary"
                disabled={!chosen.length || running || groupId === null}
                busy={doImport.isPending}
                onClick={startImport}
              >
                {t('discovery.importSelected', { count: chosen.length })}
              </Button>
            </div>
            {importFailed ? (
              <div className="w-full space-y-1" role="alert">
                <ErrorBox error={importFailed} />
                <p className="text-sm text-status-warning">{t('discovery.importMaybePartial')}</p>
              </div>
            ) : null}
          </div>
        )}
      </Card>

      <Modal
        open={summary !== null}
        onClose={() => setSummary(null)}
        title={t('discovery.importResult')}
        wide
        footer={
          <Button variant="primary" onClick={() => setSummary(null)}>
            {t('common.close')}
          </Button>
        }
      >
        {summary && <ImportSummary rows={summary} templates={templates.data} />}
      </Modal>
      <ConfirmDialog
        open={cancelAsk}
        title={t('discovery.cancelTitle')}
        message={t('discovery.cancelConfirm')}
        confirmLabel={t('discovery.cancel')}
        onConfirm={() => cancel.mutate()}
        onClose={() => setCancelAsk(false)}
        busy={cancel.isPending}
      />
      <ConfirmDialog
        open={deleteAsk}
        title={t('discovery.deleteScanTitle')}
        message={t('discovery.deleteScanConfirm', { id: s.id })}
        onConfirm={() => remove.mutate()}
        onClose={() => setDeleteAsk(false)}
        busy={remove.isPending}
        error={remove.error}
      />
    </div>
  );
}
