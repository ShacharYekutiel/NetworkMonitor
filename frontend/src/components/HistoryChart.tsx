import { useEffect, useRef } from 'react';
import * as echarts from 'echarts/core';
import { LineChart } from 'echarts/charts';
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import { useTranslation } from 'react-i18next';
import { escapeHtml, historyTooltipHtml } from '../lib/html';
import type { HistoryPoint, NmEvent, Status } from '../api/types';
import { useTheme } from '../hooks/theme';
import { formatValue } from '../lib/format';
import { eventMarkers, toSeries } from '../lib/history';
import { STATUS_HEX, STATUS_DOT, statusClass } from '../lib/status';

echarts.use([
  LineChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  MarkLineComponent,
  CanvasRenderer,
]);

export function HistoryChart({
  points,
  bucketS,
  unit,
  label,
  from,
  to,
  events,
  heightClass = 'h-72',
}: {
  points: HistoryPoint[];
  bucketS: number;
  unit: string;
  label: string;
  from: string;
  to: string;
  /** [2b-1] status_change events drawn as coloured markers (incl. unreachable). */
  events?: NmEvent[];
  /** [2b-2] smaller charts (port modal) */
  heightClass?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const { dark } = useTheme();
  const { t, i18n } = useTranslation();

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

  const markers = eventMarkers(events ?? [], from, to);

  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    const { avg, low, band } = toSeries(points, bucketS);
    const marks = eventMarkers(events ?? [], from, to);
    const text = dark ? '#cbd5e1' : '#475569';
    const grid = dark ? '#1e293b' : '#e2e8f0';
    const line = '#0284c7';
    const locale = i18n.language === 'he' ? 'he-IL' : 'en-GB';
    c.setOption(
      {
        animation: false,
        textStyle: { color: text },
        grid: { left: 64, right: 16, top: 28, bottom: 48 },
        legend: {
          top: 0,
          textStyle: { color: text },
          data: [t('chart.avg'), t('chart.minMax')],
        },
        tooltip: {
          trigger: 'axis',
          formatter: (params: unknown) => {
            const ps = params as { axisValue: number; dataIndex: number }[];
            if (!ps.length) return '';
            const i = ps[0].dataIndex;
            const a = avg[i]?.[1];
            const lo = low[i]?.[1];
            const b = band[i]?.[1];
            const when = new Date(ps[0].axisValue).toLocaleString(locale);
            const hi =
              lo !== null && lo !== undefined && b !== null && b !== undefined ? lo + b : null;
            // [1.4.1] escaped: the unit of a custom sensor channel is free text
            return historyTooltipHtml({
              when,
              avg: a === null || a === undefined ? null : formatValue(a, unit),
              min: formatValue(lo, unit),
              max: formatValue(hi, unit),
              labels: { avg: t('chart.avg'), min: t('chart.min'), max: t('chart.max') },
            });
          },
        },
        xAxis: {
          type: 'time',
          min: Date.parse(from),
          max: Date.parse(to),
          axisLine: { lineStyle: { color: grid } },
          axisLabel: {
            color: text,
            hideOverlap: true,
            // Day boundaries show a date ("4/11"), never a bare day number.
            formatter: {
              year: '{yyyy}',
              month: '{d}/{M}',
              day: '{d}/{M}',
              hour: '{HH}:{mm}',
              minute: '{HH}:{mm}',
              second: '{HH}:{mm}:{ss}',
              millisecond: '{HH}:{mm}:{ss}',
              none: '{d}/{M} {HH}:{mm}',
            },
          },
          splitLine: { show: false },
        },
        yAxis: {
          type: 'value',
          scale: false,
          axisLabel: { color: text, formatter: (v: number) => formatValue(v, unit) },
          splitLine: { lineStyle: { color: grid } },
        },
        dataZoom: [{ type: 'inside' }],
        series: [
          {
            name: 'min',
            type: 'line',
            data: low,
            stack: 'band',
            lineStyle: { opacity: 0 },
            symbol: 'none',
            silent: true,
            connectNulls: false,
          },
          {
            name: t('chart.minMax'),
            type: 'line',
            data: band,
            stack: 'band',
            lineStyle: { opacity: 0 },
            areaStyle: { color: line, opacity: 0.18 },
            itemStyle: { color: line, opacity: 0.4 },
            symbol: 'none',
            silent: true,
            connectNulls: false,
          },
          {
            name: t('chart.avg'),
            type: 'line',
            data: avg,
            symbol: 'none',
            lineStyle: { color: line, width: 1.6 },
            itemStyle: { color: line },
            connectNulls: false,
            markLine: marks.length
              ? {
                  silent: false,
                  symbol: 'none',
                  animation: false,
                  label: { show: false },
                  tooltip: {
                    formatter: (p: { data?: { name?: string } }) => escapeHtml(p.data?.name ?? ''),
                  },
                  data: marks.map((m) => ({
                    xAxis: m.t,
                    name: `${new Date(m.t).toLocaleString(locale)}: ${m.old ? `${t(`status.${m.old}`)} → ` : ''}${t(`status.${m.status}`)}`,
                    lineStyle: {
                      color: STATUS_HEX[m.status] ?? STATUS_HEX.unknown,
                      type: 'dashed',
                      width: 1.2,
                      opacity: 0.9,
                    },
                  })),
                }
              : undefined,
          },
        ],
      },
      true,
    );
  }, [points, bucketS, unit, dark, t, i18n.language, from, to, events]);

  const legend = [...new Set(markers.map((m) => m.status))] as Status[];
  // ECharts time axes stay left-to-right in both languages (conventions.md).
  return (
    <div>
      <div dir="ltr" ref={ref} className={`${heightClass} w-full`} role="img" aria-label={label} />
      {legend.length > 0 && (
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
          <span>{t('chart.statusChanges')}:</span>
          {legend.map((s) => (
            <span key={s} className="inline-flex items-center gap-1">
              <span className={`inline-block h-0.5 w-3 ${statusClass(s, STATUS_DOT)}`} />
              {t(`status.${s}`)}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
