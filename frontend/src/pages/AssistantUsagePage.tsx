import { useEffect, useRef, useState } from 'react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import * as echarts from 'echarts/core';
import { BarChart } from 'echarts/charts';
import { GridComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import { endpoints3b, qk3b } from '../api/endpoints3b';
import { usageTooltipHtml } from '../lib/html';
import type { UsageReport } from '../api/types';
import {
  Button,
  Card,
  cx,
  Empty,
  ErrorBox,
  Loading,
  PageHeader,
  Table,
  td,
  th,
} from '../components/ui';
import { useAuth } from '../hooks/auth';
import { useTheme } from '../hooks/theme';
import {
  compactTokens,
  formatCost,
  friendlyModelName,
  monthKey,
  pctTone,
  shiftMonth,
} from '../lib/assistant';

echarts.use([BarChart, GridComponent, TooltipComponent, CanvasRenderer]);

function DailyBars({ r }: { r: UsageReport }) {
  const ref = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const { dark } = useTheme();
  const { t } = useTranslation();
  useEffect(() => {
    if (!ref.current) return;
    const c = echarts.init(ref.current, undefined, { renderer: 'canvas' });
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
    const text = dark ? '#cbd5e1' : '#475569';
    const grid = dark ? '#1e293b' : '#e2e8f0';
    c.setOption(
      {
        animation: false,
        textStyle: { color: text },
        grid: { left: 56, right: 16, top: 16, bottom: 32 },
        tooltip: {
          trigger: 'axis',
          formatter: (params: unknown) => {
            const p = (params as { dataIndex: number }[])[0];
            const d = r.by_day[p?.dataIndex ?? 0];
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
          data: r.by_day.map((d) => d.day.slice(8)),
          axisLine: { lineStyle: { color: grid } },
        },
        yAxis: {
          type: 'value',
          splitLine: { lineStyle: { color: grid } },
          axisLabel: { formatter: (v: number) => compactTokens(v) },
        },
        series: [
          { type: 'bar', data: r.by_day.map((d) => d.tokens), itemStyle: { color: '#7c3aed' } },
        ],
      },
      true,
    );
  }, [r, dark, t]);
  return <div ref={ref} className="h-56 w-full" dir="ltr" data-testid="usage-chart" />;
}

function Tile({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: 'ok' | 'warn' | 'error';
}) {
  return (
    <div className="rounded-lg bg-white p-3 shadow-sm ring-1 ring-slate-200 dark:bg-slate-900 dark:ring-slate-800">
      <div className="text-xs text-slate-500">{label}</div>
      <div
        className={cx(
          'text-xl font-semibold tabular-nums',
          tone === 'warn' && 'text-amber-600',
          tone === 'error' && 'text-red-600',
        )}
      >
        {value}
      </div>
      {sub && <div className="text-xs text-slate-500">{sub}</div>}
    </div>
  );
}

/** /assistant/usage — tokens, cost estimate, caps (admins; readable while the assistant is OFF). */
export function AssistantUsagePage() {
  const { t } = useTranslation();
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
  return (
    <div className="space-y-4">
      <PageHeader
        title={t('usage.title')}
        subtitle={t('usage.estimateNote')}
        actions={
          <div className="flex items-center gap-1" dir="ltr">
            <Button
              size="sm"
              onClick={() => setMonth((m) => shiftMonth(m, -1))}
              aria-label={t('usage.prevMonth')}
            >
              ‹
            </Button>
            <span
              className="min-w-20 text-center text-sm font-medium tabular-nums"
              data-testid="usage-month"
            >
              {month}
            </span>
            <Button
              size="sm"
              onClick={() => setMonth((m) => shiftMonth(m, 1))}
              disabled={month >= current}
              aria-label={t('usage.nextMonth')}
            >
              ›
            </Button>
          </div>
        }
      />
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} />
      {r && (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-5" data-testid="usage-tiles">
            <Tile label={t('usage.questions')} value={String(questions)} />
            <Tile
              label={t('usage.tokens')}
              value={compactTokens(r.tokens)}
              sub={t('usage.ofCap', { cap: compactTokens(r.token_cap) })}
            />
            <Tile
              label={t('usage.cost')}
              value={
                r.cost_estimate !== null ? `≈ ${formatCost(r.cost_estimate, r.currency)}` : '—'
              }
              sub={
                r.cost_cap !== null
                  ? t('usage.ofCap', { cap: formatCost(r.cost_cap, r.currency) })
                  : r.cost_estimate === null
                    ? t('usage.noPrices')
                    : undefined
              }
            />
            <Tile
              label={t('usage.pctOfCap')}
              value={`${Math.round(r.pct_of_cap)} %`}
              tone={pctTone(r.pct_of_cap)}
              sub={r.background_stopped ? t('usage.backgroundStopped') : undefined}
            />
            <Tile
              label={t('usage.projectionTile')}
              value={`${Math.round(r.projection_pct)} %`}
              tone={pctTone(r.projection_pct)}
            />
          </div>
          <Card title={t('usage.perDay')}>
            {r.by_day.length ? <DailyBars r={r} /> : <Empty>{t('usage.none')}</Empty>}
          </Card>
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <Card title={t('usage.perUser')} bodyClassName="p-0">
              <Table>
                <thead>
                  <tr>
                    <th className={th}>{t('usage.user')}</th>
                    <th className={th}>{t('usage.questions')}</th>
                    <th className={th}>{t('usage.tokens')}</th>
                    <th className={th}>{t('usage.cost')}</th>
                  </tr>
                </thead>
                <tbody>
                  {r.by_user.map((u) => (
                    <tr key={u.user_id}>
                      <td className={td}>{u.user_id === 0 ? t('usage.system') : u.username}</td>
                      <td className={`${td} tabular-nums`}>{u.questions}</td>
                      <td className={`${td} tabular-nums`}>{compactTokens(u.tokens)}</td>
                      <td className={`${td} tabular-nums`}>
                        {formatCost(u.cost_estimate, r.currency)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </Card>
            <Card title={t('usage.perPurpose')} bodyClassName="p-0">
              <Table>
                <thead>
                  <tr>
                    <th className={th}>{t('usage.purpose')}</th>
                    <th className={th}>{t('usage.calls')}</th>
                    <th className={th}>{t('usage.tokens')}</th>
                    <th className={th}>{t('usage.cost')}</th>
                  </tr>
                </thead>
                <tbody>
                  {r.by_purpose.map((p) => (
                    <tr key={p.purpose}>
                      <td className={td}>
                        {t(`usage.purposes.${p.purpose}`, { defaultValue: p.purpose })}
                      </td>
                      <td className={`${td} tabular-nums`}>{p.calls}</td>
                      <td className={`${td} tabular-nums`}>{compactTokens(p.tokens)}</td>
                      <td className={`${td} tabular-nums`}>
                        {formatCost(p.cost_estimate, r.currency)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </Card>
            <Card title={t('usage.perModel')} bodyClassName="p-0">
              <div data-testid="usage-by-model">
                {r.by_model?.length ? (
                  <Table>
                    <thead>
                      <tr>
                        <th className={th}>{t('usage.model')}</th>
                        <th className={th}>{t('usage.calls')}</th>
                        <th className={th}>{t('usage.inputTokens')}</th>
                        <th className={th}>{t('usage.outputTokens')}</th>
                        <th className={th}>{t('usage.tokens')}</th>
                        <th className={th}>{t('usage.cost')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {r.by_model.map((m) => (
                        <tr key={m.model_id || '-'}>
                          <td className={td}>
                            {m.model_id ? (
                              <span title={m.model_id}>
                                {friendlyModelName(m.model_id)}
                                <bdi
                                  dir="ltr"
                                  className="block font-mono text-[11px] text-slate-500"
                                >
                                  {m.model_id}
                                </bdi>
                              </span>
                            ) : (
                              t('usage.unknownModel')
                            )}
                          </td>
                          <td className={`${td} tabular-nums`}>{m.calls}</td>
                          <td className={`${td} tabular-nums`}>
                            {compactTokens(m.input_tokens)}
                            {m.cache_read_tokens > 0 && (
                              <span
                                className="block text-[11px] text-slate-500"
                                title={t('usage.cacheRead')}
                              >
                                +{compactTokens(m.cache_read_tokens)} {t('usage.cached')}
                              </span>
                            )}
                          </td>
                          <td className={`${td} tabular-nums`}>{compactTokens(m.output_tokens)}</td>
                          <td className={`${td} tabular-nums`}>{compactTokens(m.tokens)}</td>
                          <td className={`${td} tabular-nums`}>
                            {formatCost(m.cost_estimate, r.currency)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </Table>
                ) : (
                  <Empty>{t('usage.none')}</Empty>
                )}
              </div>
            </Card>
            <Card title={t('usage.topTools')} bodyClassName="p-0">
              <Table>
                <thead>
                  <tr>
                    <th className={th}>{t('usage.tool')}</th>
                    <th className={th}>{t('usage.calls')}</th>
                    <th className={th}>{t('usage.errors')}</th>
                    <th className={th}>{t('usage.avgMs')}</th>
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
                      <td className={`${td} tabular-nums`}>{x.calls}</td>
                      <td className={`${td} tabular-nums`}>{x.errors}</td>
                      <td className={`${td} tabular-nums`}>{Math.round(x.avg_ms)}</td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </Card>
            <Card title={t('usage.errorsTitle')} bodyClassName="p-0">
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
                        <td className={`${td} tabular-nums`}>{e.count}</td>
                      </tr>
                    ))}
                  </tbody>
                </Table>
              ) : (
                <Empty>{t('usage.noErrors')}</Empty>
              )}
              <p className="border-t border-slate-200 px-3 py-2 text-xs text-slate-500 dark:border-slate-800">
                {t('usage.feedback', {
                  up: r.feedback.up,
                  down: r.feedback.down,
                  pct: fb ? Math.round((r.feedback.up / fb) * 100) : 0,
                })}
              </p>
            </Card>
          </div>
          <Card title={t('usage.last12')} bodyClassName="p-0">
            <Table>
              <thead>
                <tr>
                  <th className={th}>{t('usage.month')}</th>
                  <th className={th}>{t('usage.tokens')}</th>
                  <th className={th}>{t('usage.cost')}</th>
                </tr>
              </thead>
              <tbody>
                {r.last_12_months.map((m) => (
                  <tr key={m.month}>
                    <td className={`${td} tabular-nums`}>
                      <button
                        type="button"
                        className="hover:underline"
                        onClick={() => setMonth(m.month)}
                      >
                        {m.month}
                      </button>
                    </td>
                    <td className={`${td} tabular-nums`}>{compactTokens(m.tokens)}</td>
                    <td className={`${td} tabular-nums`}>
                      {formatCost(m.cost_estimate, r.currency)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </Card>
          <p className="text-xs text-slate-500">{t('usage.kbNote')}</p>
        </>
      )}
    </div>
  );
}
