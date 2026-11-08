import { useState, type FormEvent } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { endpoints3, qk3, type ConfigSearchQuery } from '../api/endpoints3';
import { BACKUP_RESULTS } from '../api/types3';
import { ResultBadge } from '../components/configbackup/ConfigTab';
import { GroupSelect } from '../components/EntityForms';
import { Pagination } from '../components/Pagination';
import { useToast } from '../components/toast';
import {
  Button,
  buttonClass,
  Card,
  Checkbox,
  Empty,
  ErrorBox,
  Input,
  Loading,
  PageHeader,
  Select,
  Table,
  td,
  th,
} from '../components/ui';
import { FeatureOffNotice, useFeatures } from '../hooks/features';
import { useAuth } from '../hooks/auth';
import { useLiveMessages } from '../hooks/live';
import { highlight, hostKeyChanged, searchError } from '../lib/configBackup';
import { formatDateTime, formatRelative } from '../lib/format';

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
    <Card title={t('configBackup.search')}>
      <form onSubmit={submit} className="flex flex-wrap items-end gap-3" role="search">
        <label className="min-w-[16rem] flex-1">
          <span className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-400">
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
      {err && <p className="mt-1 text-xs text-red-600">{t(err)}</p>}
      <ErrorBox error={q.error} className="mt-3" />
      {q.data && (
        <div className="mt-3 space-y-3" data-testid="config-search-results">
          {!q.data.items.length && <Empty>{t('configBackup.noMatches')}</Empty>}
          {q.data.truncated && (
            <p className="text-xs text-amber-700 dark:text-amber-300">
              {t('configBackup.searchTruncated')}
            </p>
          )}
          {q.data.items.map((it) => (
            <div
              key={`${it.device_id}-${it.version_id}`}
              className="rounded-md ring-1 ring-slate-200 dark:ring-slate-800"
            >
              <div className="flex flex-wrap items-baseline gap-2 border-b border-slate-200 px-3 py-1.5 text-sm dark:border-slate-800">
                <Link
                  to={`/devices/${it.device_id}?tab=config`}
                  className="font-medium text-sky-700 hover:underline dark:text-sky-400"
                >
                  {it.device_name}
                </Link>
                <span className="text-xs text-slate-500">{formatDateTime(it.collected_at)}</span>
              </div>
              <table dir="ltr" className="w-full font-mono text-xs">
                <tbody>
                  {it.matches.map((m) => (
                    <tr key={m.line}>
                      <td className="w-12 select-none pe-2 text-right align-top text-slate-400">
                        {m.line}
                      </td>
                      <td className="whitespace-pre-wrap break-all text-left">
                        {highlight(m.text, query?.q ?? '', !!query?.regex).map((sg, i) =>
                          sg.hit ? (
                            <mark
                              key={i}
                              className="rounded-sm bg-amber-200 text-inherit dark:bg-amber-700/60"
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
                      <td className="text-left text-slate-500">…</td>
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

/** [3] Configuration backups (/config-backups). */
export function ConfigBackupsPage() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const { features } = useFeatures();
  const qc = useQueryClient();
  const toast = useToast();
  const [group, setGroup] = useState<number | null>(null);
  const [result, setResult] = useState('');
  const [text, setText] = useState('');
  const [page, setPage] = useState(1);
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
  return (
    <div className="space-y-4">
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
              <a
                className={buttonClass()}
                href={endpoints3.exportZipUrl(group ?? undefined)}
                download
              >
                {t('configBackup.exportZip')}
              </a>
              <Button variant="primary" busy={runAll.isPending} onClick={() => runAll.mutate()}>
                {t('configBackup.backupAll')}
              </Button>
            </>
          )
        }
      />
      {!on && <FeatureOffNotice feature="config_backup" />}
      <ErrorBox error={runAll.error} />
      {r && r.driver_errors.length > 0 && (
        <div
          role="alert"
          className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-800 ring-1 ring-inset ring-red-200 dark:bg-red-950/40 dark:text-red-200 dark:ring-red-900"
        >
          <p className="font-medium">{t('driver.errors')}</p>
          <ul className="mt-1 text-xs">
            {r.driver_errors.map((d) => (
              <li key={d.file}>
                <bdi dir="ltr" className="font-mono">
                  {d.file}: {d.error}
                </bdi>
              </li>
            ))}
          </ul>
        </div>
      )}
      {on && (
        <Card bodyClassName="p-0">
          <div className="flex flex-wrap items-end gap-3 border-b border-slate-200 p-3 dark:border-slate-800">
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
              className="w-56"
            />
          </div>
          {list.isLoading && <Loading />}
          <ErrorBox error={list.error} className="m-3" />
          {list.data && !list.data.items.length && <Empty>{t('configBackup.empty')}</Empty>}
          {list.data && list.data.items.length > 0 && (
            <>
              <Table>
                <thead>
                  <tr>
                    <th className={th}>{t('common.device')}</th>
                    <th className={th}>{t('driver.label')}</th>
                    <th className={th}>{t('configBackup.lastAttempt')}</th>
                    <th className={th}>{t('configBackup.lastResult')}</th>
                    <th className={th}>{t('configBackup.lastChange')}</th>
                    <th className={th}>{t('configBackup.versions')}</th>
                    <th className={th}>{t('configBackup.nextDue')}</th>
                  </tr>
                </thead>
                <tbody>
                  {list.data.items.map((s) => (
                    <tr key={s.device_id}>
                      <td className={td}>
                        <Link
                          to={`/devices/${s.device_id}?tab=config`}
                          className="font-medium text-sky-700 hover:underline dark:text-sky-400"
                        >
                          {s.device_name}
                        </Link>
                        {hostKeyChanged(s) && (
                          <span className="ms-2 rounded bg-red-100 px-1.5 py-0.5 text-xs text-red-800 dark:bg-red-900/40 dark:text-red-200">
                            {t('hostKey.changedShort')}
                          </span>
                        )}
                        {!s.eligible && (
                          <div className="text-xs text-slate-500">
                            {t(`configBackup.reason.${s.reason}`)}
                          </div>
                        )}
                      </td>
                      <td className={td}>{s.driver ? <bdi dir="ltr">{s.driver}</bdi> : '—'}</td>
                      <td className={td}>
                        {s.last_attempt_at ? formatRelative(s.last_attempt_at) : '—'}
                      </td>
                      <td className={td}>
                        <ResultBadge s={s} />
                        {s.last_error && (
                          <div
                            className="max-w-xs truncate text-xs text-red-600"
                            dir="ltr"
                            title={s.last_error}
                          >
                            {s.last_error}
                          </div>
                        )}
                      </td>
                      <td className={td}>{formatDateTime(s.last_change_at)}</td>
                      <td className={`${td} tabular-nums`}>{s.versions}</td>
                      <td className={td}>{formatDateTime(s.next_due_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </Table>
              <Pagination
                page={page}
                pageSize={list.data.page_size}
                total={list.data.total}
                onPage={setPage}
              />
            </>
          )}
        </Card>
      )}
      {on && isAdmin && <SearchCard />}
    </div>
  );
}
