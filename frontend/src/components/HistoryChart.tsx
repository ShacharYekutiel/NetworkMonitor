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
import type { HistoryPoint, NmEvent, Status } from '../api/types';
import { useTheme } from '../hooks/theme';
import { formatValue } from '../lib/format';
import { eventMarkers, toSeries } from '../lib/history';
import { STATUS_DOT, statusClass } from '../lib/status';
import { chartTokens, statusColor } from '../lib/chartTokens';

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
    // Design tokens, re-read when the theme changes (`dark` is a dependency).
    const tk = chartTokens(dark);
    const text = tk.inkSubtle;
    const grid = tk.chartGrid;
    const line = tk.chart1;
    const mono = { fontFamily: tk.fontMono, fontSize: 10.5 };
    const locale = i18n.language === 'he' ? 'he-IL' : 'en-GB';
    c.setOption(
      {
        animation: false,
        textStyle: { color: text, fontFamily: tk.fontSans },
        grid: { left: 8, right: 16, top: 28, bottom: 40, containLabel: true },
        legend: {
          top: 0,
          icon: 'roundRect',
          itemWidth: 12,
          itemHeight: 3,
          textStyle: { color: tk.inkMuted, fontSize: 12 },
          data: [t('chart.avg'), t('chart.minMax')],
        },
        tooltip: {
          trigger: 'axis',
          backgroundColor: tk.surfaceRaised,
          borderColor: tk.border,
          textStyle: { color: tk.ink, fontSize: 12 },
          axisPointer: { lineStyle: { color: tk.inkMuted, width: 1 } },
          formatter: (params: unknown) => {
            const ps = params as { axisValue: number; dataIndex: number }[];
            if (!ps.length) return '';
            const i = ps[0].dataIndex;
            const a = avg[i]?.[1];
            const lo = low[i]?.[1];
            const b = band[i]?.[1];
            const when = new Date(ps[0].axisValue).toLocaleString(locale);
            if (a === null || a === undefined) return when;
            const hi =
              lo !== null && lo !== undefined && b !== null && b !== undefined ? lo + b : null;
            return `${when}<br/>${t('chart.avg')}: <b>${formatValue(a, unit)}</b><br/>${t('chart.min')}: ${formatValue(lo, unit)}<br/>${t('chart.max')}: ${formatValue(hi, unit)}`;
          },
        },
        xAxis: {
          type: 'time',
          min: Date.parse(from),
          max: Date.parse(to),
          axisLine: { lineStyle: { color: grid } },
          axisLabel: {
            color: text,
            ...mono,
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
          axisLabel: { color: text, ...mono, formatter: (v: number) => formatValue(v, unit) },
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
            areaStyle: { color: line, opacity: 0.12 },
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
            lineStyle: { color: line, width: 1.75 },
            itemStyle: { color: line },
            connectNulls: false,
            markLine: marks.length
              ? {
                  silent: false,
                  symbol: 'none',
                  animation: false,
                  label: { show: false },
                  tooltip: {
                    formatter: (p: { data?: { name?: string } }) => p.data?.name ?? '',
                  },
                  data: marks.map((m) => ({
                    xAxis: m.t,
                    name: `${new Date(m.t).toLocaleString(locale)}: ${m.old ? `${t(`status.${m.old}`)} → ` : ''}${t(`status.${m.status}`)}`,
                    lineStyle: {
                      color: statusColor(tk, m.status),
                      type: [6, 4],
                      width: 1.25,
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
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-muted">
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
