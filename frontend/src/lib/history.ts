import type { Status, HistoryPoint } from '../api/types';

export type SeriesPoint = [number, number | null];

/**
 * Converts history points into chart series. A null point is inserted wherever `t` jumps by
 * more than 2 × bucket_s, so the chart draws a gap (api.md § History).
 */
export function toSeries(points: HistoryPoint[], bucketS: number) {
  const avg: SeriesPoint[] = [];
  const low: SeriesPoint[] = [];
  const band: SeriesPoint[] = [];
  let prev: number | null = null;
  const gap = 2 * bucketS * 1000;
  for (const p of points) {
    const t = Date.parse(p.t);
    if (Number.isNaN(t)) continue;
    if (prev !== null && t - prev > gap) {
      const mid = prev + bucketS * 1000;
      avg.push([mid, null]);
      low.push([mid, null]);
      band.push([mid, null]);
    }
    avg.push([t, p.avg]);
    low.push([t, p.min]);
    band.push([t, p.max - p.min]);
    prev = t;
  }
  return { avg, low, band };
}

/** [2b-1] Status-change markers for the history chart (inside [from, to], oldest first). */
export function eventMarkers(
  events: { kind: string; time: string; new_status: Status | null; old_status: Status | null }[],
  from: string,
  to: string,
): { t: number; status: Status; old: Status | null }[] {
  const lo = Date.parse(from);
  const hi = Date.parse(to);
  return events
    .filter((e) => e.kind === 'status_change' && e.new_status)
    .map((e) => ({ t: Date.parse(e.time), status: e.new_status as Status, old: e.old_status }))
    .filter((m) => Number.isFinite(m.t) && m.t >= lo && m.t <= hi)
    .sort((a, b) => a.t - b.t);
}
