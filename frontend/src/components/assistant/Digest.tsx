import { useState } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { endpoints3b, qk3b } from '../../api/endpoints3b';
import type { Digest, DigestRow, DigestStatus } from '../../api/types';
import { useLiveMessages } from '../../hooks/live';
import { nmRoute } from '../../lib/assistant';
import { formatDateTime } from '../../lib/format';
import { Pagination } from '../Pagination';
import { Button, Card, Checkbox, cx, Empty, ErrorBox, Loading, Table, td, th } from '../ui';
import { Markdown } from './AnswerView';
import { AiNotice } from './common';

const PAGE = 30;

const STATUS_CLS: Record<DigestStatus, string> = {
  ok: 'text-green-700 dark:text-green-400',
  failed: 'text-red-600',
  skipped: 'text-slate-500',
  running: 'text-sky-600',
};

/** The last 7 local days (YYYY-MM-DD), yesterday first: the days a manual run may target. */
export function lastDays(n = 7, now = new Date()): string[] {
  const out: string[] = [];
  for (let i = 1; i <= n; i++) {
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - i);
    out.push(
      `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`,
    );
  }
  return out;
}

function RunNow({ day: initial }: { day?: string }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const days = lastDays();
  const [day, setDay] = useState(initial && days.includes(initial) ? initial : days[0]);
  const [send, setSend] = useState(false);
  const run = useMutation({
    mutationFn: () => endpoints3b.runDigest(day, send),
    onSuccess: (d) => {
      qc.setQueryData(qk3b.digest(d.day), d);
      void qc.invalidateQueries({ queryKey: ['assistant-digests'] });
    },
  });
  return (
    <div className="flex flex-wrap items-center gap-2">
      {!initial && (
        <select
          value={day}
          onChange={(e) => setDay(e.target.value)}
          className="rounded-md border-0 bg-white py-1 text-sm ring-1 ring-inset ring-slate-300 dark:bg-slate-900 dark:ring-slate-700"
          aria-label={t('digest.day')}
          dir="ltr"
        >
          {days.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
      )}
      <Checkbox label={t('digest.sendToo')} checked={send} onChange={setSend} />
      <Button size="sm" onClick={() => run.mutate()} busy={run.isPending}>
        {t('digest.runNow')}
      </Button>
      <ErrorBox error={run.error} />
    </div>
  );
}

/** Logs → Digest tab and /assistant/digests: list of daily digests. */
export function DigestList() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [page, setPage] = useState(1);
  const q = useQuery({
    queryKey: qk3b.digests({ page }),
    queryFn: () => endpoints3b.digests({ page, page_size: PAGE }),
    placeholderData: keepPreviousData,
  });
  useLiveMessages(['digest.ready'], () => {
    void qc.invalidateQueries({ queryKey: ['assistant-digests'] });
  });
  return (
    <Card title={t('digest.title')} actions={<RunNow />} bodyClassName="p-0">
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} className="m-3" />
      {q.data && !q.data.items.length && <Empty>{t('digest.none')}</Empty>}
      {q.data && q.data.items.length > 0 && (
        <>
          <Table>
            <thead>
              <tr>
                <th className={th}>{t('digest.day')}</th>
                <th className={th}>{t('digest.status')}</th>
                <th className={th}>{t('digest.unusual')}</th>
                <th className={th}>{t('digest.newTypes')}</th>
                <th className={th}>{t('digest.total')}</th>
              </tr>
            </thead>
            <tbody>
              {q.data.items.map((d) => (
                <tr key={d.day} data-testid="digest-row">
                  <td className={td}>
                    <Link
                      to={`/assistant/digests/${d.day}`}
                      className="font-medium text-sky-700 hover:underline dark:text-sky-400"
                      dir="ltr"
                    >
                      {d.day}
                    </Link>
                  </td>
                  <td className={cx(td, STATUS_CLS[d.status])}>{t(`digest.st.${d.status}`)}</td>
                  <td className={`${td} tabular-nums`}>{d.unusual_count}</td>
                  <td className={`${td} tabular-nums`}>{d.new_types}</td>
                  <td className={`${td} tabular-nums`}>{d.total_messages}</td>
                </tr>
              ))}
            </tbody>
          </Table>
          <Pagination
            page={q.data.page}
            pageSize={q.data.page_size}
            total={q.data.total}
            onPage={setPage}
          />
        </>
      )}
    </Card>
  );
}

function KeyCell({ row }: { row: DigestRow }) {
  const route = row.ref ? nmRoute(row.ref) : null;
  return route ? (
    <Link to={route} className="text-sky-700 hover:underline dark:text-sky-400" dir="auto">
      {row.key}
    </Link>
  ) : (
    <span dir="auto">{row.key}</span>
  );
}

function StatTable({ title, rows }: { title: string; rows: DigestRow[] }) {
  const { t } = useTranslation();
  if (!rows.length) return null;
  return (
    <Card title={title} bodyClassName="p-0">
      <Table>
        <thead>
          <tr>
            <th className={th}>{t('digest.key')}</th>
            <th className={th}>{t('digest.count')}</th>
            <th className={th}>{t('digest.baseline')}</th>
            <th className={th}>{t('digest.ratio')}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className={r.unusual ? 'bg-amber-50 dark:bg-amber-950/30' : ''}>
              <td className={td}>
                <KeyCell row={r} />
                {r.unusual && (
                  <span className="ms-1 rounded bg-amber-200 px-1 text-[10px] text-amber-900">
                    {t('digest.unusualBadge')}
                  </span>
                )}
              </td>
              <td className={`${td} tabular-nums`}>{r.count}</td>
              <td className={`${td} tabular-nums`}>
                {r.baseline === null ? '—' : r.baseline.toFixed(1)}
              </td>
              <td className={`${td} tabular-nums`}>
                {r.ratio === null ? '—' : `×${r.ratio.toFixed(1)}`}
              </td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

/** One digest: AI bullets (labelled) + the deterministic statistics (always). */
export function DigestView({ d }: { d: Digest }) {
  const { t } = useTranslation();
  const maxHour = Math.max(1, ...d.stats.by_hour.map(([, n]) => n));
  return (
    <div className="space-y-4" data-testid="digest">
      <Card title={t('digest.summary')} actions={<RunNow day={d.day} />}>
        <div className="space-y-2">
          <p className={cx('text-xs', STATUS_CLS[d.status])}>
            {t(`digest.st.${d.status}`)} · {formatDateTime(d.generated_at)} ·{' '}
            {t('digest.totals', {
              total: d.total_messages,
              unusual: d.unusual_count,
              newTypes: d.new_types,
            })}
          </p>
          {d.baseline_days < 3 && <p className="text-xs text-amber-700">{t('digest.learning')}</p>}
          {d.stats.sampled && <p className="text-xs text-amber-700">{t('digest.sampled')}</p>}
          {d.ai_markdown ? (
            <>
              <Markdown text={d.ai_markdown} />
              <AiNotice />
            </>
          ) : (
            <p className="text-sm text-slate-500">{t('digest.aiUnavailable')}</p>
          )}
        </div>
      </Card>
      {d.stats.new_types.length > 0 && (
        <Card title={t('digest.newTypesTitle')} bodyClassName="p-0">
          <Table>
            <thead>
              <tr>
                <th className={th}>{t('digest.example')}</th>
                <th className={th}>{t('digest.count')}</th>
              </tr>
            </thead>
            <tbody>
              {d.stats.new_types.map((n) => {
                const route = nmRoute(n.example_ref);
                return (
                  <tr key={n.signature}>
                    <td className={`${td} font-mono text-xs`} dir="ltr">
                      {route ? (
                        <Link to={route} className="text-sky-700 hover:underline dark:text-sky-400">
                          {n.example || n.sample}
                        </Link>
                      ) : (
                        n.example || n.sample
                      )}
                    </td>
                    <td className={`${td} tabular-nums`}>{n.count}</td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </Card>
      )}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <StatTable title={t('digest.bySeverity')} rows={d.stats.by_severity} />
        <StatTable title={t('digest.byProgram')} rows={d.stats.by_program} />
        <StatTable title={t('digest.byDevice')} rows={d.stats.by_device} />
      </div>
      {d.stats.by_hour.length > 0 && (
        <Card title={t('digest.byHour')}>
          <div className="flex h-24 items-end gap-0.5" dir="ltr" aria-hidden>
            {d.stats.by_hour.map(([h, n]) => (
              <div
                key={h}
                className="flex-1 rounded-t bg-sky-500/70"
                style={{ height: `${(n / maxHour) * 100}%` }}
                title={`${String(h).padStart(2, '0')}:00 — ${n}`}
              />
            ))}
          </div>
        </Card>
      )}
      {d.deliveries.length > 0 && (
        <Card title={t('digest.deliveries')}>
          <ul className="space-y-0.5 text-xs">
            {d.deliveries.map((x, i) => (
              <li key={i} className={x.ok ? 'text-green-700 dark:text-green-400' : 'text-red-600'}>
                <bdi dir="ltr">{x.target}</bdi> · {formatDateTime(x.at)}
                {x.error && ` · ${x.error}`}
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
