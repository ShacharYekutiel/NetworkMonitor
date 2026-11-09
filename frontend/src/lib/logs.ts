// [2b-3] Traps & syslog: pure helpers (filters, live matching, severities, facilities).
import type { CursorQuery, SyslogQuery, TrapQuery } from '../api/endpoints';
import type { SyslogMessage, Trap } from '../api/types';

export const SEVERITIES = [0, 1, 2, 3, 4, 5, 6, 7] as const;
/** RFC 5424 short names (keys `syslogSeverity.N` hold the translated text). */
export const SEVERITY_CODES = [
  'emerg',
  'alert',
  'crit',
  'err',
  'warning',
  'notice',
  'info',
  'debug',
];
export const FACILITIES = Array.from({ length: 24 }, (_, i) => i);
export const FACILITY_CODES = [
  'kern',
  'user',
  'mail',
  'daemon',
  'auth',
  'syslog',
  'lpr',
  'news',
  'uucp',
  'cron',
  'authpriv',
  'ftp',
  'ntp',
  'audit',
  'alert',
  'clock',
  'local0',
  'local1',
  'local2',
  'local3',
  'local4',
  'local5',
  'local6',
  'local7',
];

/** Severity colours (architecture.md § 12.13): 0–2 red-600, 3 orange-500, 4 amber-500, 5–6 sky-600, 7 slate-500. */
export function severityTone(sev: number): 'red' | 'orange' | 'amber' | 'sky' | 'slate' {
  if (sev <= 2) return 'red';
  if (sev === 3) return 'orange';
  if (sev === 4) return 'amber';
  if (sev <= 6) return 'sky';
  return 'slate';
}
export const SEVERITY_HEX: Record<ReturnType<typeof severityTone>, string> = {
  red: '#dc2626',
  orange: '#f97316',
  amber: '#f59e0b',
  sky: '#0284c7',
  slate: '#64748b',
};
export const SEVERITY_BADGE: Record<ReturnType<typeof severityTone>, string> = {
  red: 'bg-red-600 text-white',
  orange: 'bg-orange-500 text-white',
  amber: 'bg-amber-500 text-slate-900',
  sky: 'bg-sky-600 text-white',
  slate: 'bg-slate-500 text-white',
};

export type TimePreset = '15m' | '1h' | '24h' | '7d' | 'custom';
export const TIME_PRESETS: TimePreset[] = ['15m', '1h', '24h', '7d', 'custom'];
const PRESET_MS: Record<Exclude<TimePreset, 'custom'>, number> = {
  '15m': 15 * 60_000,
  '1h': 3_600_000,
  '24h': 86_400_000,
  '7d': 7 * 86_400_000,
};
/** api.md § Cursor pages: a range longer than 31 days → 422 */
export const MAX_RANGE_MS = 31 * 86_400_000;

export type LogFilter = {
  preset: TimePreset;
  /** custom range: datetime-local values (browser local time) */
  from: string;
  to: string;
  deviceId: number | null;
  groupId: number | null;
  unknown: boolean;
  sourceIp: string;
  /** syslog: "and worse" (≤), null = any */
  severityMax: number | null;
  facilities: number[];
  programs: string[];
  hostname: string;
  /** traps: trap OID prefix */
  trapOid: string;
  ruleId: number | null;
  q: string;
};

export function emptyFilter(over: Partial<LogFilter> = {}): LogFilter {
  return {
    preset: '1h',
    from: '',
    to: '',
    deviceId: null,
    groupId: null,
    unknown: false,
    sourceIp: '',
    severityMax: null,
    facilities: [],
    programs: [],
    hostname: '',
    trapOid: '',
    ruleId: null,
    q: '',
    ...over,
  };
}

/** A datetime-local value ("2026-11-08T10:00") → ISO; null when invalid/empty. */
export function localToIso(v: string): string | null {
  if (!v) return null;
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

/** ISO → datetime-local value in the browser's zone. */
export function isoToLocal(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

export type RangeResult =
  | { ok: true; since: string; until?: string }
  | { ok: false; error: 'invalid' | 'order' | 'tooLong' };

/** since/until of a filter; presets are anchored at `now` (until = now, open-ended for live). */
export function rangeOf(f: Pick<LogFilter, 'preset' | 'from' | 'to'>, now: number): RangeResult {
  if (f.preset !== 'custom')
    return { ok: true, since: new Date(now - PRESET_MS[f.preset]).toISOString() };
  const since = localToIso(f.from);
  const until = f.to ? localToIso(f.to) : new Date(now).toISOString();
  if (!since || !until) return { ok: false, error: 'invalid' };
  const a = Date.parse(since);
  const b = Date.parse(until);
  if (b <= a) return { ok: false, error: 'order' };
  if (b - a > MAX_RANGE_MS) return { ok: false, error: 'tooLong' };
  return { ok: true, since, until: f.to ? until : undefined };
}

/** Live tail is possible only for an open-ended range (a preset or a custom range without end). */
export function liveAllowed(f: Pick<LogFilter, 'preset' | 'to'>): boolean {
  return f.preset !== 'custom' || !f.to;
}

const trimQ = (q: string) => {
  const s = q.trim();
  return s ? s.slice(0, 200) : undefined;
};

export function syslogQuery(f: LogFilter, range: { since: string; until?: string }): SyslogQuery {
  return {
    device_id: f.deviceId ?? undefined,
    group_id: f.groupId ?? undefined,
    unknown: f.unknown || undefined,
    source_ip: f.sourceIp.trim() || undefined,
    severity_max: f.severityMax ?? undefined,
    facility: f.facilities.length ? [...f.facilities].sort((a, b) => a - b) : undefined,
    program: f.programs.length ? f.programs : undefined,
    hostname: f.hostname.trim() || undefined,
    rule_id: f.ruleId ?? undefined,
    q: trimQ(f.q),
    since: range.since,
    until: range.until,
  };
}

export function trapQuery(f: LogFilter, range: { since: string; until?: string }): TrapQuery {
  return {
    device_id: f.deviceId ?? undefined,
    group_id: f.groupId ?? undefined,
    unknown: f.unknown || undefined,
    source_ip: f.sourceIp.trim() || undefined,
    trap_oid: f.trapOid.trim().replace(/^\./, '') || undefined,
    rule_id: f.ruleId ?? undefined,
    q: trimQ(f.q),
    since: range.since,
    until: range.until,
  };
}

export function withCursor<T extends object>(
  q: T,
  cursor: string | null | undefined,
  limit = 100,
): T & CursorQuery {
  return { ...q, cursor: cursor ?? undefined, limit };
}

/** Comma / whitespace separated list → distinct, trimmed values. */
export function parseList(text: string): string[] {
  return [
    ...new Set(
      text
        .split(/[\s,;]+/)
        .map((x) => x.trim())
        .filter(Boolean),
    ),
  ];
}

function commonMatch(
  r: { device_id: number | null; source_ip: string; matched_rule_ids: number[] },
  f: LogFilter,
  groupDevices: ReadonlySet<number> | null,
): boolean {
  if (f.deviceId !== null && r.device_id !== f.deviceId) return false;
  if (f.groupId !== null && (r.device_id === null || !groupDevices?.has(r.device_id))) return false;
  if (f.unknown && r.device_id !== null) return false;
  if (f.sourceIp.trim() && r.source_ip !== f.sourceIp.trim()) return false;
  if (f.ruleId !== null && !r.matched_rule_ids.includes(f.ruleId)) return false;
  return true;
}

const contains = (hay: string, needle: string) =>
  !needle.trim() || hay.toLowerCase().includes(needle.trim().toLowerCase());

/** Client-side filter of live syslog records (same semantics as GET /api/syslog). */
export function matchesSyslog(
  m: SyslogMessage,
  f: LogFilter,
  groupDevices: ReadonlySet<number> | null = null,
): boolean {
  if (!commonMatch(m, f, groupDevices)) return false;
  if (f.severityMax !== null && m.severity > f.severityMax) return false;
  if (f.facilities.length && !f.facilities.includes(m.facility)) return false;
  if (f.programs.length && !f.programs.some((p) => p.toLowerCase() === m.program.toLowerCase()))
    return false;
  if (f.hostname.trim() && m.hostname.toLowerCase() !== f.hostname.trim().toLowerCase())
    return false;
  return contains(m.message, f.q);
}

/** Client-side filter of live traps (same semantics as GET /api/traps). */
export function matchesTrap(
  t: Trap,
  f: LogFilter,
  groupDevices: ReadonlySet<number> | null = null,
): boolean {
  if (!commonMatch(t, f, groupDevices)) return false;
  const oid = f.trapOid.trim().replace(/^\./, '');
  if (oid && !t.trap_oid.startsWith(oid)) return false;
  return contains(t.summary, f.q);
}

/** Prepends new records (newest first), dropping duplicates and keeping at most `max`. */
export function mergeNewest<T extends { id: number; time: string }>(
  incoming: T[],
  existing: T[],
  max = 1000,
): T[] {
  const seen = new Set(existing.map((x) => x.id));
  const fresh = incoming.filter((x) => !seen.has(x.id) && (seen.add(x.id), true));
  fresh.sort((a, b) => (a.time === b.time ? b.id - a.id : a.time < b.time ? 1 : -1));
  return [...fresh, ...existing].slice(0, max);
}

/** sysUpTime ticks (1/100 s) → seconds. */
export function ticksToSeconds(ticks: number | null): number | null {
  return ticks === null ? null : Math.floor(ticks / 100);
}

const IPV4 = /^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$/;
/** IPv4 or IPv6 address (no prefix). */
export function isIp(s: string): boolean {
  const v = s.trim();
  if (IPV4.test(v)) return true;
  if (!v.includes(':') || !/^[0-9a-fA-F:.]+$/.test(v)) return false;
  try {
    new URL(`http://[${v}]/`);
    return true;
  } catch {
    return false;
  }
}

/** IP or CIDR (IPv4 /0–32, IPv6 /0–128). */
export function isIpOrCidr(s: string): boolean {
  const [ip, len, extra] = s.trim().split('/');
  if (extra !== undefined || !isIp(ip)) return false;
  if (len === undefined) return true;
  if (!/^\d{1,3}$/.test(len)) return false;
  return Number(len) <= (ip.includes(':') ? 128 : 32);
}

/** Numeric dotted OID (leading dot tolerated). */
export function isOid(s: string): boolean {
  return /^\.?\d+(\.\d+)+$/.test(s.trim());
}

/** Sum of the severity counts of a stats bucket. */
export function bucketTotal(counts: number[]): number {
  return counts.reduce((a, b) => a + b, 0);
}
