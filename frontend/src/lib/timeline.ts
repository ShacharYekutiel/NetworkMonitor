// [2b-3] Device timeline helpers (architecture.md § 12.9). Pure.
import type { TimelineItem, TimelineKind } from '../api/types';

/** Kind markers: kinds are not statuses, so they use the neutral and chart tokens. */
export const TIMELINE_TONE: Record<TimelineKind, string> = {
  status: 'bg-ink',
  notification: 'bg-chart-2',
  ack: 'bg-ack',
  maintenance: 'bg-status-paused',
  trap: 'bg-chart-3',
  syslog: 'bg-chart-3',
  port: 'bg-chart-4',
  audit: 'bg-ink-subtle',
  config: 'bg-chart-1',
  new_mac: 'bg-accent',
};

/** Kinds offered as filter chips (trap/syslog/audit only while those features are on). */
export function availableKinds(f: {
  traps: boolean;
  syslog: boolean;
  audit: boolean;
  isAdmin: boolean;
  /** [3] */
  config_backup?: boolean;
  new_device_alerts?: boolean;
}): TimelineKind[] {
  const out: TimelineKind[] = ['status', 'notification', 'ack', 'maintenance'];
  if (f.traps) out.push('trap');
  if (f.syslog) out.push('syslog');
  out.push('port');
  if (f.audit && f.isAdmin) out.push('audit');
  if (f.config_backup) out.push('config');
  if (f.new_device_alerts) out.push('new_mac');
  return out;
}

/** Where a row links to: the sensor page, or (null) an in-place detail (trap/syslog/audit). */
export function timelineHref(item: TimelineItem, deviceId: number): string | null {
  if (item.kind === 'audit') return `/audit?device_id=${deviceId}`;
  // [3] the diff is admin-only (TimelineTab hides the link for viewers)
  if (item.kind === 'config') return `/devices/${deviceId}?tab=config`;
  if (item.kind === 'new_mac') return '/new-devices';
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
