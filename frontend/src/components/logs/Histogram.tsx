import { useEffect, useRef } from 'react';
import * as echarts from 'echarts/core';
import { BarChart } from 'echarts/charts';
import { GridComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import { useTranslation } from 'react-i18next';
import { useTheme } from '../../hooks/theme';
import { SEVERITY_HEX, severityTone } from '../../lib/logs';

echarts.use([BarChart, GridComponent, TooltipComponent, CanvasRenderer]);

export type HistogramSeries = { name: string; color: string; data: number[] };

/** [2b-3] Bars over time (syslog: stacked by severity; traps: one series). */
export function Histogram({
  times,
  series,
  bucketS,
  from,
  to,
}: {
  times: string[];
  series: HistogramSeries[];
  bucketS: number;
  from: string;
  to: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const { dark } = useTheme();
  const { i18n } = useTranslation();

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
    const locale = i18n.language === 'he' ? 'he-IL' : 'en-GB';
    const ts = times.map((x) => Date.parse(x) + (bucketS * 1000) / 2);
    c.setOption(
      {
        animation: false,
        textStyle: { color: text },
        grid: { left: 40, right: 12, top: 8, bottom: 24 },
        tooltip: {
          trigger: 'axis',
          axisPointer: { type: 'shadow' },
          formatter: (params: unknown) => {
            const ps = params as {
              dataIndex: number;
              seriesName: string;
              value: [number, number];
              color: string;
            }[];
            if (!ps.length) return '';
            const i = ps[0].dataIndex;
            const head = new Date(Date.parse(times[i])).toLocaleString(locale);
            const rows = ps
              .filter((p) => p.value[1] > 0)
              .map(
                (p) =>
                  `<span style="color:${p.color}">●</span> ${p.seriesName}: <b>${p.value[1]}</b>`,
              );
            return [head, ...rows].join('<br/>');
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
            formatter: {
              day: '{d}/{M}',
              hour: '{HH}:{mm}',
              minute: '{HH}:{mm}',
              none: '{d}/{M} {HH}:{mm}',
            },
          },
          splitLine: { show: false },
        },
        yAxis: {
          type: 'value',
          minInterval: 1,
          axisLabel: { color: text },
          splitLine: { lineStyle: { color: grid } },
        },
        series: series.map((s) => ({
          name: s.name,
          type: 'bar',
          stack: 'all',
          barMaxWidth: 24,
          itemStyle: { color: s.color },
          data: s.data.map((v, i) => [ts[i], v]),
        })),
      },
      true,
    );
  }, [times, series, bucketS, from, to, dark, i18n.language]);

  return <div ref={ref} className="h-32 w-full" data-testid="logs-histogram" />;
}

/** Syslog stats buckets → 8 stacked series (one per severity, worst first). */
export function severitySeries(
  buckets: { counts: number[] }[],
  label: (sev: number) => string,
): HistogramSeries[] {
  return [0, 1, 2, 3, 4, 5, 6, 7].map((sev) => ({
    name: label(sev),
    color: SEVERITY_HEX[severityTone(sev)],
    data: buckets.map((b) => b.counts[sev] ?? 0),
  }));
}
