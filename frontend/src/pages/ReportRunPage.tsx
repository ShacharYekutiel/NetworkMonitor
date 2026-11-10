import { useMemo } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { endpoints3, qk3 } from '../api/endpoints3';
import type { FlowTopItem, ReportData, ReportSection } from '../api/types3';
import { buttonClass, Button, ErrorBox, Loading, Notice } from '../components/ui';
import { useFeatures, FeatureOffNotice } from '../hooks/features';
import { useLiveMessages } from '../hooks/live';
import { formatDuration } from '../lib/format';
import { formatBytes, formatShare } from '../lib/flows';
import { availabilityTone, barPct, formatAvailability } from '../lib/reports';
import { DeliveriesCell, RunStatusBadge } from './ReportsPage';

const pth =
  'border-b border-slate-300 px-2 py-1 text-start text-xs font-semibold text-slate-600 print:border-slate-400';
const ptd = 'border-b border-slate-100 px-2 py-1 align-middle print:border-slate-300';

function Bar({ pct, tone = 'sky' }: { pct: number; tone?: 'sky' | 'green' | 'amber' | 'red' }) {
  const color = {
    sky: 'bg-sky-500',
    green: 'bg-green-500',
    amber: 'bg-amber-500',
    red: 'bg-red-500',
  }[tone];
  return (
    <div className="h-2 w-28 overflow-hidden rounded bg-slate-200 print:border print:border-slate-300">
      <div
        className={`h-full ${color}`}
        style={{ width: `${pct}%`, printColorAdjust: 'exact', WebkitPrintColorAdjust: 'exact' }}
      />
    </div>
  );
}

function Avail({ v, tr }: { v: number | null; tr: TFunction }) {
  const s = formatAvailability(v);
  const tone = availabilityTone(v);
  return (
    <span
      className={
        tone === 'ok'
          ? 'text-green-700'
          : tone === 'warn'
            ? 'text-amber-700'
            : tone === 'bad'
              ? 'font-semibold text-red-700'
              : 'text-slate-500'
      }
    >
      <bdi dir="ltr">{s ?? tr('reports.na')}</bdi>
    </span>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="break-inside-avoid-page space-y-2">
      <h2 className="border-b-2 border-slate-800 pb-1 text-lg font-semibold dark:border-slate-300 print:border-slate-800">
        {title}
      </h2>
      {children}
    </section>
  );
}

function TopItems({ items, tr }: { items: FlowTopItem[]; tr: TFunction }) {
  const max = Math.max(1, ...items.map((x) => x.bytes));
  return (
    <table className="w-full text-sm">
      <thead>
        <tr>
          <th className={pth}>{tr('reports.col.item')}</th>
          <th className={pth}>{tr('flows.bytes')}</th>
          <th className={pth}>{tr('flows.share')}</th>
        </tr>
      </thead>
      <tbody>
        {items.map((x) => (
          <tr key={x.key}>
            <td className={ptd}>
              <bdi dir="ltr" className="font-mono text-xs">
                {x.label}
              </bdi>
            </td>
            <td className={ptd}>
              <div className="flex items-center gap-2">
                <Bar pct={barPct(x.bytes, max)} />
                <bdi dir="ltr" className="tabular-nums">
                  {formatBytes(x.bytes)}
                </bdi>
              </div>
            </td>
            <td className={`${ptd} tabular-nums`}>{formatShare(x.share)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ReportSectionView({
  s,
  d,
  tr,
  fmt,
}: {
  s: ReportSection;
  d: ReportData;
  tr: TFunction;
  fmt: (iso: string) => string;
}) {
  const title = tr(`reportSection.${s}`);
  const note = d.notes.find((n) => n.section === s);
  if (note)
    return (
      <Section title={title}>
        <p className="text-sm text-slate-500">{tr(`reports.note.${note.note}`)}</p>
      </Section>
    );
  switch (s) {
    case 'availability': {
      const a = d.availability;
      if (!a) return null;
      return (
        <Section title={title}>
          <p className="text-sm">
            {tr('reports.overall')}:{' '}
            <span className="text-2xl font-bold">
              <Avail v={a.overall} tr={tr} />
            </span>
          </p>
          {a.groups.length > 0 && (
            <table className="w-full text-sm">
              <thead>
                <tr>
                  <th className={pth}>{tr('reports.col.group')}</th>
                  <th className={pth}>{tr('reports.col.devices')}</th>
                  <th className={pth}>{tr('reports.col.availability')}</th>
                </tr>
              </thead>
              <tbody>
                {a.groups.map((g) => (
                  <tr key={g.group_id}>
                    <td className={ptd}>{g.group_name}</td>
                    <td className={`${ptd} tabular-nums`}>{g.devices}</td>
                    <td className={ptd}>
                      <div className="flex items-center gap-2">
                        <Bar
                          pct={g.availability ?? 0}
                          tone={
                            availabilityTone(g.availability) === 'bad'
                              ? 'red'
                              : availabilityTone(g.availability) === 'warn'
                                ? 'amber'
                                : 'green'
                          }
                        />
                        <Avail v={g.availability} tr={tr} />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <table className="w-full text-sm">
            <thead>
              <tr>
                <th className={pth}>{tr('reports.col.device')}</th>
                <th className={pth}>{tr('reports.col.group')}</th>
                <th className={pth}>{tr('reports.col.availability')}</th>
                <th className={pth}>{tr('reports.col.downtime')}</th>
                <th className={pth}>{tr('reports.col.outages')}</th>
                <th className={pth}>{tr('reports.col.longest')}</th>
              </tr>
            </thead>
            <tbody>
              {a.devices.map((r) => (
                <tr key={r.device_id}>
                  <td className={ptd}>{r.device_name}</td>
                  <td className={ptd}>{r.group_name}</td>
                  <td className={ptd}>
                    <Avail v={r.availability} tr={tr} />
                  </td>
                  <td className={ptd}>{r.downtime_s ? formatDuration(r.downtime_s) : '—'}</td>
                  <td className={`${ptd} tabular-nums`}>{r.outages}</td>
                  <td className={ptd}>
                    {r.longest_outage_s ? formatDuration(r.longest_outage_s) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {a.not_measured.length > 0 && (
            <p className="text-xs text-slate-500">
              {tr('reports.notMeasured')}: {a.not_measured.map((x) => x.device_name).join(', ')}
            </p>
          )}
        </Section>
      );
    }
    case 'top_alerts': {
      const rows = d.top_alerts ?? [];
      const max = Math.max(1, ...rows.map((r) => r.notifications));
      return (
        <Section title={title}>
          <table className="w-full text-sm">
            <thead>
              <tr>
                <th className={pth}>{tr('reports.col.sensor')}</th>
                <th className={pth}>{tr('reports.col.notifications')}</th>
                <th className={pth}>{tr('reports.col.problemTime')}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.sensor_id}>
                  <td className={ptd}>
                    {r.device_name} · {r.sensor_name}
                  </td>
                  <td className={ptd}>
                    <div className="flex items-center gap-2">
                      <Bar pct={barPct(r.notifications, max)} tone="red" />
                      <span className="tabular-nums">{r.notifications}</span>
                    </div>
                  </td>
                  <td className={ptd}>{formatDuration(r.problem_s)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      );
    }
    case 'flapping_ports':
      return (
        <Section title={title}>
          <table className="w-full text-sm">
            <thead>
              <tr>
                <th className={pth}>{tr('reports.col.device')}</th>
                <th className={pth}>{tr('reports.col.port')}</th>
                <th className={pth}>{tr('reports.col.episodes')}</th>
                <th className={pth}>{tr('reports.col.transitions')}</th>
              </tr>
            </thead>
            <tbody>
              {(d.flapping_ports ?? []).map((r) => (
                <tr key={`${r.device_id}-${r.if_index}`}>
                  <td className={ptd}>{r.device_name}</td>
                  <td className={ptd}>
                    <bdi dir="ltr">{r.port_name}</bdi>
                  </td>
                  <td className={`${ptd} tabular-nums`}>{r.episodes}</td>
                  <td className={`${ptd} tabular-nums`}>{r.transitions}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      );
    case 'port_errors':
      return (
        <Section title={title}>
          <table className="w-full text-sm">
            <thead>
              <tr>
                <th className={pth}>{tr('reports.col.device')}</th>
                <th className={pth}>{tr('reports.col.port')}</th>
                <th className={pth}>{tr('reports.col.errors')}</th>
              </tr>
            </thead>
            <tbody>
              {(d.port_errors ?? []).map((r, i) => (
                <tr key={i}>
                  <td className={ptd}>{r.device_name}</td>
                  <td className={ptd}>
                    <bdi dir="ltr">{r.port_name}</bdi>
                  </td>
                  <td className={`${ptd} tabular-nums`}>{r.errors}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      );
    case 'config_changes':
      return (
        <Section title={title}>
          <table className="w-full text-sm">
            <thead>
              <tr>
                <th className={pth}>{tr('reports.col.device')}</th>
                <th className={pth}>{tr('reports.col.changes')}</th>
                <th className={pth}>{tr('reports.col.lines')}</th>
                <th className={pth}>{tr('reports.col.lastChange')}</th>
              </tr>
            </thead>
            <tbody>
              {(d.config_changes ?? []).map((r) => (
                <tr key={r.device_id}>
                  <td className={ptd}>{r.device_name}</td>
                  <td className={`${ptd} tabular-nums`}>{r.changes}</td>
                  <td className={`${ptd} tabular-nums`}>
                    <bdi dir="ltr">
                      +{r.added} −{r.removed}
                    </bdi>
                  </td>
                  <td className={ptd}>{fmt(r.last_change_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      );
    case 'new_devices':
      return (
        <Section title={title}>
          <table className="w-full text-sm">
            <thead>
              <tr>
                <th className={pth}>MAC</th>
                <th className={pth}>{tr('newDevices.vendor')}</th>
                <th className={pth}>{tr('newDevices.ipHost')}</th>
                <th className={pth}>{tr('newDevices.switchPort')}</th>
                <th className={pth}>{tr('reports.col.detected')}</th>
                <th className={pth}>{tr('reports.col.acknowledged')}</th>
              </tr>
            </thead>
            <tbody>
              {(d.new_devices ?? []).map((r) => (
                <tr key={r.mac}>
                  <td className={ptd}>
                    <bdi dir="ltr" className="font-mono text-xs">
                      {r.mac}
                    </bdi>
                  </td>
                  <td className={ptd}>{r.vendor ?? '—'}</td>
                  <td className={ptd}>
                    <bdi dir="ltr">{[r.ip, r.hostname].filter(Boolean).join(' · ') || '—'}</bdi>
                  </td>
                  <td className={ptd}>
                    {r.switch_name ?? '—'} <bdi dir="ltr">{r.port_name ?? ''}</bdi>
                  </td>
                  <td className={ptd}>{fmt(r.detected_at)}</td>
                  <td className={ptd}>{r.acknowledged ? tr('common.yes') : tr('common.no')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      );
    case 'top_talkers': {
      const tt = d.top_talkers;
      if (!tt) return null;
      return (
        <Section title={title}>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 print:grid-cols-2">
            <div>
              <h3 className="mb-1 text-sm font-semibold">{tr('flows.topTalkers')}</h3>
              <TopItems items={tt.hosts} tr={tr} />
            </div>
            <div>
              <h3 className="mb-1 text-sm font-semibold">{tr('flows.topApps')}</h3>
              <TopItems items={tt.applications} tr={tr} />
            </div>
          </div>
        </Section>
      );
    }
  }
}

/** [3] Report page /reports/runs/:id — printable (Print / Save as PDF), rendered in the report's language. */
export function ReportRunPage() {
  const { id } = useParams();
  const runId = Number(id);
  const { t, i18n } = useTranslation();
  const { features } = useFeatures();
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: qk3.reportRun(runId),
    queryFn: () => endpoints3.reportRun(runId),
    enabled: Number.isFinite(runId) && features.reports,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 3000 : false),
  });
  useLiveMessages(['report.run'], (m) => {
    if (m.type === 'report.run' && m.data.run.id === runId)
      void qc.invalidateQueries({ queryKey: qk3.reportRun(runId) });
  });
  const run = q.data;
  const lang = run?.language ?? (i18n.language === 'he' ? 'he' : 'en');
  const tr = useMemo(() => i18n.getFixedT(lang), [i18n, lang]);
  const d = run?.data;
  const fmt = useMemo(() => {
    const f = new Intl.DateTimeFormat(lang === 'he' ? 'he-IL' : 'en-GB', {
      dateStyle: 'short',
      timeStyle: 'short',
      timeZone: d?.timezone,
    });
    return (iso: string) => {
      const x = new Date(iso);
      return Number.isNaN(x.getTime()) ? '—' : f.format(x);
    };
  }, [lang, d?.timezone]);
  if (!features.reports) return <FeatureOffNotice feature="reports" />;
  return (
    <div className="mx-auto max-w-5xl space-y-4" data-testid="report-page">
      <div className="flex flex-wrap items-center gap-2 print:hidden">
        <Link to="/reports" className="text-sm text-sky-700 hover:underline dark:text-sky-400">
          ← {t('reports.title')}
        </Link>
        <span className="flex-1" />
        {run && run.status !== 'running' && (
          <>
            <a className={buttonClass()} href={endpoints3.reportHtmlUrl(run.id)} download>
              {t('reports.downloadHtml')}
            </a>
            <Button variant="primary" onClick={() => window.print()}>
              {t('reports.print')}
            </Button>
          </>
        )}
      </div>
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} />
      {run && (
        <div className="print:hidden">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <RunStatusBadge status={run.status} />
            <DeliveriesCell run={run} />
          </div>
        </div>
      )}
      {run?.status === 'running' && <Notice>{t('reports.runningNote')}</Notice>}
      {run && d && (
        <article
          dir={lang === 'he' ? 'rtl' : 'ltr'}
          lang={lang}
          className="report-print space-y-6 rounded-lg bg-white p-6 text-slate-900 shadow-sm ring-1 ring-slate-200 print:p-0 print:shadow-none print:ring-0"
        >
          <header className="space-y-1">
            <h1 className="text-2xl font-bold">{run.report_name}</h1>
            <p className="text-sm text-slate-600">
              {tr('reports.periodLine', {
                from: fmt(d.period_start),
                to: fmt(d.period_end),
                tz: d.timezone,
              })}
            </p>
            <p className="text-sm text-slate-600">
              {tr('reports.scopeLine', { scope: d.scope_label })} ·{' '}
              {tr('reports.generated', { time: fmt(d.generated_at) })}
            </p>
          </header>
          {d.sections.map((s) => (
            <ReportSectionView key={s} s={s} d={d} tr={tr} fmt={fmt} />
          ))}
          <footer className="border-t border-slate-200 pt-2 text-xs text-slate-500">
            NetworkMonitor
          </footer>
        </article>
      )}
    </div>
  );
}
