import { useMemo, useState, type FormEvent } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { endpoints3, qk3, type ConfigSearchQuery } from '../api/endpoints3';
import { BACKUP_RESULTS, type BackupStatus, type ConfigVersion } from '../api/types3';
import type { RiskSeverity } from '../api/types';
import { endpoints3b, qk3b } from '../api/endpoints3b';
import { DiffView, ResultBadge } from '../components/configbackup/ConfigTab';
import { useAiFeature } from '../components/assistant/common';
import { ChevronIcon } from '../components/icons';
import { Banner } from '../components/kit';
import { GroupSelect } from '../components/EntityForms';
import { Pagination } from '../components/Pagination';
import { useToast } from '../components/toast';
import {
  Button,
  buttonClass,
  Card,
  Checkbox,
  cx,
  Empty,
  ErrorBox,
  GroupList,
  GroupTitle,
  Input,
  KeyValue,
  ListRow,
  Loading,
  PageHeader,
  Select,
  TAG,
  TAG_TONE,
} from '../components/ui';
import { FeatureOffNotice, useFeatures } from '../hooks/features';
import { useAuth } from '../hooks/auth';
import { useLiveMessages } from '../hooks/live';
import { highlight, hostKeyChanged, resultTone, searchError } from '../lib/configBackup';
import { formatDateTime, formatShortTime } from '../lib/format';

function SearchCard() {
  const { t } = useTranslation();
  const [text, setText] = useState('');
  const [regex, setRegex] = useState(false);
  const [all, setAll] = useState(false);
  const [group, setGroup] = useState<number | null>(null);
  const [query, setQuery] = useState<ConfigSearchQuery | null>(null);
  const err = text ? searchError(text, regex) : null;
  const q = useQuery({
    queryKey: qk3.backupSearch(query ?? {}),
    queryFn: () => endpoints3.searchConfigs(query as ConfigSearchQuery),
    enabled: !!query,
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (searchError(text, regex)) return;
    setQuery({
      q: text.trim(),
      regex: regex || undefined,
      all_versions: all || undefined,
      group_id: group ?? undefined,
    });
  };
  return (
    <Card title={t('configBackup.search')} anchor="config-search">
      <form
        onSubmit={submit}
        className="flex flex-wrap items-end gap-3"
        role="search"
        id="config-search"
      >
        <label className="min-w-[16rem] flex-1">
          <span className="mb-1 block text-xs font-medium text-ink-muted">
            {t('configBackup.searchLabel')}
          </span>
          <Input
            value={text}
            onChange={(e) => setText(e.target.value)}
            dir="ltr"
            className="font-mono"
            placeholder="vlan 20"
            aria-invalid={!!err}
          />
        </label>
        <div className="w-56">
          <GroupSelect
            value={group}
            onChange={setGroup}
            allowRoot
            rootLabel={t('configBackup.allGroups')}
          />
        </div>
        <Checkbox label={t('configBackup.regex')} checked={regex} onChange={setRegex} />
        <Checkbox label={t('configBackup.allVersions')} checked={all} onChange={setAll} />
        <Button type="submit" variant="primary" disabled={!!err || !text} busy={q.isFetching}>
          {t('configBackup.searchGo')}
        </Button>
      </form>
      {err && <p className="mt-1 text-xs text-status-down">{t(err)}</p>}
      <ErrorBox error={q.error} className="mt-3" />
      {q.data && (
        <div className="mt-3 space-y-3" data-testid="config-search-results">
          {!q.data.items.length && <Empty>{t('configBackup.noMatches')}</Empty>}
          {q.data.truncated && (
            <p className="text-xs text-status-warning">{t('configBackup.searchTruncated')}</p>
          )}
          {q.data.items.map((it) => (
            <div key={`${it.device_id}-${it.version_id}`} className="rounded-lg ring-1 ring-line">
              <div className="flex flex-wrap items-baseline gap-2 border-b border-line px-3 py-1.5 text-sm">
                <Link
                  to={`/devices/${it.device_id}?tab=config`}
                  className="font-medium text-accent-ink hover:underline"
                >
                  {it.device_name}
                </Link>
                <span className="text-xs text-ink-muted">{formatDateTime(it.collected_at)}</span>
              </div>
              <table dir="ltr" className="w-full font-mono text-[13px]">
                <tbody>
                  {it.matches.map((m) => (
                    <tr key={m.line}>
                      <td className="w-12 select-none pe-2 text-right align-top text-ink-subtle">
                        {m.line}
                      </td>
                      <td className="whitespace-pre-wrap break-all text-left">
                        {highlight(m.text, query?.q ?? '', !!query?.regex).map((sg, i) =>
                          sg.hit ? (
                            <mark
                              key={i}
                              className="rounded-sm bg-status-warning-soft text-inherit"
                            >
                              {sg.text}
                            </mark>
                          ) : (
                            <span key={i}>{sg.text}</span>
                          ),
                        )}
                      </td>
                    </tr>
                  ))}
                  {it.more && (
                    <tr>
                      <td />
                      <td className="text-left text-ink-muted">…</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

/** Dot of a device's backup state (prototype backups rows): ok green, failing red, attention amber. */
function backupDot(s: BackupStatus): string {
  if (s.running) return 'bg-accent';
  if (hostKeyChanged(s)) return 'bg-status-warning';
  if (!s.eligible || !s.last_result) return 'bg-status-paused';
  const tone = resultTone(s.last_result);
  return tone === 'error'
    ? 'bg-status-down'
    : tone === 'warn'
      ? 'bg-status-warning'
      : 'bg-status-up';
}

/** The grey line under a device: what matters most about its backups right now. */
function useBackupLine() {
  const { t } = useTranslation();
  return (s: BackupStatus): string => {
    if (s.running) return t('configBackup.running');
    if (!s.eligible) return t(`configBackup.reason.${s.reason}`);
    if (hostKeyChanged(s)) return t('hostKey.changedShort');
    if (resultTone(s.last_result) === 'error')
      return s.consecutive_failures > 1
        ? t('configBackup.failures', { n: s.consecutive_failures })
        : t(`configBackup.result.${s.last_result}`);
    if (s.last_change_at)
      return t('configBackup.changedAt', { when: formatShortTime(s.last_change_at) });
    if (s.versions) return t('configBackup.noChange');
    return t('configBackup.never');
  };
}

const RISK_ORDER: Record<RiskSeverity, number> = { high: 3, medium: 2, info: 1 };

/** "High risk" tag of one version against the one before it (deterministic flags, feature ai_config_diff). */
function VersionRisk({ deviceId, from, to }: { deviceId: number; from: number; to: number }) {
  const { t } = useTranslation();
  const q = useQuery({
    queryKey: qk3b.diffFlags(deviceId, from, to),
    queryFn: () => endpoints3b.diffFlags(deviceId, from, to),
    staleTime: 300_000,
  });
  const worst = (q.data?.flags ?? []).reduce<RiskSeverity | null>(
    (w, f) => (!w || RISK_ORDER[f.severity] > RISK_ORDER[w] ? f.severity : w),
    null,
  );
  if (!worst || worst === 'info') return null;
  return (
    <span className={cx(TAG, worst === 'high' ? TAG_TONE.down : TAG_TONE.warning)}>
      {t('diffRisk.riskTag', { level: t(`diffRisk.severity.${worst}`) })}
    </span>
  );
}

/**
 * The selected device (prototype backups side card): how it is backed up, its latest versions
 * (each opens the comparison with the one before), the masking note.
 */
function DeviceCard({
  s,
  onDiff,
  diffTo,
}: {
  s: BackupStatus;
  onDiff: (from: number, to: number, versions: ConfigVersion[]) => void;
  diffTo: number | null;
}) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const aiDiff = useAiFeature('ai_config_diff');
  const versions = useQuery({
    queryKey: qk3.configVersions(s.device_id, 1),
    queryFn: () => endpoints3.configVersions(s.device_id, 1),
  });
  const items = versions.data?.items ?? [];
  const shown = items.slice(0, 5);
  return (
    <Card
      title={
        <Link to={`/devices/${s.device_id}?tab=config`} className="hover:underline">
          <bdi dir="auto">{s.device_name}</bdi>
        </Link>
      }
      actions={<ResultBadge s={s} />}
    >
      <div className="grid gap-4" data-testid="backup-device">
        <KeyValue
          items={[
            {
              k: t('configBackup.mode'),
              v: (
                <span>
                  {t(`configBackup.modes.${s.mode}`)}
                  {s.driver && (
                    <>
                      {' · '}
                      <bdi dir="ltr">{s.driver}</bdi>
                    </>
                  )}
                </span>
              ),
            },
            { k: t('configBackup.nextDue'), v: formatDateTime(s.next_due_at) },
            { k: t('configBackup.lastChange'), v: formatDateTime(s.last_change_at) },
            { k: t('configBackup.kept'), v: t('configBackup.versionsN', { count: s.versions }) },
          ]}
        />
        {s.last_error && (
          <pre
            dir="ltr"
            className="m-0 whitespace-pre-wrap break-all rounded-[14px] bg-status-down-soft px-3.5 py-2.5 text-left font-mono text-xs text-status-down"
          >
            {s.last_error}
          </pre>
        )}
        <ErrorBox error={versions.error} />
        {shown.length > 0 && (
          <ul className="m-0 list-none overflow-hidden rounded-[16px] bg-chip p-0 [&>li+li]:border-t [&>li+li]:border-line">
            {shown.map((v, i) => {
              const prev = items[i + 1];
              const canDiff = isAdmin && !!prev;
              const body = (
                <>
                  <span className="grid min-w-0 flex-1">
                    <b className="font-medium">
                      {t('configBackup.versionNo', { n: v.version_no })}
                    </b>
                    <span className="truncate text-[13px] text-ink-muted">
                      {formatShortTime(v.collected_at)} · {t(`configBackup.trigger.${v.trigger}`)}
                    </span>
                  </span>
                  {aiDiff && prev && (
                    <VersionRisk deviceId={s.device_id} from={prev.id} to={v.id} />
                  )}
                  <span className="num shrink-0 text-sm font-medium text-ink-muted" dir="ltr">
                    {v.initial
                      ? t('configBackup.initial')
                      : v.added || v.removed
                        ? `+${v.added} −${v.removed}`
                        : '—'}
                  </span>
                  {canDiff && (
                    <ChevronIcon size={16} className="shrink-0 text-ink-subtle rtl:rotate-180" />
                  )}
                </>
              );
              const cls = cx(
                'flex w-full items-center gap-3 px-4 py-3 text-start',
                canDiff && 'hover:bg-surface-hover',
                diffTo === v.id && 'bg-surface',
              );
              return (
                <li key={v.id}>
                  {canDiff ? (
                    <button
                      type="button"
                      className={cls}
                      onClick={() => onDiff(prev.id, v.id, items)}
                      aria-pressed={diffTo === v.id}
                      data-testid="backup-version"
                    >
                      {body}
                    </button>
                  ) : (
                    <div className={cls}>{body}</div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
        {versions.data && !items.length && (
          <p className="m-0 text-sm text-ink-muted">{t('configBackup.noVersions')}</p>
        )}
        <p className="m-0 text-[13px] text-ink-muted">
          {items.some((v) => v.masked) ? t('configBackup.maskedNote') : ''}
          {!isAdmin && items.length > 0 && ` ${t('configBackup.adminOnly')}`}
        </p>
      </div>
    </Card>
  );
}

/** [3] Configuration backups (/config-backups). */
export function ConfigBackupsPage() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const { features } = useFeatures();
  const qc = useQueryClient();
  const toast = useToast();
  const line = useBackupLine();
  const [params, setParams] = useSearchParams();
  const [group, setGroup] = useState<number | null>(null);
  const [result, setResult] = useState('');
  const [text, setText] = useState('');
  const [page, setPage] = useState(1);
  const [diff, setDiff] = useState<{
    device: number;
    from: number;
    to: number;
    versions: ConfigVersion[];
  } | null>(null);
  const on = features.config_backup;
  const filter = {
    group_id: group ?? undefined,
    result: result || undefined,
    q: text || undefined,
    page,
  };
  const list = useQuery({
    queryKey: qk3.backups(filter),
    queryFn: () => endpoints3.backups(filter),
    enabled: on,
    placeholderData: keepPreviousData,
  });
  const runner = useQuery({
    queryKey: qk3.backupRunner,
    queryFn: endpoints3.backupRunner,
    enabled: on,
    refetchInterval: 15_000,
  });
  useLiveMessages(['config_backup.update', 'config_backup.changed'], () => {
    void qc.invalidateQueries({ queryKey: ['config-backups'] });
  });
  const runAll = useMutation({
    mutationFn: () => endpoints3.backupAll(),
    onSuccess: (r) => {
      toast.push('success', t('configBackup.queuedN', { n: r.queued }));
      void qc.invalidateQueries({ queryKey: qk3.backupRunner });
    },
  });
  const r = runner.data;
  // devices that can be backed up first (prototype: the list is the backed-up devices), by name
  const items = useMemo(
    () => [...(list.data?.items ?? [])].sort((a, b) => Number(b.eligible) - Number(a.eligible)),
    [list.data],
  );
  // the selected device (?device=): else the one changed last, else the first
  const pd = Number(params.get('device'));
  const lastChanged = items
    .filter((x) => x.last_change_at)
    .sort((a, b) => (a.last_change_at! < b.last_change_at! ? 1 : -1))[0];
  const sel = items.find((x) => x.device_id === pd) ?? lastChanged ?? items[0] ?? null;
  const select = (id: number) => {
    setParams({ device: String(id) }, { replace: true });
    setDiff(null);
  };
  const shownDiff = diff && sel && diff.device === sel.device_id ? diff : null;
  return (
    <div className="space-y-5">
      <PageHeader
        title={t('configBackup.title')}
        subtitle={
          r &&
          t('configBackup.runnerLine', {
            running: r.running,
            queued: r.queued,
            concurrency: r.concurrency,
            next: r.next_schedule_at ? formatDateTime(r.next_schedule_at) : '—',
            n: r.eligible_devices,
          })
        }
        actions={
          on &&
          isAdmin && (
            <>
              <Button variant="primary" busy={runAll.isPending} onClick={() => runAll.mutate()}>
                {t('configBackup.backupAll')}
              </Button>
              <a href="#config-search" className={buttonClass()}>
                {t('configBackup.search')}
              </a>
              <a
                className={buttonClass()}
                href={endpoints3.exportZipUrl(group ?? undefined)}
                download
              >
                {t('configBackup.exportZip')}
              </a>
            </>
          )
        }
      />
      {!on && <FeatureOffNotice feature="config_backup" />}
      <ErrorBox error={runAll.error} />
      {r && r.driver_errors.length > 0 && (
        <Banner tone="down" title={t('driver.errors')}>
          {r.driver_errors.map((d) => (
            <bdi key={d.file} dir="ltr" className="block font-mono text-xs">
              {d.file}: {d.error}
            </bdi>
          ))}
        </Banner>
      )}
      {on && (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <div className="w-56">
              <GroupSelect
                value={group}
                onChange={(g) => {
                  setGroup(g);
                  setPage(1);
                }}
                allowRoot
                rootLabel={t('configBackup.allGroups')}
              />
            </div>
            <Select
              value={result}
              onChange={(e) => {
                setResult(e.target.value);
                setPage(1);
              }}
              className="w-44"
              aria-label={t('configBackup.lastResult')}
            >
              <option value="">{t('configBackup.anyResult')}</option>
              {BACKUP_RESULTS.map((x) => (
                <option key={x} value={x}>
                  {t(`configBackup.result.${x}`)}
                </option>
              ))}
            </Select>
            <Input
              value={text}
              onChange={(e) => {
                setText(e.target.value);
                setPage(1);
              }}
              placeholder={t('configBackup.filterDevice')}
              aria-label={t('configBackup.filterDevice')}
              className="w-56"
            />
          </div>
          {list.isLoading && <Loading />}
          <ErrorBox error={list.error} />
          {list.data && !items.length && (
            <Card>
              <Empty>{t('configBackup.empty')}</Empty>
            </Card>
          )}
          {items.length > 0 && (
            // prototype .two: the devices at the start, the selected device at the end
            <div className="grid grid-cols-1 items-start gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
              <div className="grid gap-3">
                <GroupList label={t('configBackup.devices')}>
                  {items.map((x) => (
                    <ListRow
                      key={x.device_id}
                      testId="backup-row"
                      dot={backupDot(x)}
                      title={<bdi dir="auto">{x.device_name}</bdi>}
                      subtitle={line(x)}
                      value={t('configBackup.versionsN', { count: x.versions })}
                      onClick={() => select(x.device_id)}
                      className={cx(sel?.device_id === x.device_id && '[&>button]:bg-chip')}
                    />
                  ))}
                </GroupList>
                <Pagination
                  page={page}
                  pageSize={list.data?.page_size ?? 100}
                  total={list.data?.total ?? 0}
                  onPage={setPage}
                />
              </div>
              {sel && (
                <DeviceCard
                  key={sel.device_id}
                  s={sel}
                  diffTo={shownDiff?.to ?? null}
                  onDiff={(from, to, versions) =>
                    setDiff({ device: sel.device_id, from, to, versions })
                  }
                />
              )}
            </div>
          )}
          {isAdmin && shownDiff && sel && (
            <section aria-labelledby="backup-diff-title" className="grid gap-3">
              <GroupTitle id="backup-diff-title">
                <bdi dir="auto">{sel.device_name}</bdi> · {t('configBackup.diff')}
              </GroupTitle>
              <DiffView
                deviceId={sel.device_id}
                versions={shownDiff.versions}
                initialFrom={shownDiff.from}
                initialTo={shownDiff.to}
              />
            </section>
          )}
        </>
      )}
      {on && isAdmin && <SearchCard />}
    </div>
  );
}
