// [2b-1] ECharts graph series for the topology map (no extra library).
import { useEffect, useRef, useState } from 'react';
import * as echarts from 'echarts/core';
import { GraphChart } from 'echarts/charts';
import { TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import { flatTooltip, initChart } from '../lib/echarts';
import { useTranslation } from 'react-i18next';
import { escapeHtml } from '../lib/html';
import type { LayoutPosition, TopologyGraph } from '../api/types';
import { buildGraph, isRealDrag, type GraphNodeDatum } from '../lib/topology';
import {
  canvasLabelMeasurer,
  completeLayout,
  FOOT_BOTTOM,
  FOOT_TOP,
  groupCaptions,
  quantAspect,
  TILE,
  type GroupInfo,
  type LayoutLink,
  type LayoutNode,
} from '../lib/topologyLayout';
import { chartTokens } from '../lib/chartTokens';
import { cx } from './ui';

echarts.use([GraphChart, TooltipComponent, CanvasRenderer]);

const esc = escapeHtml; // [1.4.1] shared helper (lib/html.ts)

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

type View = { box: [number, number]; centre: [number, number]; span: [number, number] };

/** Zoom that shows the whole picture in a `w`×`h` canvas (never above 1.2); the top and bottom
 * 56px stay free for the "Fit" button and the hint line. */
function fitZoom(v: View, w: number, h: number): number {
  if (!w || !h) return 1;
  const z = Math.min((w - 48) / v.span[0], (h - 112) / v.span[1]);
  return Math.max(0.1, Math.min(1.2, Math.round(z * 1000) / 1000));
}

/** Names are drawn at 12px × zoom; below this zoom they would be unreadable and are hidden. */
const MIN_LABEL_ZOOM = 0.45;
const quantZoom = (z: number) => Math.round(z * 20) / 20;
function currentZoom(c: echarts.ECharts): number {
  const opt = c.getOption() as { series?: { zoom?: number }[] };
  return opt.series?.[0]?.zoom ?? 1;
}

/** Test hook (E2E): node centres in canvas pixels and the tile size at the current zoom. */
export type TopologyProbe = {
  zoom: number;
  /** visible tile size in px at the current zoom */
  tile: number;
  /** centre in canvas px (x, y) and the position in data units (pos) */
  nodes: { key: string; x: number; y: number; pos: [number, number] }[];
};
declare global {
  interface Window {
    __nmTopology?: () => TopologyProbe | null;
  }
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
  fitToken = 0,
  groups,
  selected,
  className,
}: {
  graph: TopologyGraph;
  dark: boolean;
  showTraffic: boolean;
  /** admin: positions are persisted (PUT layout) */
  canSave: boolean;
  /** local positions (drags and the default arrangement) overriding the API x/y */
  positions: Record<string, [number, number]>;
  onPositions: (p: Record<string, [number, number]>) => void;
  onSave: (p: LayoutPosition[]) => void;
  onNodeClick: (n: GraphNodeDatum) => void;
  onLinkClick: (linkId: number) => void;
  onBackgroundClick: () => void;
  /** changes when the layout was reset: a fresh series */
  resetToken: number;
  /** changes when the whole picture should be fitted into the canvas again */
  fitToken?: number;
  /** group names and tree order: unlinked devices are arranged and captioned by group */
  groups?: ReadonlyMap<number, GroupInfo>;
  /** key of the selected node (inverted tile) */
  selected?: string | null;
  /** size of the canvas (default: 60% of the viewport height, at least 22rem) */
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const idsRef = useRef<string[]>([]);
  const viewRef = useRef<View | null>(null);
  /** the fitToken last applied (null: never fitted) */
  const fittedRef = useRef<number | null>(null);
  /** the user zoomed or panned since the last fit (a resize then keeps their view) */
  const roamedRef = useRef(false);
  /** the zoom in steps of 0.05: names and captions are redrawn at that size */
  const [zoomStep, setZoomStep] = useState(1);
  const downRef = useRef<{
    id: string;
    px: [number, number];
    pos: [number, number] | null;
  } | null>(null);
  /** A node is held (drag in progress). Re-applying the series now would put the node back to
   * its stored position and end the drag (a live update — any sensor result changes the tree —
   * lands mid-drag on a busy system), so option updates wait until the pointer is released. */
  const heldRef = useRef(false);
  const deferredRef = useRef(false);
  const [releaseTick, setReleaseTick] = useState(0);
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
    const c = initChart(ref.current);
    chart.current = c;
    const ro = new ResizeObserver(() => {
      c.resize();
      // Until the user zooms or pans, the picture follows the canvas size (menu pinned, window).
      const v = viewRef.current;
      if (v && !roamedRef.current) {
        const zoom = fitZoom(v, c.getWidth(), c.getHeight());
        c.setOption({ series: [{ zoom, center: v.centre }] });
        setZoomStep(quantZoom(zoom));
      }
    });
    ro.observe(ref.current);
    c.on('graphroam', () => {
      roamedRef.current = true;
      setZoomStep(quantZoom(currentZoom(c)));
    });
    c.on('click', (p) => {
      const params = p as { dataType?: string; data?: unknown };
      if (params.dataType === 'node') {
        const d = params.data as GraphNodeDatum & { caption?: boolean };
        if (!d.caption) cb.current.onNodeClick(d);
      } else if (params.dataType === 'edge')
        cb.current.onLinkClick((params.data as { linkId: number }).linkId);
    });
    c.getZr().on('click', (e) => {
      if (!e.target) cb.current.onBackgroundClick();
    });
    // A drag starts with mousedown on a node: remember where it was (pixels and layout).
    c.on('mousedown', (p) => {
      const params = p as {
        dataType?: string;
        dataIndex: number;
        event?: { offsetX: number; offsetY: number };
      };
      const id = idsRef.current[params.dataIndex];
      if (params.dataType !== 'node' || !params.event || !id) {
        downRef.current = null;
        return;
      }
      downRef.current = {
        id,
        px: [params.event.offsetX, params.event.offsetY],
        pos: readPositions(c, idsRef.current)[id] ?? null,
      };
      heldRef.current = true;
    });
    // The release (anywhere, also outside the canvas) ends the hold once the chart's own mouseup
    // handler below has run (same input task), then applies the updates that waited.
    const release = () => {
      if (!heldRef.current) return;
      setTimeout(() => {
        heldRef.current = false;
        downRef.current = null;
        if (deferredRef.current) {
          deferredRef.current = false;
          setReleaseTick((x) => x + 1);
        }
      });
    };
    window.addEventListener('pointerup', release);
    window.addEventListener('pointercancel', release);
    // Dragging ends on mouseup: keep the node where it was dropped, and save it (admins only).
    // A plain click (≤ 3 px of pointer movement, or no layout change) never saves.
    c.on('mouseup', (p) => {
      const params = p as {
        dataType?: string;
        dataIndex: number;
        event?: { offsetX: number; offsetY: number };
      };
      const down = downRef.current;
      downRef.current = null;
      if (params.dataType !== 'node' || !down || !params.event) return;
      const id = idsRef.current[params.dataIndex];
      if (!id || id !== down.id) return;
      const now = readPositions(c, idsRef.current)[id];
      if (!now || !isRealDrag(down.px, [params.event.offsetX, params.event.offsetY], down.pos, now))
        return;
      cb.current.onPositions({ ...cb.current.positions, [id]: now });
      if (cb.current.canSave) cb.current.onSave([{ node: id, x: now[0], y: now[1] }]);
    });
    window.__nmTopology = () => {
      const ids = idsRef.current;
      const pos = readPositions(c, ids);
      const zoom = currentZoom(c);
      const nodes = ids
        .filter((k) => pos[k])
        .map((key) => {
          const [x, y] = c.convertToPixel({ seriesIndex: 0 }, pos[key]) as number[];
          return { key, x, y, pos: pos[key] };
        });
      return { zoom, tile: 56 * zoom, nodes };
    };
    return () => {
      window.removeEventListener('pointerup', release);
      window.removeEventListener('pointercancel', release);
      ro.disconnect();
      c.dispose();
      chart.current = null;
      delete window.__nmTopology;
    };
  }, []);

  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    if (heldRef.current) {
      deferredRef.current = true;
      return;
    }
    const tk = chartTokens(dark);
    const measure = canvasLabelMeasurer(tk.fontDisplay);
    const keys = new Set(graph.nodes.map((n) => n.key));
    const lnodes: LayoutNode[] = graph.nodes.map((n) => ({
      key: n.key,
      name: n.name,
      kind: n.kind,
      groupId: n.kind === 'device' ? n.group_id : null,
      deviceClass: n.kind === 'device' ? n.device_class : null,
      pos: positions[n.key] ?? (n.x !== null && n.y !== null ? [n.x, n.y] : null),
    }));
    const llinks: LayoutLink[] = graph.links
      .filter((l) => keys.has(l.a.node) && keys.has(l.b.node))
      .map((l) => ({ a: l.a.node, b: l.b.node }));
    const opts = { measure, groups, aspect: quantAspect(c.getWidth() - 48, c.getHeight() - 112) };
    // Nodes without a usable position get one (the default arrangement, or free space near their
    // neighbours); the admin's arrangement is saved for everybody.
    const added = completeLayout(lnodes, llinks, opts);
    const merged = { ...positions, ...added };
    if (Object.keys(added).length) {
      cb.current.onPositions({ ...cb.current.positions, ...added });
      if (cb.current.canSave)
        cb.current.onSave(Object.entries(added).map(([node, [x, y]]) => ({ node, x, y })));
    }
    const built = buildGraph(graph, { dark, showTraffic, positions: merged, selected });
    idsRef.current = built.nodes.map((n) => n.id);
    const all: Record<string, [number, number]> = {};
    for (const n of built.nodes) if (n.x !== undefined && n.y !== undefined) all[n.id] = [n.x, n.y];
    const captions = groupCaptions(lnodes, llinks, all, opts);
    const text = tk.ink;

    // The series box is exactly the bounding box of the data, so 1 data unit = 1 px at zoom 1 and
    // nodes (nodeScaleRatio 1) scale with the zoom: the picture never piles up when fitted.
    const pts = [...Object.values(all), ...captions.map((k) => [k.x, k.y] as [number, number])];
    const xs = pts.map((p) => p[0]);
    const ys = pts.map((p) => p[1]);
    const [x1, x2, y1, y2] = pts.length
      ? [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)]
      : [0, 0, 0, 0];
    const widest = Math.max(TILE, ...lnodes.map((n) => measure(n.name)));
    const view: View = {
      box: [x2 - x1 || 2, y2 - y1 || 2],
      centre: [(x1 + x2) / 2, (y1 + y2) / 2 + (FOOT_BOTTOM - FOOT_TOP) / 2],
      span: [x2 - x1 + widest, y2 - y1 + FOOT_TOP + FOOT_BOTTOM],
    };
    viewRef.current = view;
    const fit = fittedRef.current !== fitToken;
    if (fit) {
      fittedRef.current = fitToken;
      roamedRef.current = false;
    }
    const zoom = fit ? fitZoom(view, c.getWidth(), c.getHeight()) : currentZoom(c);
    if (fit && quantZoom(zoom) !== zoomStep) setZoomStep(quantZoom(zoom));
    // names scale with the tiles (the layout leaves room for them at any zoom)
    const z = Math.max(zoom, 0.1);
    const showNames = zoom >= MIN_LABEL_ZOOM;
    const captionData = captions.map((k) => ({
      id: `cap:${k.groupId}`,
      name: k.name,
      caption: true,
      x: k.x,
      y: k.y,
      fixed: true,
      draggable: false,
      symbol: 'rect',
      symbolSize: [1, 1],
      value: 0,
      itemStyle: { color: 'transparent', borderWidth: 0 },
      tooltip: { show: false },
      emphasis: { disabled: true },
      cursor: 'default',
      label: {
        // group captions stay longer than the names: they orient the overview
        show: zoom >= 0.25,
        position: 'inside' as const,
        formatter: '{b}',
        fontSize: Math.max(13 * z, 10),
        fontWeight: 600,
        fontFamily: tk.fontDisplay,
        color: tk.inkMuted,
        backgroundColor: 'transparent',
        shadowBlur: 0,
        padding: 0,
      },
    }));
    const series = {
      id: `topology-${resetToken}`,
      type: 'graph' as const,
      layout: 'none' as const,
      left: 'center',
      top: 'middle',
      width: view.box[0],
      height: view.box[1],
      ...(fit ? { zoom, center: view.centre } : {}),
      roam: true,
      draggable: true,
      nodeScaleRatio: 1,
      scaleLimit: { min: 0.1, max: 4 },
      autoCurveness: true,
      symbolKeepAspect: true,
      edgeSymbol: ['none', 'none'],
      // The name pill under the tile (prototype .topo .node .nm): Inter 12/500 on surface, a soft
      // shadow; an opaque plate keeps names readable where links cross them.
      label: {
        show: showNames,
        position: 'bottom' as const,
        fontSize: 12 * z,
        fontWeight: 500,
        fontFamily: tk.fontDisplay,
        color: text,
        backgroundColor: tk.surface,
        borderRadius: 999,
        padding: [5 * z, 10 * z],
        shadowBlur: 10 * z,
        shadowOffsetY: 2 * z,
        shadowColor: dark ? 'rgba(0,0,0,0.5)' : 'rgba(0,0,0,0.10)',
        distance: -6 * z,
        rich: {
          n: {
            fontSize: 12 * z,
            fontWeight: 500,
            fontFamily: tk.fontDisplay,
            color: text,
            padding: [0, 6 * z, 0, 0],
          },
        },
      },
      edgeLabel: {
        fontSize: 10,
        fontWeight: 600,
        position: 'middle' as const,
        fontFamily: tk.fontMono,
        color: tk.inkMuted,
        backgroundColor: tk.surface,
        padding: [2, 7],
        borderRadius: 999,
      },
      emphasis: { focus: 'adjacency' as const, lineStyle: { width: 3 } },
      // the status dot in the name pill scales with the names
      data: [
        ...built.nodes.map((n) => {
          const d = n.label.rich?.d;
          return d
            ? { ...n, label: { ...n.label, rich: { d: { ...d, width: 7 * z, height: 7 * z } } } }
            : n;
        }),
        ...captionData,
      ],
      links: built.links,
    };
    c.setOption(
      {
        animation: false,
        backgroundColor: 'transparent',
        tooltip: {
          trigger: 'item' as const,
          confine: true,
          ...flatTooltip(tk),
          textStyle: { color: tk.ink, fontSize: 12, fontFamily: tk.fontSans },
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
        series: [series],
      },
      { replaceMerge: ['series'] },
    );
  }, [
    graph,
    dark,
    showTraffic,
    positions,
    resetToken,
    fitToken,
    groups,
    selected,
    zoomStep,
    t,
    releaseTick,
  ]);

  // The canvas is never mirrored: coordinates are absolute (architecture.md § 10.3 RTL).
  return (
    <div
      dir="ltr"
      ref={ref}
      className={cx(className ?? 'h-[60vh] min-h-[22rem]', 'w-full touch-none')}
      role="img"
      aria-label={t('topology.title')}
    />
  );
}
