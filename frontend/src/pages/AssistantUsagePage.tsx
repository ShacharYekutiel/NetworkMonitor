import { useEffect, useRef, useState } from 'react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import * as echarts from 'echarts/core';
import { BarChart } from 'echarts/charts';
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import { flatTooltip, initChart } from '../lib/echarts';
import { endpoints3b, qk3b } from '../api/endpoints3b';
import { usageTooltipHtml } from '../lib/html';
import { canvasColor, chartTokens } from '../lib/chartTokens';
import type { UsageReport } from '../api/types';
import { Link } from 'react-router';
import { ChevronIcon, GaugeIcon } from '../components/icons';
import { Banner, EmptyState, ShareBar } from '../components/kit';
import {
  ColumnsMenu,
  DataTable,
  labelOf,
  useDataTable,
  type DataColumn,
} from '../components/kitTable';
import {
  buttonClass,
  Card,
  cx,
  Empty,
  GroupTitle,
  Loading,
  PageHeader,
  Table,
  TAG,
  TAG_TONE,
  td,
  th,
} from '../components/ui';
import { errorMessage } from '../lib/errors';

/** A number cell (prototype .tbl td.n): end-aligned Inter. */
const NUM = cx(td, 'num text-end tabular-nums');
import { useAuth } from '../hooks/auth';
import { useTheme } from '../hooks/theme';
import {
  compactTokens,
  formatCost,
  friendlyModelName,
  monthDays,
  monthKey,
  pctTone,
  shiftMonth,
} from '../lib/assistant';

echarts.use([BarChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

function DailyBars({ r, month }: { r: UsageReport; month: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const { dark } = useTheme();
  const { t } = useTranslation();
  useEffect(() => {
    if (!ref.current) return;
    const c = initChart(ref.current);
    chart.current = c;
    const ro = new ResizeObserver(() => c.resize());
    ro.observe(ref.current);
    return () => {
      ro.disconnect();
      c.dispose();
      chart.current = null;
    };
  }, []);
  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    // premium flat bars (prototype usage .bars): grey bars, today in ink, no y axis and no grid;
    // time runs left to right in both directions
    const tk = chartTokens(dark);
    const days = monthDays(month, r.by_day);
    const today = new Date().toISOString().slice(0, 10);
    const grey = canvasColor(tk.borderStrong);
    const empty = canvasColor(tk.border);
    c.setOption(
      {
        animation: false,
        textStyle: { color: tk.inkSubtle, fontFamily: tk.fontSans },
        grid: { left: 4, right: 4, top: 8, bottom: 26 },
        tooltip: {
          trigger: 'axis',
          ...flatTooltip(tk),
          textStyle: { color: tk.ink, fontSize: 12 },
          axisPointer: { type: 'none' },
          formatter: (params: unknown) => {
            const p = (params as { dataIndex: number }[])[0];
            const d = days[p?.dataIndex ?? 0];
            if (!d) return '';
            // [1.4.1] escaped (the currency is a free-text setting)
            return usageTooltipHtml({
              day: d.day,
              tokens: compactTokens(d.tokens),
              questions: d.questions,
              cost: d.cost_estimate !== null ? formatCost(d.cost_estimate, r.currency) : null,
              labels: { tokens: t('usage.tokens'), questions: t('usage.questions') },
            });
          },
        },
        xAxis: {
          type: 'category',
          data: days.map((d) => String(Number(d.day.slice(8)))),
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: {
            color: tk.inkSubtle,
            fontFamily: tk.fontDisplay,
            fontSize: 11.5,
            fontWeight: 500,
            interval: (i: number) => i % 7 === 0 || (i === days.length - 1 && i % 7 >= 3),
          },
        },
        yAxis: { type: 'value', show: false, splitLine: { show: false } },
        series: [
          {
            name: t('usage.tokens'),
            type: 'bar',
            barCategoryGap: '12%',
            barMinHeight: 3,
            data: days.map((d) => ({
              value: d.tokens,
              itemStyle: {
                color: d.day === today ? tk.ink : d.tokens ? grey : empty,
                borderRadius: [4, 4, 2, 2],
              },
            })),
          },
        ],
      },
      true,
    );
  }, [r, month, dark, t]);
  return <div ref={ref} className="h-[150px] w-full" dir="ltr" data-testid="usage-chart" />;
}

/** A usage tile (prototype .tile): grey label, 34px Inter number, a small grey line under it. */
function Tile({
  label,
  value,
  sub,
  tone,
  bar,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: 'ok' | 'warn' | 'error';
  /** a share bar under the number (% of the cap) */
  bar?: number;
}) {
  return (
    <div className="grid min-w-0 content-start gap-1.5 rounded-[22px] bg-surface px-5 py-[18px] shadow-card">
      <span className="truncate text-[13.5px] text-ink-muted">{label}</span>
      <b
        className={cx(
          'num text-[34px] font-semibold leading-[1.05] tracking-[-0.03em] tabular-nums',
          tone === 'warn' && 'text-status-warning',
          tone === 'error' && 'text-status-down',
        )}
      >
        {value}
      </b>
      {sub && <small className="text-[12.5px] text-ink-subtle">{sub}</small>}
      {bar !== undefined && (
        <div className="mt-1.5">
          <ShareBar pct={bar} />
        </div>
      )}
    </div>
  );
}

/** /assistant/usage — tokens, cost estimate, caps (admins; readable while the assistant is OFF). */
type ByUser = UsageReport['by_user'][number];
type ByModel = NonNullable<UsageReport['by_model']>[number];

/** Usage per user (a data table: columns move / resize, remembered). */
function UsageByUser({ r }: { r: UsageReport }) {
  const { t } = useTranslation();
  const cols: DataColumn<ByUser>[] = [
    {
      id: 'user',
      label: t('usage.user'),
      minWidth: 120,
      title: (u) => (u.user_id === 0 ? t('usage.system') : u.username),
      cell: (u) =>
        u.user_id === 0 ? (
          <b className="font-semibold">{t('usage.system')}</b>
        ) : (
          <b className="font-display font-semibold">{u.username}</b>
        ),
    },
    {
      id: 'questions',
      label: t('usage.questions'),
      width: 110,
      numeric: true,
      cell: (u) => u.questions,
    },
    {
      id: 'tokens',
      label: t('usage.tokens'),
      width: 110,
      numeric: true,
      cell: (u) => compactTokens(u.tokens),
    },
    {
      id: 'cost',
      label: t('usage.cost'),
      width: 110,
      numeric: true,
      cell: (u) => formatCost(u.cost_estimate, r.currency),
    },
  ];
  const layout = useDataTable('usage-users', cols);
  return (
    <div className="min-w-0">
      <GroupTitle
        action={
          <ColumnsMenu
            layout={layout}
            labelOf={(id) => labelOf(cols, id)}
            testId="usage-user-columns"
          />
        }
      >
        {t('usage.perUser')}
      </GroupTitle>
      <Card bodyClassName="p-0">
        <DataTable
          layout={layout}
          columns={layout.columns}
          rows={r.by_user}
          rowKey={(u) => u.user_id}
        />
        {!r.by_user.length && <Empty>{t('usage.none')}</Empty>}
      </Card>
    </div>
  );
}

/** Usage per model (a data table). */
function UsageByModel({ r }: { r: UsageReport }) {
  const { t } = useTranslation();
  const cols: DataColumn<ByModel>[] = [
    {
      id: 'model',
      label: t('usage.model'),
      minWidth: 150,
      title: (m) => m.model_id || undefined,
      cell: (m) =>
        m.model_id ? (
          <span>
            {friendlyModelName(m.model_id)}
            <bdi dir="ltr" className="block truncate font-mono text-[11px] text-ink-muted">
              {m.model_id}
            </bdi>
          </span>
        ) : (
          t('usage.unknownModel')
        ),
    },
    { id: 'calls', label: t('usage.calls'), width: 80, numeric: true, cell: (m) => m.calls },
    {
      id: 'input',
      label: t('usage.inputTokens'),
      width: 120,
      numeric: true,
      cell: (m) => (
        <>
          {compactTokens(m.input_tokens)}
          {m.cache_read_tokens > 0 && (
            <span className="block text-[11px] text-ink-muted" title={t('usage.cacheRead')}>
              +{compactTokens(m.cache_read_tokens)} {t('usage.cached')}
            </span>
          )}
        </>
      ),
    },
    {
      id: 'output',
      label: t('usage.outputTokens'),
      width: 110,
      numeric: true,
      cell: (m) => compactTokens(m.output_tokens),
    },
    {
      id: 'tokens',
      label: t('usage.tokens'),
      width: 100,
      numeric: true,
      cell: (m) => compactTokens(m.tokens),
    },
    {
      id: 'cost',
      label: t('usage.cost'),
      width: 100,
      numeric: true,
      cell: (m) => formatCost(m.cost_estimate, r.currency),
    },
  ];
  const layout = useDataTable('usage-models', cols);
  return (
    <div className="min-w-0">
      <GroupTitle
        action={
          <ColumnsMenu
            layout={layout}
            labelOf={(id) => labelOf(cols, id)}
            testId="usage-model-columns"
          />
        }
      >
        {t('usage.perModel')}
      </GroupTitle>
      <Card bodyClassName="p-0">
        <div data-testid="usage-by-model">
          {r.by_model?.length ? (
            <DataTable
              layout={layout}
              columns={layout.columns}
              rows={r.by_model}
              rowKey={(m) => m.model_id || '-'}
            />
          ) : (
            <Empty>{t('usage.none')}</Empty>
          )}
        </div>
      </Card>
    </div>
  );
}

export function AssistantUsagePage() {
  const { t, i18n } = useTranslation();
  const { isAdmin } = useAuth();
  const [month, setMonth] = useState(() => monthKey(new Date()));
  const q = useQuery({
    queryKey: qk3b.usage(month),
    queryFn: () => endpoints3b.usage(month),
    enabled: isAdmin,
    placeholderData: keepPreviousData,
  });
  const r = q.data;
  const current = monthKey(new Date());
  const questions = r?.by_day.reduce((a, d) => a + d.questions, 0) ?? 0;
  const fb = r ? r.feedback.up + r.feedback.down : 0;
  const locale = i18n.language === 'he' ? 'he-IL' : 'en-GB';
  const monthLabel = new Intl.DateTimeFormat(locale, { month: 'long', year: 'numeric' }).format(
    new Date(`${month}-15T12:00:00`),
  );
  const purposeTotal = r?.by_purpose.reduce((a, p) => a + p.tokens, 0) ?? 0;
  const tokenPct = r && r.token_cap ? (r.tokens / r.token_cap) * 100 : 0;
  const round =
    'inline-flex h-9 w-9 items-center justify-center rounded-full text-ink-muted transition-colors hover:bg-chip hover:text-ink disabled:opacity-40 disabled:hover:bg-transparent';
  return (
    <div>
      <PageHeader title={t('usage.title')} subtitle={t('usage.estimateNote')} />
      {/* prototype .monthnav: round chevrons around the month's name */}
      <div className="-mt-2 mb-6 flex items-center gap-1">
        <button
          type="button"
          className={round}
          onClick={() => setMonth((m) => shiftMonth(m, -1))}
          aria-label={t('usage.prevMonth')}
        >
          <ChevronIcon size={16} className="rotate-180 rtl:rotate-0" />
        </button>
        <b className="px-2 text-[15px] font-semibold" data-testid="usage-month" data-month={month}>
          {monthLabel}
        </b>
        <button
          type="button"
          className={round}
          onClick={() => setMonth((m) => shiftMonth(m, 1))}
          disabled={month >= current}
          aria-label={t('usage.nextMonth')}
        >
          <ChevronIcon size={16} className="rtl:rotate-180" />
        </button>
      </div>
      {q.isLoading && <Loading />}
      {q.error && (
        <div className="rounded-xl bg-surface shadow-card">
          <EmptyState error title={t('usage.loadError')}>
            {errorMessage(t, q.error)}
          </EmptyState>
        </div>
      )}
      {r && (
        <div className="grid gap-4">
          {r.background_stopped && (
            <Banner
              tone="warn"
              title={t('usage.backgroundStopped')}
              actions={
                <Link to="/settings?tab=assistant" className={buttonClass('secondary', 'sm')}>
                  {t('nav.settings')}
                </Link>
              }
            >
              {t('usage.backgroundStoppedHint')}
            </Banner>
          )}
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4" data-testid="usage-tiles">
            <Tile
              label={t('usage.questions')}
              value={String(questions)}
              sub={t('usage.thisMonth')}
            />
            <Tile
              label={t('usage.tokens')}
              value={compactTokens(r.tokens)}
              sub={t('usage.ofCap', { cap: compactTokens(r.token_cap) })}
              bar={tokenPct}
              tone={pctTone(tokenPct) === 'ok' ? undefined : pctTone(tokenPct)}
            />
            <Tile
              label={t('usage.cost')}
              value={r.cost_estimate !== null ? formatCost(r.cost_estimate, r.currency) : '—'}
              sub={[
                r.cost_cap !== null
                  ? t('usage.ofCap', { cap: formatCost(r.cost_cap, r.currency) })
                  : r.cost_estimate === null
                    ? t('usage.noPrices')
                    : '',
                t('usage.pctOfMonthlyCap', { pct: Math.round(r.pct_of_cap) }),
              ]
                .filter(Boolean)
                .join(' · ')}
              tone={pctTone(r.pct_of_cap) === 'ok' ? undefined : pctTone(r.pct_of_cap)}
            />
            <Tile
              label={t('usage.projectionTile')}
              value={`${Math.round(r.projection_pct)}%`}
              sub={t('usage.ofMonthlyCap')}
              tone={r.projection_pct > 100 ? 'warn' : undefined}
            />
          </div>
          <Card
            title={t('usage.perDay')}
            actions={<span className={cx(TAG, TAG_TONE.neutral)}>{t('usage.inOut')}</span>}
          >
            {r.by_day.length ? (
              <DailyBars r={r} month={month} />
            ) : (
              <EmptyState icon={GaugeIcon} title={t('usage.none')}>
                {t('usage.noneHint')}
              </EmptyState>
            )}
          </Card>
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <UsageByUser r={r} />
            <div className="min-w-0">
              <GroupTitle>{t('usage.perPurpose')}</GroupTitle>
              <Card>
                <div className="grid gap-3.5" data-testid="usage-by-purpose">
                  {r.by_purpose.map((p) => {
                    const pct = purposeTotal ? (p.tokens / purposeTotal) * 100 : 0;
                    return (
                      <div key={p.purpose} className="grid gap-1.5">
                        <div className="flex justify-between gap-3">
                          <span>
                            {t(`usage.purposes.${p.purpose}`, { defaultValue: p.purpose })}
                          </span>
                          <span
                            className="num text-ink-muted"
                            title={`${t('usage.calls')}: ${p.calls} · ${t('usage.tokens')}: ${compactTokens(p.tokens)} · ${formatCost(p.cost_estimate, r.currency)}`}
                          >
                            {Math.round(pct)}%
                          </span>
                        </div>
                        <ShareBar pct={pct} />
                      </div>
                    );
                  })}
                  {!r.by_purpose.length && <p className="m-0 text-ink-muted">{t('usage.none')}</p>}
                  <span className="text-[12.5px] text-ink-subtle">
                    {t('usage.feedback', {
                      up: r.feedback.up,
                      down: r.feedback.down,
                      pct: fb ? Math.round((r.feedback.up / fb) * 100) : 0,
                    })}
                  </span>
                </div>
              </Card>
            </div>
            <UsageByModel r={r} />
            <div className="min-w-0">
              <GroupTitle>{t('usage.topTools')}</GroupTitle>
              <Card bodyClassName="p-0">
                <Table>
                  <thead>
                    <tr>
                      <th className={th}>{t('usage.tool')}</th>
                      <th className={cx(th, 'text-end')}>{t('usage.calls')}</th>
                      <th className={cx(th, 'text-end')}>{t('usage.errors')}</th>
                      <th className={cx(th, 'text-end')}>{t('usage.avgMs')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {r.top_tools.map((x) => (
                      <tr key={x.tool}>
                        <td className={td}>
                          <bdi dir="ltr" className="font-mono text-xs">
                            {x.tool}
                          </bdi>
                        </td>
                        <td className={NUM}>{x.calls}</td>
                        <td className={NUM}>{x.errors}</td>
                        <td className={NUM}>{Math.round(x.avg_ms)}</td>
                      </tr>
                    ))}
                  </tbody>
                </Table>
                {!r.top_tools.length && <Empty>{t('usage.none')}</Empty>}
              </Card>
            </div>
            <div className="min-w-0">
              <GroupTitle>{t('usage.errorsTitle')}</GroupTitle>
              <Card bodyClassName="p-0">
                {r.errors.length ? (
                  <Table>
                    <tbody>
                      {r.errors.map((e) => (
                        <tr key={e.code}>
                          <td className={td}>
                            <bdi dir="ltr" className="font-mono text-xs">
                              {e.code}
                            </bdi>
                          </td>
                          <td className={NUM}>{e.count}</td>
                        </tr>
                      ))}
                    </tbody>
                  </Table>
                ) : (
                  <Empty>{t('usage.noErrors')}</Empty>
                )}
              </Card>
            </div>
            <div className="min-w-0">
              <GroupTitle>{t('usage.last12')}</GroupTitle>
              <Card bodyClassName="p-0">
                <Table>
                  <thead>
                    <tr>
                      <th className={th}>{t('usage.month')}</th>
                      <th className={cx(th, 'text-end')}>{t('usage.tokens')}</th>
                      <th className={cx(th, 'text-end')}>{t('usage.cost')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {r.last_12_months.map((m) => (
                      <tr key={m.month}>
                        <td className={cx(td, 'num')}>
                          <button
                            type="button"
                            className="text-ink hover:underline"
                            onClick={() => setMonth(m.month)}
                          >
                            {m.month}
                          </button>
                        </td>
                        <td className={NUM}>{compactTokens(m.tokens)}</td>
                        <td className={NUM}>{formatCost(m.cost_estimate, r.currency)}</td>
                      </tr>
                    ))}
                  </tbody>
                </Table>
              </Card>
            </div>
          </div>
          <p className="m-0 text-[12.5px] text-ink-subtle">{t('usage.kbNote')}</p>
        </div>
      )}
    </div>
  );
}
