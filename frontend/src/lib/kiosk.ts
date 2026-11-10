// [3] NOC kiosk and chat-ack link helpers (architecture.md § 13.7, § 13.8). Pure.
import type { KioskTokenResponse, KioskView, KioskWidget } from '../api/types3';
import { parseSources } from './ingestSettings';

export const KIOSK_TOKEN_KEY = 'netmon.kiosk.token';
export const ACK_TOKEN_KEY = 'netmon.ack.token';

/** `#nmk_…` → the token (`nmk_` + 43 url-safe chars), else null. */
export function kioskTokenFromHash(hash: string): string | null {
  const v = hash.replace(/^#/, '').trim();
  return /^nmk_[A-Za-z0-9_-]{20,64}$/.test(v) ? v : null;
}

/** `#a1.…` → the ack token (`a1.` + 38 base64url chars = 41), else null. */
export function ackTokenFromHash(hash: string): string | null {
  const v = hash.replace(/^#/, '').trim();
  return /^a1\.[A-Za-z0-9_-]{38}$/.test(v) ? v : null;
}

/** The kiosk URL to show once: the server's, else built from the current origin. */
export function kioskUrl(r: Pick<KioskTokenResponse, 'token' | 'url'>, origin: string): string {
  return r.url ?? `${origin.replace(/\/$/, '')}/noc#${r.token}`;
}

/** Allowed sources: 1–64 IPs/CIDRs. Returns the list or an i18n error key. */
export function parseAllowedSources(text: string): { list: string[]; error: string | null } {
  const r = parseSources(text, 64);
  if (!r.list.length) return { list: r.list, error: 'kiosk.err.sourcesRequired' };
  if (r.tooMany) return { list: r.list, error: 'kiosk.err.sourcesTooMany' };
  if (r.invalid.length) return { list: r.list, error: 'kiosk.err.sourcesInvalid' };
  return { list: r.list, error: null };
}

export function refreshError(v: string): string | null {
  const s = v.trim();
  if (!/^\d+$/.test(s)) return 'kiosk.err.refresh';
  const n = Number(s);
  return n >= 10 && n <= 300 ? null : 'kiosk.err.refresh';
}

/** A stale banner after 3 × refresh_s without fresh data. */
export function isStale(lastOkMs: number | null, refreshS: number, now: number): boolean {
  if (lastOkMs === null) return false;
  return now - lastOkMs > 3 * refreshS * 1000;
}

/** Widgets in configured order that have data (null = not configured). */
export function visibleWidgets(v: KioskView): KioskWidget[] {
  return v.screen.widgets.filter((w) => v[w] !== null && v[w] !== undefined);
}

/** Reconnect delay of the kiosk WS (1, 2, 5, 10, 30 s). */
export function kioskBackoff(attempt: number): number {
  const steps = [1000, 2000, 5000, 10000, 30000];
  return steps[Math.min(attempt, steps.length - 1)];
}

export function kioskWsUrl(loc: Pick<Location, 'protocol' | 'host'>): string {
  return `${loc.protocol === 'https:' ? 'wss:' : 'ws:'}//${loc.host}/ws/kiosk`;
}

/** Map node positions normalised to 0–1 (nodes without a saved position are laid out on a grid). */
export function normalizePositions(
  nodes: { id: number; x: number | null; y: number | null }[],
): Map<number, { x: number; y: number }> {
  const out = new Map<number, { x: number; y: number }>();
  const placed = nodes.filter((n) => n.x !== null && n.y !== null);
  const xs = placed.map((n) => n.x as number);
  const ys = placed.map((n) => n.y as number);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  for (const n of placed)
    out.set(n.id, {
      x: maxX > minX ? ((n.x as number) - minX) / (maxX - minX) : 0.5,
      y: maxY > minY ? ((n.y as number) - minY) / (maxY - minY) : 0.5,
    });
  const rest = nodes.filter((n) => n.x === null || n.y === null);
  const cols = Math.max(1, Math.ceil(Math.sqrt(rest.length)));
  rest.forEach((n, i) => {
    const c = i % cols;
    const r = Math.floor(i / cols);
    const rows = Math.max(1, Math.ceil(rest.length / cols));
    out.set(n.id, {
      x: cols > 1 ? c / (cols - 1) : 0.5,
      y: rows > 1 ? r / (rows - 1) : 0.5,
    });
  });
  return out;
}
