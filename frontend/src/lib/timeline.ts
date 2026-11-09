// [2b-3] Device timeline helpers (architecture.md § 12.9). Pure.
import type { TimelineItem, TimelineKind } from '../api/types';

export const TIMELINE_TONE: Record<TimelineKind, string> = {
  status: 'bg-sky-600',
  notification: 'bg-violet-600',
  ack: 'bg-emerald-600',
  maintenance: 'bg-slate-500',
  trap: 'bg-orange-500',
  syslog: 'bg-amber-500',
  port: 'bg-cyan-600',
  audit: 'bg-slate-700',
};

/** Kinds offered as filter chips (trap/syslog/audit only while those features are on). */
export function availableKinds(f: {
  traps: boolean;
  syslog: boolean;
  audit: boolean;
  isAdmin: boolean;
}): TimelineKind[] {
  const out: TimelineKind[] = ['status', 'notification', 'ack', 'maintenance'];
  if (f.traps) out.push('trap');
  if (f.syslog) out.push('syslog');
  out.push('port');
  if (f.audit && f.isAdmin) out.push('audit');
  return out;
}

/** Where a row links to: the sensor page, or (null) an in-place detail (trap/syslog/audit). */
export function timelineHref(item: TimelineItem, deviceId: number): string | null {
  if (item.kind === 'audit') return `/audit?device_id=${deviceId}`;
  if (item.kind === 'trap' || item.kind === 'syslog') return null;
  if (item.sensor_id !== null) return `/sensors/${item.sensor_id}`;
  return null;
}

/** Groups items by local calendar day (keeps order). */
export function groupByDay<T extends { time: string }>(items: T[]): { day: string; items: T[] }[] {
  const out: { day: string; items: T[] }[] = [];
  for (const it of items) {
    const d = new Date(it.time);
    const p = (n: number) => String(n).padStart(2, '0');
    const day = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
    const last = out[out.length - 1];
    if (last && last.day === day) last.items.push(it);
    else out.push({ day, items: [it] });
  }
  return out;
}
