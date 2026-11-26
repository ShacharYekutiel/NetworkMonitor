import { useEffect, useMemo, useRef, useState } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { TFunction } from 'i18next';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router';
import { isApiError } from '../../api/client';
import { endpoints3, qk3 } from '../../api/endpoints3';
import type { Device } from '../../api/types';
import type { BackupStatus, ConfigVersion } from '../../api/types3';
import type { DiffFlag, RiskSeverity } from '../../api/types';
import { endpoints3b, qk3b } from '../../api/endpoints3b';
import { useAiFeature } from '../assistant/common';
import { useAuth } from '../../hooks/auth';
import { errorMessage } from '../../lib/errors';
import { formatDateTime, formatRelative } from '../../lib/format';
import {
  hostKeyChanged,
  parseUnifiedDiff,
  resultTone,
  sideBySide,
  type DiffRow,
} from '../../lib/configBackup';
import { DetailRows } from '../Drawer';
import { DiffExplain } from '../assistant/DiffExplain';
import { Pagination } from '../Pagination';
import { ColumnsMenu, DataTable, labelOf, useDataTable, type DataColumn } from '../kitTable';
import { useToast } from '../toast';
import {
  Button,
  buttonClass,
  Card,
  ConfirmDialog,
  cx,
  Empty,
  ErrorBox,
  Loading,
  Notice,
  SegmentedControl,
  Select,
  TAG,
  TAG_TONE,
} from '../ui';

export function ResultBadge({ s }: { s: Pick<BackupStatus, 'last_result' | 'running'> }) {
  const { t } = useTranslation();
  if (s.running)
    return <span className={cx(TAG, TAG_TONE.accent)}>{t('configBackup.running')}</span>;
  const tone = resultTone(s.last_result);
  return (
    <span
      className={cx(
        TAG,
        tone === 'ok'
          ? TAG_TONE.up
          : tone === 'error'
            ? TAG_TONE.down
            : tone === 'warn'
              ? TAG_TONE.warning
              : TAG_TONE.neutral,
      )}
    >
      {s.last_result ? t(`configBackup.result.${s.last_result}`) : t('configBackup.never')}
    </span>
  );
}

function useWide(minPx: number): boolean {
  const q = `(min-width: ${minPx}px)`;
  const [wide, setWide] = useState(() =>
    typeof window !== 'undefined' && window.matchMedia ? window.matchMedia(q).matches : true,
  );
  useEffect(() => {
    if (!window.matchMedia) return;
    const m = window.matchMedia(q);
    const h = () => setWide(m.matches);
    m.addEventListener?.('change', h);
    return () => m.removeEventListener?.('change', h);
  }, [q]);
  return wide;
}

// prototype .code: added lines green, removed red, hunk headers grey
const rowTone: Record<DiffRow['type'], string> = {
  add: 'bg-status-up-soft text-status-up',
  del: 'bg-status-down-soft text-status-down',
  hunk: 'text-ink-subtle',
  context: 'text-ink',
  meta: 'text-ink-muted',
};

const RISK_TAG: Record<RiskSeverity, string> = {
  high: 'bg-status-down text-white',
  medium: 'bg-status-warning text-white',
  info: 'bg-surface text-ink-muted',
};
const SEV_RANK: Record<RiskSeverity, number> = { high: 3, medium: 2, info: 1 };

/** Worst flag severity per unified-diff line (the inline risk tags of the prototype). */
function riskByLine(flags: DiffFlag[] | undefined): Map<number, RiskSeverity> {
  const m = new Map<number, RiskSeverity>();
  for (const f of flags ?? [])
    for (const l of f.lines) {
      const cur = m.get(l);
      if (!cur || SEV_RANK[f.severity] > SEV_RANK[cur]) m.set(l, f.severity);
    }
  return m;
}

function UnifiedDiff({
  rows,
  hl,
  risks,
}: {
  rows: DiffRow[];
  hl?: ReadonlySet<number>;
  risks?: Map<number, RiskSeverity>;
}) {
  const { t } = useTranslation();
  return (
    <table
      dir="ltr"
      className="w-full font-mono text-[12.5px] leading-[1.65]"
      data-testid="diff-unified"
    >
      <tbody>
        {rows
          // [3b] line = 1-based line of the unified diff text (risk flags refer to it)
          .map((r, i) => ({ r, line: i + 1 }))
          .filter(({ r }) => r.type !== 'meta')
          .map(({ r, line }, i) => (
            <tr
              key={i}
              data-diff-line={line}
              className={cx(
                rowTone[r.type],
                hl?.has(line) && 'outline outline-2 -outline-offset-2 outline-status-warning',
              )}
            >
              <td className="w-12 select-none ps-4 pe-2 text-right text-ink-subtle">
                {r.oldNo ?? ''}
              </td>
              <td className="w-10 select-none pe-2 text-right text-ink-subtle">{r.newNo ?? ''}</td>
              <td className="w-4 select-none opacity-70">
                {r.type === 'add' ? '+' : r.type === 'del' ? '−' : ''}
              </td>
              <td className="whitespace-pre-wrap break-all pe-4 text-left">
                {risks?.get(line) && (
                  <span
                    className={cx(
                      'float-right ms-3 rounded-full px-2 py-px font-sans text-[11px] font-semibold',
                      RISK_TAG[risks.get(line)!],
                    )}
                  >
                    {t(`diffRisk.severity.${risks.get(line)!}`)}
                  </span>
                )}
                {r.text}
              </td>
            </tr>
          ))}
      </tbody>
    </table>
  );
}

function SideDiff({ rows }: { rows: DiffRow[] }) {
  const pairs = useMemo(() => sideBySide(rows), [rows]);
  return (
    <table
      dir="ltr"
      className="w-full table-fixed font-mono text-[12.5px] leading-[1.65]"
      data-testid="diff-side"
    >
      <colgroup>
        <col className="w-12" />
        <col />
        <col className="w-10" />
        <col />
      </colgroup>
      <tbody>
        {pairs.map((p, i) =>
          p.hunk ? (
            <tr key={i} className={rowTone.hunk}>
              <td colSpan={4} className="px-4 text-left">
                {p.hunk}
              </td>
            </tr>
          ) : (
            <tr key={i}>
              <td className="select-none ps-4 pe-2 text-right text-ink-subtle">
                {p.left?.oldNo ?? ''}
              </td>
              <td
                className={cx(
                  'whitespace-pre-wrap break-all border-e border-line pe-3 text-left',
                  p.left && p.left.type === 'del' && rowTone.del,
                )}
              >
                {p.left?.text ?? ''}
              </td>
              <td className="select-none pe-2 text-right text-ink-subtle">
                {p.right?.newNo ?? ''}
              </td>
              <td
                className={cx(
                  'whitespace-pre-wrap break-all pe-4 text-left',
                  p.right && p.right.type === 'add' && rowTone.add,
                )}
              >
                {p.right?.text ?? ''}
              </td>
            </tr>
          ),
        )}
      </tbody>
    </table>
  );
}

function versionLabel(t: TFunction, v: ConfigVersion) {
  return `${t('configBackup.versionNo', { n: v.version_no })} · ${formatDateTime(v.collected_at)}`;
}

/**
 * Two versions compared (prototype "השוואה"): from / to, unified or side by side, the change
 * statistics and download; the risk review card (DiffExplain) above the code; flagged lines carry
 * an inline risk tag in the unified view.
 */
export function DiffView({
  deviceId,
  versions,
  initialFrom,
  initialTo,
}: {
  deviceId: number;
  versions: ConfigVersion[];
  initialFrom: number;
  initialTo: number | null;
}) {
  const { t } = useTranslation();
  const [from, setFrom] = useState(initialFrom);
  const [to, setTo] = useState<number | null>(initialTo);
  useEffect(() => {
    setFrom(initialFrom);
    setTo(initialTo);
  }, [initialFrom, initialTo]);
  const wide = useWide(1200);
  const [mode, setMode] = useState<'auto' | 'unified'>('auto');
  const q = useQuery({
    queryKey: qk3.configDiff(deviceId, from, to),
    queryFn: () => endpoints3.configDiff(deviceId, from, to),
  });
  const rows = useMemo(() => parseUnifiedDiff(q.data?.diff ?? ''), [q.data]);
  const aiOn = useAiFeature('ai_config_diff');
  const flags = useQuery({
    queryKey: qk3b.diffFlags(deviceId, q.data?.from.id ?? 0, q.data?.to.id ?? 0),
    queryFn: () => endpoints3b.diffFlags(deviceId, q.data!.from.id, q.data!.to.id),
    enabled: aiOn && !!q.data,
  });
  const risks = useMemo(() => riskByLine(flags.data?.flags), [flags.data]);
  // [3b] risk flags / AI risks point at diff lines
  const [hl, setHl] = useState<ReadonlySet<number>>(new Set());
  const scrollRef = useRef<HTMLDivElement>(null);
  const showLines = (lines: number[]) => {
    setMode('unified');
    setHl(new Set(lines));
    requestAnimationFrame(() =>
      scrollRef.current
        ?.querySelector(`[data-diff-line="${lines[0]}"]`)
        ?.scrollIntoView({ block: 'center', behavior: 'smooth' }),
    );
  };
  const side = wide && mode === 'auto';
  const changed = rows.some((r) => r.type === 'add' || r.type === 'del');
  return (
    <div className="grid gap-4" data-testid="diff-view">
      {q.data && changed && (
        <DiffExplain
          deviceId={deviceId}
          from={q.data.from.id}
          to={q.data.to.id}
          onLines={showLines}
        />
      )}
      <Card
        title={t('configBackup.diff')}
        bodyClassName="px-[22px] pb-5 pt-1"
        actions={
          <>
            {q.data && (
              <span className="num text-[13px] font-medium" dir="ltr">
                <span className="text-status-up">+{q.data.added}</span>{' '}
                <span className="text-status-down">−{q.data.removed}</span>
              </span>
            )}
            {wide && (
              <SegmentedControl
                label={t('configBackup.diffMode')}
                size="sm"
                value={side ? 'side' : 'unified'}
                onChange={(v) => setMode(v === 'side' ? 'auto' : 'unified')}
                options={[
                  { value: 'unified', label: t('configBackup.unified') },
                  { value: 'side', label: t('configBackup.sideBySide') },
                ]}
              />
            )}
            {q.data && (
              <a
                className={buttonClass('secondary', 'sm')}
                href={endpoints3.configDownloadUrl(deviceId, q.data.to.id)}
                download
              >
                {t('configBackup.download')}
              </a>
            )}
          </>
        }
      >
        <div className="mb-4 flex flex-wrap items-center gap-3 text-sm">
          <label className="flex items-center gap-2">
            <span className="text-ink-muted">{t('configBackup.from')}</span>
            <Select
              value={from}
              onChange={(e) => setFrom(Number(e.target.value))}
              className="w-auto"
            >
              {versions.map((v) => (
                <option key={v.id} value={v.id}>
                  {versionLabel(t, v)}
                </option>
              ))}
            </Select>
          </label>
          <label className="flex items-center gap-2">
            <span className="text-ink-muted">{t('configBackup.to')}</span>
            <Select
              value={to ?? ''}
              onChange={(e) => setTo(e.target.value ? Number(e.target.value) : null)}
              className="w-auto"
            >
              <option value="">{t('configBackup.latest')}</option>
              {versions.map((v) => (
                <option key={v.id} value={v.id}>
                  {versionLabel(t, v)}
                </option>
              ))}
            </Select>
          </label>
        </div>
        {q.isLoading && <Loading />}
        <ErrorBox error={q.error} />
        {q.data?.truncated && (
          <p className="m-0 mb-2 text-xs text-status-warning">{t('configBackup.diffTruncated')}</p>
        )}
        {q.data && !changed && <Empty>{t('configBackup.noDifferences')}</Empty>}
        {q.data && rows.length > 0 && (
          // prototype .code: chip background, radius 16, mono 12.5/1.65, LTR
          <div className="max-h-[70vh] overflow-auto rounded-[16px] bg-chip py-3.5" ref={scrollRef}>
            {side ? <SideDiff rows={rows} /> : <UnifiedDiff rows={rows} hl={hl} risks={risks} />}
          </div>
        )}
      </Card>
    </div>
  );
}

function ContentView({ deviceId, version }: { deviceId: number; version: ConfigVersion }) {
  const { t } = useTranslation();
  const q = useQuery({
    queryKey: qk3.configVersion(deviceId, version.id),
    queryFn: () => endpoints3.configVersion(deviceId, version.id),
    staleTime: Infinity,
  });
  const lines = useMemo(() => (q.data?.content ?? '').split('\n'), [q.data]);
  return (
    <Card
      title={versionLabel(t, version)}
      bodyClassName="p-0"
      actions={
        <a
          className={buttonClass('secondary', 'sm')}
          href={endpoints3.configDownloadUrl(deviceId, version.id)}
          download
        >
          {t('configBackup.download')}
        </a>
      }
    >
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} className="m-3" />
      {q.data && (
        <div className="max-h-[70vh] overflow-auto p-2" data-testid="config-content">
          {version.masked && (
            <p className="mb-1 text-xs text-ink-muted">{t('configBackup.maskedNote')}</p>
          )}
          <table dir="ltr" className="font-mono text-[13px]">
            <tbody>
              {lines.map((l, i) => (
                <tr key={i}>
                  <td className="select-none pe-3 text-right align-top text-ink-subtle">{i + 1}</td>
                  <td className="whitespace-pre text-left">{l}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function HostKeyCard({ device, s }: { device: Device; s: BackupStatus }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const toast = useToast();
  const [resetting, setResetting] = useState(false);
  const hk = s.host_key;
  const done = (msg: string) => {
    void qc.invalidateQueries({ queryKey: qk3.deviceConfig(device.id) });
    toast.push('success', msg);
  };
  const accept = useMutation({
    mutationFn: () => endpoints3.acceptHostKey(device.id, hk?.presented_fingerprint ?? ''),
    onSuccess: () => done(t('hostKey.accepted')),
  });
  const reset = useMutation({
    mutationFn: () => endpoints3.resetHostKey(device.id),
    onSuccess: () => {
      setResetting(false);
      done(t('hostKey.resetDone'));
    },
  });
  if (!hk) return <p className="text-sm text-ink-muted">{t('hostKey.none')}</p>;
  const changed = hostKeyChanged(s);
  return (
    <div className="space-y-2 text-sm">
      {changed && (
        <div role="alert" className="rounded-md bg-status-down-soft px-3 py-2 text-status-down">
          <p className="font-medium">{t('hostKey.changedTitle')}</p>
          <p className="text-xs">{t('hostKey.changedText')}</p>
        </div>
      )}
      <DetailRows
        rows={[
          [
            t('hostKey.pinned'),
            <bdi key="p" dir="ltr" className="break-all font-mono text-xs">
              {hk.algorithm} {hk.fingerprint}
            </bdi>,
          ],
          [t('hostKey.pinnedAt'), formatDateTime(hk.pinned_at)],
          ...(hk.accepted_by
            ? ([[t('hostKey.acceptedBy'), hk.accepted_by]] as [string, string][])
            : []),
          ...(changed
            ? ([
                [
                  t('hostKey.presented'),
                  <bdi key="n" dir="ltr" className="break-all font-mono text-xs text-status-down">
                    {hk.presented_algorithm} {hk.presented_fingerprint}
                  </bdi>,
                ],
                [t('hostKey.changedAt'), formatDateTime(hk.changed_at)],
              ] as [string, React.ReactNode][])
            : []),
        ]}
      />
      {isAdmin && (
        <div className="flex flex-wrap gap-2">
          {changed && (
            <Button
              size="sm"
              variant="primary"
              busy={accept.isPending}
              onClick={() => accept.mutate()}
            >
              {t('hostKey.accept')}
            </Button>
          )}
          <Button size="sm" onClick={() => setResetting(true)}>
            {t('hostKey.reset')}
          </Button>
        </div>
      )}
      <ErrorBox error={accept.error} />
      <ConfirmDialog
        open={resetting}
        title={t('hostKey.reset')}
        message={t('hostKey.resetConfirm')}
        confirmLabel={t('hostKey.reset')}
        onConfirm={() => reset.mutate()}
        onClose={() => setResetting(false)}
        busy={reset.isPending}
        error={reset.error}
      />
    </div>
  );
}

/** [3] Device page tab "Configuration": status, host key, versions, viewer and diff. */
export function ConfigTab({ device }: { device: Device }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const [page, setPage] = useState(1);
  // [3b] deep links from the assistant: ?version=<vid> or ?from=<a>&to=<b>
  const [params] = useSearchParams();
  const pInt = (k: string) => (/^\d+$/.test(params.get(k) ?? '') ? Number(params.get(k)) : null);
  const [view, setView] = useState<number | null>(() => pInt('version'));
  const [diff, setDiff] = useState<{ from: number; to: number | null } | null>(() => {
    const f = pInt('from');
    return f !== null ? { from: f, to: pInt('to') } : null;
  });
  const status = useQuery({
    queryKey: qk3.deviceConfig(device.id),
    queryFn: () => endpoints3.deviceConfig(device.id),
  });
  const versions = useQuery({
    queryKey: qk3.configVersions(device.id, page),
    queryFn: () => endpoints3.configVersions(device.id, page),
    placeholderData: keepPreviousData,
  });
  const backup = useMutation({
    mutationFn: () => endpoints3.backupNow(device.id),
    onSuccess: () => {
      toast.push('success', t('configBackup.queued'));
      void qc.invalidateQueries({ queryKey: qk3.deviceConfig(device.id) });
    },
    onError: (e) => {
      if (isApiError(e) && e.code === 'backup_running')
        toast.push('info', t('errors.backup_running'));
      else if (isApiError(e) && e.details?.[0]?.reason)
        toast.push('error', t(`configBackup.reason.${e.details[0].reason}`));
      else toast.push('error', errorMessage(t, e));
    },
  });
  const s = status.data;
  const items = versions.data?.items ?? [];
  const selected = items.find((v) => v.id === view) ?? null;
  const verCols: DataColumn<ConfigVersion>[] = [
    { id: 'no', label: '#', width: 64, className: 'tabular-nums', cell: (v) => v.version_no },
    {
      id: 'collected',
      label: t('configBackup.collectedAt'),
      minWidth: 160,
      title: (v) => `${formatDateTime(v.collected_at)} (${formatRelative(v.collected_at)})`,
      cell: (v) => (
        <>
          {formatDateTime(v.collected_at)}
          <span className="ms-1 text-xs text-ink-muted">({formatRelative(v.collected_at)})</span>
        </>
      ),
    },
    {
      id: 'trigger',
      label: t('configBackup.triggerLabel'),
      width: 150,
      cell: (v) => t(`configBackup.trigger.${v.trigger}`),
    },
    {
      id: 'changes',
      label: t('configBackup.changes'),
      width: 180,
      className: 'tabular-nums',
      cell: (v) => (
        <>
          {v.initial ? (
            <span className="rounded bg-surface-sunken px-1.5 py-0.5 text-xs">
              {t('configBackup.initial')}
            </span>
          ) : (
            <bdi dir="ltr">
              <span className="text-status-up">+{v.added}</span>{' '}
              <span className="text-status-down">−{v.removed}</span>
            </bdi>
          )}
          {v.is_latest && (
            <span className="ms-1 rounded bg-accent-soft px-1.5 py-0.5 text-xs text-accent-ink">
              {t('configBackup.latest')}
            </span>
          )}
        </>
      ),
    },
    {
      id: 'lines',
      label: t('configBackup.lines'),
      width: 90,
      className: 'tabular-nums',
      cell: (v) => v.lines,
    },
    ...(isAdmin
      ? [
          {
            id: 'actions',
            label: t('tableCols.actions'),
            pin: 'end' as const,
            fixed: true,
            width: 250,
            cell: (v: ConfigVersion) => {
              const i = items.indexOf(v);
              return (
                <span className="inline-flex gap-1">
                  <Button size="sm" onClick={() => setView(v.id === view ? null : v.id)}>
                    {t('configBackup.view')}
                  </Button>
                  {items[i + 1] && (
                    <Button size="sm" onClick={() => setDiff({ from: items[i + 1].id, to: v.id })}>
                      {t('configBackup.diffPrev')}
                    </Button>
                  )}
                </span>
              );
            },
          },
        ]
      : []),
  ];
  const verLayout = useDataTable('config-versions', verCols);
  return (
    <div className="space-y-4">
      {status.isLoading && <Loading />}
      <ErrorBox error={status.error} />
      {s && (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <Card
            title={t('configBackup.status')}
            actions={
              isAdmin && (
                <Button
                  size="sm"
                  variant="primary"
                  busy={backup.isPending}
                  disabled={s.running}
                  onClick={() => backup.mutate()}
                >
                  {t('configBackup.backupNow')}
                </Button>
              )
            }
          >
            <div className="space-y-3 text-sm">
              {!s.eligible && <Notice>{t(`configBackup.reason.${s.reason}`)}</Notice>}
              <DetailRows
                rows={[
                  [t('configBackup.lastResult'), <ResultBadge key="r" s={s} />],
                  [
                    t('configBackup.lastAttempt'),
                    s.last_attempt_at
                      ? `${formatDateTime(s.last_attempt_at)}${s.last_trigger ? ` · ${t(`configBackup.trigger.${s.last_trigger}`)}` : ''}`
                      : '—',
                  ],
                  [t('configBackup.lastSuccess'), formatDateTime(s.last_success_at)],
                  [t('configBackup.lastChange'), formatDateTime(s.last_change_at)],
                  [t('configBackup.nextDue'), formatDateTime(s.next_due_at)],
                  [
                    t('driver.label'),
                    s.driver ? (
                      <span key="d">
                        <bdi dir="ltr">{s.driver}</bdi>
                        {s.driver_source && (
                          <span className="ms-1 text-xs text-ink-muted">
                            ({t(`driver.source.${s.driver_source}`)})
                          </span>
                        )}
                      </span>
                    ) : (
                      '—'
                    ),
                  ],
                  [
                    t('ssh.credentialsSource'),
                    t(`ssh.source.${device.effective_ssh_source ?? 'none'}`),
                  ],
                  [t('configBackup.mode'), t(`configBackup.modes.${s.mode}`)],
                  [t('configBackup.versions'), String(s.versions)],
                ]}
              />
              {s.consecutive_failures > 0 && (
                <p className="text-xs text-status-down">
                  {t('configBackup.failures', { n: s.consecutive_failures })}
                </p>
              )}
              {(s.last_error || s.last_message) && (
                <pre
                  dir="ltr"
                  className="whitespace-pre-wrap break-all rounded bg-surface-sunken p-2 text-left font-mono text-xs text-ink"
                >
                  {[s.last_error, s.last_message].filter(Boolean).join('\n')}
                </pre>
              )}
            </div>
          </Card>
          <Card title={t('hostKey.title')}>
            <HostKeyCard device={device} s={s} />
          </Card>
        </div>
      )}
      <Card
        title={t('configBackup.versionList')}
        bodyClassName="p-0"
        actions={
          items.length > 0 && (
            <ColumnsMenu
              layout={verLayout}
              labelOf={(id) => labelOf(verCols, id)}
              testId="config-version-columns"
            />
          )
        }
      >
        <ErrorBox error={versions.error} className="m-3" />
        {versions.data && !items.length && <Empty>{t('configBackup.noVersions')}</Empty>}
        {items.length > 0 && (
          <>
            <DataTable
              layout={verLayout}
              columns={verLayout.columns}
              rows={items}
              rowKey={(v) => v.id}
              rowProps={(v) => ({
                className: cx(
                  view === v.id && '[&>td]:bg-[color-mix(in_srgb,var(--accent)_8%,var(--surface))]',
                ),
              })}
              testId="config-versions"
            />
            <Pagination
              page={page}
              pageSize={versions.data?.page_size ?? 50}
              total={versions.data?.total ?? 0}
              onPage={setPage}
            />
          </>
        )}
        {!isAdmin && items.length > 0 && (
          <p className="px-3 pb-3 text-xs text-ink-muted">{t('configBackup.adminOnly')}</p>
        )}
      </Card>
      {isAdmin && diff && (
        <DiffView
          deviceId={device.id}
          versions={items}
          initialFrom={diff.from}
          initialTo={diff.to}
        />
      )}
      {isAdmin && selected && <ContentView deviceId={device.id} version={selected} />}
    </div>
  );
}
