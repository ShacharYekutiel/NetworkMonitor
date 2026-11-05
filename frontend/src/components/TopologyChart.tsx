// [2b-1] ECharts graph series for the topology map (no extra library).
import { useEffect, useRef } from 'react';
import * as echarts from 'echarts/core';
import { GraphChart } from 'echarts/charts';
import { TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import { useTranslation } from 'react-i18next';
import type { LayoutPosition, TopologyGraph } from '../api/types';
import { buildGraph, forceRepulsion, type GraphNodeDatum } from '../lib/topology';

echarts.use([GraphChart, TooltipComponent, CanvasRenderer]);

const esc = (s: string) =>
  s.replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!,
  );

type ItemLayoutData = { getItemLayout(i: number): [number, number] | null; count(): number };
type ModelAccess = {
  getModel(): { getSeriesByIndex(i: number): { getData(): ItemLayoutData } | undefined };
};

/** Current node positions of the chart's graph series (data coordinates). */
function readPositions(c: echarts.ECharts, ids: string[]): Record<string, [number, number]> {
  const out: Record<string, [number, number]> = {};
  const data = (c as unknown as ModelAccess).getModel().getSeriesByIndex(0)?.getData();
  if (!data) return out;
  for (let i = 0; i < ids.length && i < data.count(); i++) {
    const p = data.getItemLayout(i);
    if (p && Number.isFinite(p[0]) && Number.isFinite(p[1]))
      out[ids[i]] = [Math.round(p[0] * 10) / 10, Math.round(p[1] * 10) / 10];
  }
  return out;
}

export function TopologyChart({
  graph,
  dark,
  showTraffic,
  canSave,
  positions,
  onPositions,
  onSave,
  onNodeClick,
  onLinkClick,
  onBackgroundClick,
  resetToken,
}: {
  graph: TopologyGraph;
  dark: boolean;
  showTraffic: boolean;
  /** admin: positions are persisted (PUT layout) */
  canSave: boolean;
  /** local positions (drags and the one-off force layout) overriding the API x/y */
  positions: Record<string, [number, number]>;
  onPositions: (p: Record<string, [number, number]>) => void;
  onSave: (p: LayoutPosition[]) => void;
  onNodeClick: (n: GraphNodeDatum) => void;
  onLinkClick: (linkId: number) => void;
  onBackgroundClick: () => void;
  /** changes when the layout was reset: forces a fresh force layout */
  resetToken: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const idsRef = useRef<string[]>([]);
  const nodesRef = useRef<GraphNodeDatum[]>([]);
  const cb = useRef({
    onPositions,
    onSave,
    onNodeClick,
    onLinkClick,
    onBackgroundClick,
    canSave,
    positions,
  });
  cb.current = {
    onPositions,
    onSave,
    onNodeClick,
    onLinkClick,
    onBackgroundClick,
    canSave,
    positions,
  };
  const { t } = useTranslation();

  useEffect(() => {
    if (!ref.current) return;
    const c = echarts.init(ref.current, undefined, { renderer: 'canvas' });
    chart.current = c;
    const ro = new ResizeObserver(() => c.resize());
    ro.observe(ref.current);
    c.on('click', (p) => {
      const params = p as { dataType?: string; data?: unknown };
      if (params.dataType === 'node') cb.current.onNodeClick(params.data as GraphNodeDatum);
      else if (params.dataType === 'edge')
        cb.current.onLinkClick((params.data as { linkId: number }).linkId);
    });
    c.getZr().on('click', (e) => {
      if (!e.target) cb.current.onBackgroundClick();
    });
    // Dragging ends on mouseup: keep the node where it was dropped, and save it (admins only).
    c.on('mouseup', (p) => {
      const params = p as { dataType?: string; dataIndex: number };
      if (params.dataType !== 'node') return;
      const id = idsRef.current[params.dataIndex];
      const now = readPositions(c, idsRef.current)[id];
      if (!id || !now) return;
      const prev = cb.current.positions[id];
      if (prev && Math.abs(prev[0] - now[0]) < 0.5 && Math.abs(prev[1] - now[1]) < 0.5) return;
      cb.current.onPositions({ ...cb.current.positions, [id]: now });
      if (cb.current.canSave) cb.current.onSave([{ node: id, x: now[0], y: now[1] }]);
    });
    return () => {
      ro.disconnect();
      c.dispose();
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    const built = buildGraph(graph, { dark, showTraffic, positions });
    idsRef.current = built.nodes.map((n) => n.id);
    nodesRef.current = built.nodes;
    const text = dark ? '#e2e8f0' : '#1e293b';
    const series = (layout: 'force' | 'none', nodes: GraphNodeDatum[]) => ({
      id: `topology-${resetToken}`,
      type: 'graph' as const,
      layout,
      roam: true,
      draggable: true,
      scaleLimit: { min: 0.2, max: 5 },
      autoCurveness: true,
      symbolKeepAspect: true,
      edgeSymbol: ['none', 'none'],
      label: {
        show: true,
        position: 'bottom' as const,
        fontSize: 11,
        color: text,
        overflow: 'truncate' as const,
        width: 120,
      },
      edgeLabel: { fontSize: 10, position: 'middle' as const },
      emphasis: { focus: 'adjacency' as const, lineStyle: { width: 4 } },
      force: {
        repulsion: forceRepulsion(nodes.length),
        edgeLength: [90, 170],
        gravity: 0.08,
        friction: 0.6,
        layoutAnimation: false,
      },
      data: nodes,
      links: built.links,
    });
    const base = {
      animation: false,
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'item' as const,
        confine: true,
        formatter: (p: unknown) => {
          const params = p as { dataType?: string; data: Record<string, unknown> };
          if (params.dataType === 'edge') {
            const l = graph.links.find((x) => x.id === params.data.linkId);
            if (!l) return '';
            return `<div dir="ltr">${esc(l.a.port || '?')} ↔ ${esc(l.b.port || '?')}</div>`;
          }
          const d = params.data as unknown as GraphNodeDatum;
          const n = graph.nodes.find((x) => x.key === d.id);
          if (!n) return '';
          if (n.kind === 'device')
            return `<b>${esc(n.name)}</b><br/><span dir="ltr">${esc(n.host)}</span><br/>${esc(t(`status.${n.status}`))}${n.in_maintenance ? ` · ${esc(t('maintenance.badge'))}` : ''}`;
          return `<b>${esc(n.name)}</b><br/>${esc(t('topology.unmanaged'))}${n.mgmt_address ? `<br/><span dir="ltr">${esc(n.mgmt_address)}</span>` : ''}`;
        },
      },
    };
    if (built.needsLayout) {
      // One-off force layout (existing positions stay fixed), then fix it as layout "none".
      c.setOption(
        { ...base, series: [series('force', built.nodes)] },
        { replaceMerge: ['series'] },
      );
      const pos = readPositions(c, idsRef.current);
      const fixed = built.nodes.map((n) =>
        pos[n.id] ? { ...n, x: pos[n.id][0], y: pos[n.id][1], fixed: true } : n,
      );
      c.setOption({ ...base, series: [series('none', fixed)] }, { replaceMerge: ['series'] });
      cb.current.onPositions({ ...cb.current.positions, ...pos });
      if (cb.current.canSave && Object.keys(pos).length)
        cb.current.onSave(Object.entries(pos).map(([node, [x, y]]) => ({ node, x, y })));
    } else {
      c.setOption({ ...base, series: [series('none', built.nodes)] }, { replaceMerge: ['series'] });
    }
  }, [graph, dark, showTraffic, positions, resetToken, t]);

  // The canvas is never mirrored: coordinates are absolute (architecture.md § 10.3 RTL).
  return (
    <div
      dir="ltr"
      ref={ref}
      className="h-[60vh] min-h-[22rem] w-full touch-none"
      role="img"
      aria-label={t('topology.title')}
    />
  );
}
