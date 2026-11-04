import { useEffect, useRef } from 'react';
import * as echarts from 'echarts/core';
import { LineChart } from 'echarts/charts';
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import { useTranslation } from 'react-i18next';
import type { HistoryPoint } from '../api/types';
import { useTheme } from '../hooks/theme';
import { formatValue } from '../lib/format';
import { toSeries } from '../lib/history';

echarts.use([
  LineChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  CanvasRenderer,
]);

export function HistoryChart({
  points,
  bucketS,
  unit,
  label,
  from,
  to,
}: {
  points: HistoryPoint[];
  bucketS: number;
  unit: string;
  label: string;
  from: string;
  to: string;
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

  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    const { avg, low, band } = toSeries(points, bucketS);
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
          },
        ],
      },
      true,
    );
  }, [points, bucketS, unit, dark, t, i18n.language, from, to]);

  // ECharts time axes stay left-to-right in both languages (conventions.md).
  return <div dir="ltr" ref={ref} className="h-72 w-full" role="img" aria-label={label} />;
}
