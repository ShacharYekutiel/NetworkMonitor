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
    const text = dark ? '#cbd5e1' : '#334155';
    chart.current?.setOption(
      { textStyle: { color: text }, backgroundColor: 'transparent', ...option },
      true,
    );
  }, [option, dark]);
  return <div ref={ref} dir="ltr" className={className} data-testid={testId} />;
}
