import { useEffect, useRef } from 'react';
import * as echarts from 'echarts/core';
import { BarChart, LineChart } from 'echarts/charts';
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { EChartsCoreOption } from 'echarts/core';
import { chartTokens } from '../../lib/chartTokens';
import { useTheme } from '../../hooks/theme';

echarts.use([
  BarChart,
  LineChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  CanvasRenderer,
]);

/** [3] A thin ECharts wrapper (resizes with its box; text colours follow the theme). */
export function EChart({
  option,
  className = 'h-72',
  testId,
}: {
  option: EChartsCoreOption;
  className?: string;
  testId?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const { dark } = useTheme();
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
    // Design tokens for this theme (re-read when `dark` changes).
    const tk = chartTokens(dark);
    c.setOption(
      {
        color: [tk.chart1, tk.chart2, tk.chart3, tk.chart4],
        textStyle: { color: tk.inkMuted, fontFamily: tk.fontSans },
        backgroundColor: 'transparent',
        ...option,
      },
      true,
    );
    // Axis and tooltip styling merged on top (single-axis charts).
    const axis = {
      axisLine: { lineStyle: { color: tk.border } },
      axisLabel: { color: tk.inkSubtle },
      splitLine: { lineStyle: { color: tk.chartGrid } },
    };
    const o = option as { xAxis?: unknown; yAxis?: unknown; tooltip?: unknown };
    c.setOption({
      ...(o.xAxis && !Array.isArray(o.xAxis) ? { xAxis: axis } : {}),
      ...(o.yAxis && !Array.isArray(o.yAxis) ? { yAxis: axis } : {}),
      ...(o.tooltip
        ? {
            tooltip: {
              backgroundColor: tk.surfaceRaised,
              borderColor: tk.border,
              textStyle: { color: tk.ink, fontSize: 12 },
            },
          }
        : {}),
    });
  }, [option, dark]);
  return <div ref={ref} dir="ltr" className={className} data-testid={testId} />;
}
