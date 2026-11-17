import { useState } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { endpoints3b, qk3b } from '../../api/endpoints3b';
import type { Digest, DigestRow, DigestStatus } from '../../api/types';
import { useLiveMessages } from '../../hooks/live';
import { nmRoute } from '../../lib/assistant';
import { formatDateTime } from '../../lib/format';
import { Pagination } from '../Pagination';
import {
  Button,
  Card,
  Checkbox,
  cx,
  Empty,
  ErrorBox,
  Loading,
  Select,
  Table,
  TAG,
  TAG_TONE,
  td,
  th,
} from '../ui';
import { ChevronIcon, LogsIcon } from '../icons';
import { AiTag, Banner, EmptyState } from '../kit';
import { Markdown } from './AnswerView';
import { AiNotice } from './common';

const PAGE = 30;

const STATUS_CLS: Record<DigestStatus, string> = {
  ok: 'text-status-up',
  failed: 'text-status-down',
  skipped: 'text-ink-muted',
  running: 'text-accent-ink',
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
        <Select
          value={day}
          onChange={(e) => setDay(e.target.value)}
          className="w-auto font-mono"
          aria-label={t('digest.day')}
          dir="ltr"
        >
          {days.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </Select>
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
                      className="font-medium text-accent-ink hover:underline"
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
    <Link to={route} className="font-medium text-ink hover:underline" dir="auto">
      {row.key}
    </Link>
  ) : (
    <span dir="auto" className="font-medium">
      {row.key}
    </span>
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
            <th className={cx(th, 'text-end')}>{t('digest.count')}</th>
            <th className={cx(th, 'text-end')}>{t('digest.baseline')}</th>
            <th className={cx(th, 'text-end')}>{t('digest.ratio')}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key}>
              <td className={td}>
                <KeyCell row={r} />
                {r.unusual && (
                  <span className={cx(TAG, TAG_TONE.warning, 'ms-2')}>
                    {t('digest.unusualBadge')}
                  </span>
                )}
              </td>
              <td className={cx(td, 'num text-end')}>{r.count}</td>
              <td className={cx(td, 'num text-end text-ink-muted')}>
                {r.baseline === null ? '—' : r.baseline.toFixed(1)}
              </td>
              <td className={cx(td, 'num text-end')}>
                {r.ratio === null ? '—' : `×${r.ratio.toFixed(1)}`}
              </td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

/** A stat tile (prototype .tiles .tile): label, a big number, a small line under it. */
function Tile({
  label,
  value,
  sub,
  warn,
}: {
  label: string;
  value: number;
  sub?: string;
  warn?: boolean;
}) {
  return (
    <div className="grid gap-1 rounded-xl bg-surface px-5 py-4 shadow-card">
      <span className="text-[13px] text-ink-muted">{label}</span>
      <b
        className={cx(
          'num text-[30px] font-semibold leading-tight tracking-[-0.02em]',
          warn && 'text-status-warning',
        )}
      >
        {value.toLocaleString()}
      </b>
      {sub && <small className="text-[12.5px] text-ink-subtle">{sub}</small>}
    </div>
  );
}

/** One digest: AI bullets (labelled) + the deterministic statistics (always). */
export function DigestView({ d }: { d: Digest }) {
  const { t } = useTranslation();
  const maxHour = Math.max(1, ...d.stats.by_hour.map(([, n]) => n));
  const baseRows = d.stats.by_severity.filter((r) => r.baseline !== null);
  const baseline = baseRows.length
    ? Math.round(baseRows.reduce((a, r) => a + (r.baseline ?? 0), 0))
    : null;
  // prototype "חריגים": every unusual key, the largest ratio first
  const unusual = [...d.stats.by_device, ...d.stats.by_program, ...d.stats.by_severity]
    .filter((r) => r.unusual)
    .sort((a, b) => (b.ratio ?? 0) - (a.ratio ?? 0));
  return (
    <div className="space-y-4" data-testid="digest">
      <p className="m-0 mx-1 text-ink-muted">
        {t('digest.totals', {
          total: d.total_messages.toLocaleString(),
          unusual: d.unusual_count,
          newTypes: d.new_types,
        })}
        <span className="text-ink-subtle"> · {formatDateTime(d.generated_at)}</span>
      </p>
      {d.baseline_days < 3 && <Banner title={t('digest.learning')} />}
      {d.stats.sampled && <Banner tone="warn" title={t('digest.sampled')} />}
      {d.ai_markdown ? (
        <Card
          title={
            <span className="inline-flex items-center gap-2.5">
              {t('digest.summary')}
              <AiTag />
            </span>
          }
        >
          <div className="grid gap-3 leading-relaxed">
            <Markdown text={d.ai_markdown} />
            <AiNotice />
          </div>
        </Card>
      ) : (
        <Banner
          tone={d.status === 'failed' ? 'down' : 'neutral'}
          title={t('digest.aiUnavailable')}
        />
      )}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Tile
          label={t('digest.total')}
          value={d.total_messages}
          sub={
            baseline !== null ? t('digest.baselineN', { n: baseline.toLocaleString() }) : undefined
          }
        />
        <Tile label={t('digest.unusual')} value={d.unusual_count} warn={d.unusual_count > 0} />
        <Tile label={t('digest.newTypes')} value={d.new_types} />
        <Tile label={t('digest.devices')} value={d.stats.by_device.length} />
      </div>
      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-2">
        {d.stats.new_types.length > 0 && (
          <Card title={t('digest.newTypesTitle')} bodyClassName="p-0">
            <Table>
              <thead>
                <tr>
                  <th className={th}>{t('digest.example')}</th>
                  <th className={cx(th, 'text-end')}>{t('digest.count')}</th>
                </tr>
              </thead>
              <tbody>
                {d.stats.new_types.map((n) => {
                  const route = nmRoute(n.example_ref);
                  return (
                    <tr key={n.signature}>
                      <td className={td}>
                        <div
                          dir="ltr"
                          className="max-w-[460px] truncate text-left font-mono text-[12.5px] text-ink-muted rtl:text-right"
                          title={n.example || n.sample}
                        >
                          {route ? (
                            <Link to={route} className="text-ink hover:underline">
                              {n.example || n.sample}
                            </Link>
                          ) : (
                            n.example || n.sample
                          )}
                        </div>
                      </td>
                      <td className={cx(td, 'num text-end')}>{n.count}</td>
                    </tr>
                  );
                })}
              </tbody>
            </Table>
          </Card>
        )}
        {unusual.length > 0 && <StatTable title={t('digest.unusualTitle')} rows={unusual} />}
      </div>
      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-3">
        <StatTable title={t('digest.bySeverity')} rows={d.stats.by_severity} />
        <StatTable title={t('digest.byProgram')} rows={d.stats.by_program} />
        <StatTable title={t('digest.byDevice')} rows={d.stats.by_device} />
      </div>
      {d.stats.by_hour.length > 0 && (
        <Card title={t('digest.byHour')}>
          {/* flat bars (prototype .bars), time left → right */}
          <div className="flex h-24 items-end gap-[3px]" dir="ltr" aria-hidden>
            {d.stats.by_hour.map(([h, n]) => (
              <div
                key={h}
                className="min-h-[3px] flex-1 rounded-t-[4px] rounded-b-[2px] bg-ink/20"
                style={{ height: `${(n / maxHour) * 100}%` }}
                title={`${String(h).padStart(2, '0')}:00 — ${n}`}
              />
            ))}
          </div>
        </Card>
      )}
      {d.deliveries.length > 0 && (
        <Card title={t('digest.deliveries')}>
          <ul className="m-0 grid list-none gap-1 p-0 text-[13.5px]">
            {d.deliveries.map((x, i) => (
              <li key={i} className="flex items-center gap-2">
                <span
                  aria-hidden="true"
                  className={cx('h-2 w-2 rounded-full', x.ok ? 'bg-status-up' : 'bg-status-down')}
                />
                <bdi dir="ltr">{x.target}</bdi>
                <span className="text-ink-muted">· {formatDateTime(x.at)}</span>
                {x.error && <span className="text-status-down">· {x.error}</span>}
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}

/** "Tuesday, 15 November" for a YYYY-MM-DD day (local date, no time zone shift). */
function dayLabel(day: string, lang: string): string {
  const [y, m, dd] = day.split('-').map(Number);
  return new Intl.DateTimeFormat(lang === 'he' ? 'he-IL' : 'en-GB', {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
  }).format(new Date(y, m - 1, dd));
}

const DAY_TONE: Record<DigestStatus, string> = {
  ok: TAG_TONE.up,
  failed: TAG_TONE.down,
  skipped: TAG_TONE.neutral,
  running: TAG_TONE.accent,
};

/**
 * Logs → Digest tab (prototype "לוגים → תקציר"): one day at a time with ‹ › to the previous /
 * next digest, its status, Run now; the whole list lives at /assistant/digests.
 */
export function DigestDay() {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const [idx, setIdx] = useState(0);
  const list = useQuery({
    queryKey: qk3b.digests({ page: 1 }),
    queryFn: () => endpoints3b.digests({ page: 1, page_size: PAGE }),
  });
  const items = list.data?.items ?? [];
  const day = items[Math.min(idx, items.length - 1)]?.day;
  const q = useQuery({
    queryKey: qk3b.digest(day ?? ''),
    queryFn: () => endpoints3b.digest(day!),
    enabled: !!day,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 3000 : false),
  });
  useLiveMessages(['digest.ready'], () => {
    void qc.invalidateQueries({ queryKey: ['assistant-digests'] });
    if (day) void qc.invalidateQueries({ queryKey: qk3b.digest(day) });
  });
  if (list.isLoading) return <Loading />;
  if (list.error) return <ErrorBox error={list.error} />;
  if (!day)
    return (
      <Card>
        <EmptyState icon={LogsIcon} title={t('digest.none')} actions={<RunNow />}>
          {t('digest.noneHint')}
        </EmptyState>
      </Card>
    );
  const st = q.data?.status ?? items[idx]?.status;
  const round =
    'grid h-9 w-9 place-items-center rounded-full bg-chip text-ink transition-colors hover:bg-surface-hover disabled:opacity-40';
  return (
    <div className="space-y-4" data-testid="digest-day">
      <div className="flex flex-wrap items-center gap-3">
        <span className="inline-flex items-center gap-2">
          <button
            type="button"
            className={round}
            aria-label={t('digest.prevDay')}
            disabled={idx >= items.length - 1}
            onClick={() => setIdx((i) => i + 1)}
          >
            <ChevronIcon size={16} className="rotate-180 rtl:rotate-0" />
          </button>
          <b className="text-[17px] font-semibold">{dayLabel(day, i18n.language)}</b>
          <button
            type="button"
            className={round}
            aria-label={t('digest.nextDay')}
            disabled={idx === 0}
            onClick={() => setIdx((i) => i - 1)}
          >
            <ChevronIcon size={16} className="rtl:rotate-180" />
          </button>
        </span>
        {st && <span className={cx(TAG, DAY_TONE[st])}>{t(`digest.st.${st}`)}</span>}
        <span className="flex-1" />
        <RunNow day={day} />
        <Link to="/assistant/digests" className="text-sm text-ink-muted hover:text-ink">
          {t('digest.all')}
        </Link>
      </div>
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} />
      {q.data && <DigestView d={q.data} />}
    </div>
  );
}
