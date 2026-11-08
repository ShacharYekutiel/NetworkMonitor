import { useEffect, useMemo, useState } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { isApiError } from '../../api/client';
import { endpoints3, qk3 } from '../../api/endpoints3';
import type { Device } from '../../api/types';
import type { BackupStatus, ConfigVersion } from '../../api/types3';
import { useAuth } from '../../hooks/auth';
import { errorMessage } from '../../lib/errors';
import { formatDateTime, formatRelative } from '../../lib/format';
import {
  changeStat,
  hostKeyChanged,
  parseUnifiedDiff,
  resultTone,
  sideBySide,
  type DiffRow,
} from '../../lib/configBackup';
import { DetailRows } from '../Drawer';
import { Pagination } from '../Pagination';
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
  Select,
  Table,
  td,
  th,
} from '../ui';

export function ResultBadge({ s }: { s: Pick<BackupStatus, 'last_result' | 'running'> }) {
  const { t } = useTranslation();
  if (s.running)
    return (
      <span className="inline-flex items-center gap-1 rounded bg-sky-100 px-1.5 py-0.5 text-xs font-medium text-sky-800 dark:bg-sky-900/40 dark:text-sky-200">
        <span className="h-2 w-2 animate-pulse rounded-full bg-sky-500" />
        {t('configBackup.running')}
      </span>
    );
  const tone = resultTone(s.last_result);
  return (
    <span
      className={cx(
        'rounded px-1.5 py-0.5 text-xs font-medium',
        tone === 'ok' && 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-200',
        tone === 'error' && 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-200',
        tone === 'warn' && 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200',
        tone === 'muted' && 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
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

const rowTone: Record<DiffRow['type'], string> = {
  add: 'bg-green-50 text-green-900 dark:bg-green-950/40 dark:text-green-200',
  del: 'bg-red-50 text-red-900 dark:bg-red-950/40 dark:text-red-200',
  hunk: 'bg-sky-50 text-sky-800 dark:bg-sky-950/40 dark:text-sky-300',
  context: '',
  meta: 'text-slate-500',
};

function UnifiedDiff({ rows }: { rows: DiffRow[] }) {
  return (
    <table dir="ltr" className="w-full font-mono text-xs" data-testid="diff-unified">
      <tbody>
        {rows
          .filter((r) => r.type !== 'meta')
          .map((r, i) => (
            <tr key={i} className={rowTone[r.type]}>
              <td className="w-10 select-none pe-2 text-right text-slate-400">{r.oldNo ?? ''}</td>
              <td className="w-10 select-none pe-2 text-right text-slate-400">{r.newNo ?? ''}</td>
              <td className="w-4 select-none text-slate-500">
                {r.type === 'add' ? '+' : r.type === 'del' ? '−' : ''}
              </td>
              <td className="whitespace-pre-wrap break-all text-left">{r.text}</td>
            </tr>
          ))}
      </tbody>
    </table>
  );
}

function SideDiff({ rows }: { rows: DiffRow[] }) {
  const pairs = useMemo(() => sideBySide(rows), [rows]);
  return (
    <table dir="ltr" className="w-full table-fixed font-mono text-xs" data-testid="diff-side">
      <colgroup>
        <col className="w-10" />
        <col />
        <col className="w-10" />
        <col />
      </colgroup>
      <tbody>
        {pairs.map((p, i) =>
          p.hunk ? (
            <tr key={i} className={rowTone.hunk}>
              <td colSpan={4} className="px-1 text-left">
                {p.hunk}
              </td>
            </tr>
          ) : (
            <tr key={i}>
              <td className="select-none pe-2 text-right text-slate-400">{p.left?.oldNo ?? ''}</td>
              <td
                className={cx(
                  'whitespace-pre-wrap break-all text-left',
                  p.left && p.left.type === 'del' && rowTone.del,
                )}
              >
                {p.left?.text ?? ''}
              </td>
              <td className="select-none pe-2 text-right text-slate-400">{p.right?.newNo ?? ''}</td>
              <td
                className={cx(
                  'whitespace-pre-wrap break-all text-left',
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

function versionLabel(t: (k: string, o?: Record<string, unknown>) => string, v: ConfigVersion) {
  return `${t('configBackup.versionNo', { n: v.version_no })} · ${formatDateTime(v.collected_at)}`;
}

function DiffView({
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
  return (
    <Card
      title={t('configBackup.diff')}
      bodyClassName="p-0"
      actions={
        q.data && (
          <span className="text-xs tabular-nums">
            <bdi dir="ltr">{changeStat(q.data.added, q.data.removed)}</bdi>
          </span>
        )
      }
    >
      <div className="flex flex-wrap items-end gap-2 border-b border-slate-200 p-3 text-sm dark:border-slate-800">
        <label className="flex items-center gap-1">
          <span className="text-xs text-slate-500">{t('configBackup.from')}</span>
          <Select value={from} onChange={(e) => setFrom(Number(e.target.value))} className="w-auto">
            {versions.map((v) => (
              <option key={v.id} value={v.id}>
                {versionLabel(t, v)}
              </option>
            ))}
          </Select>
        </label>
        <label className="flex items-center gap-1">
          <span className="text-xs text-slate-500">{t('configBackup.to')}</span>
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
        {wide && (
          <Button size="sm" onClick={() => setMode((m) => (m === 'auto' ? 'unified' : 'auto'))}>
            {mode === 'auto' ? t('configBackup.unified') : t('configBackup.sideBySide')}
          </Button>
        )}
      </div>
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} className="m-3" />
      {q.data?.truncated && (
        <p className="px-3 pt-2 text-xs text-amber-700 dark:text-amber-300">
          {t('configBackup.diffTruncated')}
        </p>
      )}
      {q.data && !rows.some((r) => r.type === 'add' || r.type === 'del') && (
        <Empty>{t('configBackup.noDifferences')}</Empty>
      )}
      {q.data && rows.length > 0 && (
        <div className="max-h-[70vh] overflow-auto p-2">
          {wide && mode === 'auto' ? <SideDiff rows={rows} /> : <UnifiedDiff rows={rows} />}
        </div>
      )}
    </Card>
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
            <p className="mb-1 text-xs text-slate-500">{t('configBackup.maskedNote')}</p>
          )}
          <table dir="ltr" className="font-mono text-xs">
            <tbody>
              {lines.map((l, i) => (
                <tr key={i}>
                  <td className="select-none pe-3 text-right align-top text-slate-400">{i + 1}</td>
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
  if (!hk) return <p className="text-sm text-slate-500">{t('hostKey.none')}</p>;
  const changed = hostKeyChanged(s);
  return (
    <div className="space-y-2 text-sm">
      {changed && (
        <div
          role="alert"
          className="rounded-md bg-red-50 px-3 py-2 text-red-800 ring-1 ring-inset ring-red-200 dark:bg-red-950/40 dark:text-red-200 dark:ring-red-900"
        >
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
                  <bdi
                    key="n"
                    dir="ltr"
                    className="break-all font-mono text-xs text-red-700 dark:text-red-300"
                  >
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
  const [view, setView] = useState<number | null>(null);
  const [diff, setDiff] = useState<{ from: number; to: number | null } | null>(null);
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
                          <span className="ms-1 text-xs text-slate-500">
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
                <p className="text-xs text-red-600">
                  {t('configBackup.failures', { n: s.consecutive_failures })}
                </p>
              )}
              {(s.last_error || s.last_message) && (
                <pre
                  dir="ltr"
                  className="whitespace-pre-wrap break-all rounded bg-slate-50 p-2 text-left font-mono text-xs text-slate-700 dark:bg-slate-800 dark:text-slate-200"
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
      <Card title={t('configBackup.versionList')} bodyClassName="p-0">
        <ErrorBox error={versions.error} className="m-3" />
        {versions.data && !items.length && <Empty>{t('configBackup.noVersions')}</Empty>}
        {items.length > 0 && (
          <>
            <Table>
              <thead>
                <tr>
                  <th className={th}>#</th>
                  <th className={th}>{t('configBackup.collectedAt')}</th>
                  <th className={th}>{t('configBackup.triggerLabel')}</th>
                  <th className={th}>{t('configBackup.changes')}</th>
                  <th className={th}>{t('configBackup.lines')}</th>
                  <th className={th} />
                </tr>
              </thead>
              <tbody>
                {items.map((v, i) => (
                  <tr key={v.id} className={cx(view === v.id && 'bg-sky-50 dark:bg-sky-900/20')}>
                    <td className={`${td} tabular-nums`}>{v.version_no}</td>
                    <td className={td}>
                      {formatDateTime(v.collected_at)}
                      <span className="ms-1 text-xs text-slate-500">
                        ({formatRelative(v.collected_at)})
                      </span>
                    </td>
                    <td className={td}>{t(`configBackup.trigger.${v.trigger}`)}</td>
                    <td className={`${td} tabular-nums`}>
                      {v.initial ? (
                        <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs dark:bg-slate-800">
                          {t('configBackup.initial')}
                        </span>
                      ) : (
                        <bdi dir="ltr">
                          <span className="text-green-700 dark:text-green-400">+{v.added}</span>{' '}
                          <span className="text-red-600">−{v.removed}</span>
                        </bdi>
                      )}
                      {v.is_latest && (
                        <span className="ms-1 rounded bg-sky-100 px-1.5 py-0.5 text-xs text-sky-800 dark:bg-sky-900/40 dark:text-sky-200">
                          {t('configBackup.latest')}
                        </span>
                      )}
                    </td>
                    <td className={`${td} tabular-nums`}>{v.lines}</td>
                    <td className={`${td} text-end`}>
                      {isAdmin && (
                        <span className="inline-flex gap-1">
                          <Button size="sm" onClick={() => setView(v.id === view ? null : v.id)}>
                            {t('configBackup.view')}
                          </Button>
                          {items[i + 1] && (
                            <Button
                              size="sm"
                              onClick={() => setDiff({ from: items[i + 1].id, to: v.id })}
                            >
                              {t('configBackup.diffPrev')}
                            </Button>
                          )}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </Table>
            <Pagination
              page={page}
              pageSize={versions.data?.page_size ?? 50}
              total={versions.data?.total ?? 0}
              onPage={setPage}
            />
          </>
        )}
        {!isAdmin && items.length > 0 && (
          <p className="px-3 pb-3 text-xs text-slate-500">{t('configBackup.adminOnly')}</p>
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
