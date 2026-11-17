import { useMemo, type CSSProperties } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { endpoints3, qk3 } from '../api/endpoints3';
import type { FlowTopItem, ReportData, ReportSection } from '../api/types3';
import {
  BackLink,
  buttonClass,
  Button,
  ErrorBox,
  Loading,
  Notice,
  PageHeader,
} from '../components/ui';
import { useAuth } from '../hooks/auth';
import { TOKEN_VALUES } from '../styles/tokens.gen';
import { useToast } from '../components/toast';
import { useFeatures, FeatureOffNotice } from '../hooks/features';
import { useLiveMessages } from '../hooks/live';
import { formatDuration } from '../lib/format';
import { formatBytes, formatShare } from '../lib/flows';
import { availabilityTone, barPct, formatAvailability } from '../lib/reports';
import { DeliveriesCell, RunStatusBadge } from './ReportsPage';

// prototype .paper table: 14px, hairline rows, grey 12.5px headers, numbers at the end in Inter
const pth =
  'border-b border-line px-1 pb-2 pt-1 text-start text-[12.5px] font-medium text-ink-muted';
const ptd = 'border-b border-line px-1 py-2.5 align-middle';
const pthN = `${pth} !text-end`;
const ptdN = `${ptd} num text-end`;

function Bar({ pct, tone = 'sky' }: { pct: number; tone?: 'sky' | 'green' | 'amber' | 'red' }) {
  const color = {
    sky: 'bg-accent',
    green: 'bg-status-up',
    amber: 'bg-status-warning',
    red: 'bg-status-down',
  }[tone];
  return (
    <div className="h-1.5 w-24 overflow-hidden rounded-full bg-chip print:border print:border-line-strong">
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
          ? 'text-status-up'
          : tone === 'warn'
            ? 'text-status-warning'
            : tone === 'bad'
              ? 'font-semibold text-status-down'
              : 'text-ink-muted'
      }
    >
      <bdi dir="ltr">{s ?? tr('reports.na')}</bdi>
    </span>
  );
}

/** Every token with its light value: the report paper is a white sheet in both themes. */
const PAPER_VARS = {
  colorScheme: 'light',
  ...Object.fromEntries(Object.entries(TOKEN_VALUES.light).map(([k, v]) => [`--${k}`, v])),
} as CSSProperties;

/** Hours of downtime as "3:38". */
function formatHM(s: number): string {
  const m = Math.round(s / 60);
  return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}`;
}

/** A big figure of the paper head (prototype: 32px Inter over a grey label). */
function Figure({ n, label }: { n: string; label: string }) {
  return (
    <div>
      <div className="num text-[32px] font-semibold leading-tight" dir="ltr">
        {n}
      </div>
      <span className="text-ink-muted">{label}</span>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="break-inside-avoid-page space-y-3">
      <h3 className="m-0 text-[19px] font-semibold tracking-[-0.01em]">{title}</h3>
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
            <td className={ptdN}>{formatShare(x.share)}</td>
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
        <p className="text-sm text-ink-muted">{tr(`reports.note.${note.note}`)}</p>
      </Section>
    );
  switch (s) {
    case 'availability': {
      const a = d.availability;
      if (!a) return null;
      return (
        <Section title={title}>
          {a.groups.length > 0 && (
            <table className="w-full text-sm">
              <thead>
                <tr>
                  <th className={pth}>{tr('reports.col.group')}</th>
                  <th className={pthN}>{tr('reports.col.devices')}</th>
                  <th className={pthN}>{tr('reports.col.availability')}</th>
                </tr>
              </thead>
              <tbody>
                {a.groups.map((g) => (
                  <tr key={g.group_id}>
                    <td className={ptd}>{g.group_name}</td>
                    <td className={ptdN}>{g.devices}</td>
                    <td className={ptdN}>
                      <div className="inline-flex items-center gap-2">
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
                <th className={pthN}>{tr('reports.col.availability')}</th>
                <th className={pthN}>{tr('reports.col.downtime')}</th>
                <th className={pthN}>{tr('reports.col.outages')}</th>
                <th className={pthN}>{tr('reports.col.longest')}</th>
              </tr>
            </thead>
            <tbody>
              {a.devices.map((r) => (
                <tr key={r.device_id}>
                  <td className={ptd}>{r.device_name}</td>
                  <td className={ptd}>{r.group_name}</td>
                  <td className={ptdN}>
                    <Avail v={r.availability} tr={tr} />
                  </td>
                  <td className={ptdN}>{r.downtime_s ? formatDuration(r.downtime_s) : '—'}</td>
                  <td className={ptdN}>{r.outages}</td>
                  <td className={ptdN}>
                    {r.longest_outage_s ? formatDuration(r.longest_outage_s) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {a.not_measured.length > 0 && (
            <p className="text-xs text-ink-muted">
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
                  <td className={ptdN}>{r.episodes}</td>
                  <td className={ptdN}>{r.transitions}</td>
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
                  <td className={ptdN}>{r.errors}</td>
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
                  <td className={ptdN}>{r.changes}</td>
                  <td className={ptdN}>
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
  const { isAdmin } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const resend = useMutation({
    mutationFn: () => endpoints3.resendReport(runId),
    onSuccess: () => {
      toast.push('success', t('reports.resent'));
      void qc.invalidateQueries({ queryKey: qk3.reportRun(runId) });
      void qc.invalidateQueries({ queryKey: ['report-runs'] });
    },
  });
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
  const a = d?.availability;
  const downtime = a ? a.devices.reduce((n, r) => n + r.downtime_s, 0) : 0;
  const outages = a ? a.devices.reduce((n, r) => n + r.outages, 0) : 0;
  const failedDelivery = !!run && run.status !== 'running' && run.deliveries.some((x) => !x.ok);
  return (
    <div data-testid="report-page">
      <div className="print:hidden">
        <PageHeader
          breadcrumbs={<BackLink to="/reports" label={t('reports.title')} />}
          title={run?.report_name ?? t('reports.report')}
          subtitle={
            d &&
            t('reports.paperSub', {
              from: fmt(d.period_start),
              to: fmt(d.period_end),
              time: fmt(d.generated_at),
            })
          }
          meta={
            run && (
              <>
                <RunStatusBadge status={run.status} />
                <DeliveriesCell run={run} />
                {/* [3b] AI summary status (run details only) */}
                {run.ai_summary_status && run.ai_summary_status !== 'ok' && (
                  <span data-testid="ai-summary-status">
                    {t('reports.aiSummaryStatus', {
                      status: t(`reports.aiStatus.${run.ai_summary_status}`),
                    })}
                  </span>
                )}
              </>
            )
          }
          actions={
            run &&
            run.status !== 'running' && (
              <>
                <a className={buttonClass()} href={endpoints3.reportHtmlUrl(run.id)} download>
                  {t('reports.downloadHtml')}
                </a>
                {isAdmin && failedDelivery && (
                  <Button busy={resend.isPending} onClick={() => resend.mutate()}>
                    {t('reports.resendFailed')}
                  </Button>
                )}
                <Button variant="primary" onClick={() => window.print()}>
                  {t('reports.print')}
                </Button>
              </>
            )
          }
        />
      </div>
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error ?? resend.error} />
      {run?.status === 'running' && <Notice>{t('reports.runningNote')}</Notice>}
      {run && d && (
        // prototype .paper: a white sheet in both themes (light tokens inside), 820px, centred
        <article
          dir={lang === 'he' ? 'rtl' : 'ltr'}
          lang={lang}
          style={PAPER_VARS}
          className="report-print mx-auto grid max-w-[820px] gap-[26px] rounded-[6px] bg-surface px-[52px] py-[46px] text-ink shadow-[0_30px_80px_rgba(0,0,0,0.18)] max-sm:px-5 max-sm:py-7 print:max-w-none print:p-0 print:shadow-none"
        >
          <header className="grid gap-2">
            <span className="text-ink-muted">
              Network Monitor · <bdi dir="auto">{d.scope_label}</bdi>
            </span>
            <h1 className="m-0 text-[30px] font-semibold leading-tight tracking-[-0.02em]">
              {a && a.overall !== null ? (
                tr('reports.paperHeadline', { pct: formatAvailability(a.overall) })
              ) : (
                <bdi dir="auto">{run.report_name}</bdi>
              )}
            </h1>
            <p className="m-0 text-sm text-ink-muted">
              {a && a.overall !== null && (
                <>
                  <bdi dir="auto">{run.report_name}</bdi>
                  {' · '}
                </>
              )}
              {tr('reports.periodLine', {
                from: fmt(d.period_start),
                to: fmt(d.period_end),
                tz: d.timezone,
              })}
              {' · '}
              {tr('reports.generated', { time: fmt(d.generated_at) })}
            </p>
          </header>
          {a && (
            <div className="flex flex-wrap gap-10" data-testid="report-figures">
              <Figure n={String(a.devices.length)} label={tr('reports.fig.devices')} />
              <Figure n={formatHM(downtime)} label={tr('reports.fig.downtime')} />
              <Figure n={String(outages)} label={tr('reports.fig.outages')} />
            </div>
          )}
          {d.ai_summary && (
            <section
              className="rounded-[14px] bg-surface-sunken px-4 py-3.5 print:bg-transparent print:px-0"
              data-testid="report-ai-summary"
            >
              <h2 className="mb-1 text-xs font-semibold text-ink-muted">
                {tr('reports.aiSummaryLabel')}
              </h2>
              <p className="m-0 whitespace-pre-line text-sm" dir="auto">
                {d.ai_summary}
              </p>
            </section>
          )}
          {d.sections.map((s) => (
            <ReportSectionView key={s} s={s} d={d} tr={tr} fmt={fmt} />
          ))}
          <footer className="border-t border-line pt-2 text-xs text-ink-muted">
            NetworkMonitor
          </footer>
        </article>
      )}
    </div>
  );
}
