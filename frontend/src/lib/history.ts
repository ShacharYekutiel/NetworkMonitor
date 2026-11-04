import type { HistoryPoint } from '../api/types';

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
