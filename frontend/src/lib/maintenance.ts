// [2b-1] Maintenance windows: time-zone math, occurrence preview, grouping (pure).
// Mirrors data-model.md § Maintenance windows / Occurrences (Python zoneinfo, fold=0).
import type { MaintenanceWindow, MaintenanceWindowInput, Occurrence } from '../api/types';

const MIN = 60_000;
const DAY = 86_400_000;

type Parts = { y: number; mo: number; d: number; h: number; mi: number; s: number };

const fmtCache = new Map<string, Intl.DateTimeFormat>();
function partsFormatter(tz: string): Intl.DateTimeFormat {
  let f = fmtCache.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      hourCycle: 'h23',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
    fmtCache.set(tz, f);
  }
  return f;
}

/** Wall-clock parts of instant `ms` in `tz`. */
export function zonedParts(ms: number, tz: string): Parts {
  const p: Record<string, number> = {};
  for (const x of partsFormatter(tz).formatToParts(new Date(ms)))
    if (x.type !== 'literal') p[x.type] = Number(x.value);
  return { y: p.year, mo: p.month, d: p.day, h: p.hour % 24, mi: p.minute, s: p.second };
}

/** UTC offset of `tz` at instant `ms`, in minutes (e.g. +120 for CEST). */
export function tzOffsetMin(ms: number, tz: string): number {
  const p = zonedParts(ms, tz);
  const asUtc = Date.UTC(p.y, p.mo - 1, p.d, p.h, p.mi, p.s);
  return Math.round((asUtc - Math.floor(ms / 1000) * 1000) / MIN);
}

/**
 * The instant of a local wall-clock time in `tz`, with Python `zoneinfo` fold=0 semantics:
 * an ambiguous time (DST fall-back) takes the earlier instant, and a time inside a DST gap
 * uses the pre-transition offset (so it lands after the gap, e.g. 02:30 → 03:30 CEST).
 */
export function zonedToUtc(
  y: number,
  mo: number,
  d: number,
  h: number,
  mi: number,
  tz: string,
): number {
  const wall = Date.UTC(y, mo - 1, d, h, mi);
  const pre = tzOffsetMin(wall - DAY, tz);
  const c1 = wall - pre * MIN;
  if (tzOffsetMin(c1, tz) === pre) return c1;
  const post = tzOffsetMin(c1, tz);
  const c2 = wall - post * MIN;
  if (tzOffsetMin(c2, tz) === post) return c2;
  return c1; // gap
}

/** ISO weekday (1 = Monday … 7 = Sunday) of a calendar date. */
export function isoWeekday(y: number, mo: number, d: number): number {
  const wd = new Date(Date.UTC(y, mo - 1, d)).getUTCDay();
  return wd === 0 ? 7 : wd;
}

export function parseHHMM(s: string | null | undefined): { h: number; m: number } | null {
  const m = /^([01]\d|2[0-3]):([0-5]\d)$/.exec((s ?? '').trim());
  return m ? { h: Number(m[1]), m: Number(m[2]) } : null;
}

type Schedule = {
  kind: 'once' | 'weekly';
  starts_at?: string | null;
  ends_at?: string | null;
  weekdays?: number[] | null;
  start_time?: string | null;
  duration_min?: number | null;
};

/** Merges overlapping/adjacent intervals (data-model: "overlaps just merge"). */
function merge(list: [number, number][]): [number, number][] {
  const sorted = [...list].sort((a, b) => a[0] - b[0]);
  const out: [number, number][] = [];
  for (const iv of sorted) {
    const last = out[out.length - 1];
    if (last && iv[0] <= last[1]) last[1] = Math.max(last[1], iv[1]);
    else out.push([iv[0], iv[1]]);
  }
  return out;
}

/**
 * Occurrences of a window that end after `fromMs` and start before `toMs` (a current
 * occurrence is included), at most `limit`. Same rules as the backend's `occurrences()`.
 */
export function occurrences(
  w: Schedule,
  tz: string,
  fromMs: number,
  toMs: number,
  limit = 100,
): Occurrence[] {
  let ivs: [number, number][] = [];
  if (w.kind === 'once') {
    const s = Date.parse(w.starts_at ?? '');
    const e = Date.parse(w.ends_at ?? '');
    if (Number.isFinite(s) && Number.isFinite(e) && s < e) ivs.push([s, e]);
  } else {
    const t = parseHHMM(w.start_time);
    const days = (w.weekdays ?? []).filter((x) => x >= 1 && x <= 7);
    const dur = w.duration_min ?? 0;
    if (!t || !days.length || dur < 1) return [];
    // Start a few days early so an occurrence that began before `from` (up to 7 days long) is
    // still found; iterate local calendar dates.
    const start = zonedParts(fromMs - dur * MIN - DAY, tz);
    let cursor = Date.UTC(start.y, start.mo - 1, start.d);
    const endCursor = toMs + 2 * DAY;
    while (cursor <= endCursor) {
      const cd = new Date(cursor);
      const y = cd.getUTCFullYear();
      const mo = cd.getUTCMonth() + 1;
      const d = cd.getUTCDate();
      if (days.includes(isoWeekday(y, mo, d))) {
        const s = zonedToUtc(y, mo, d, t.h, t.m, tz);
        ivs.push([s, s + dur * MIN]);
      }
      cursor += DAY;
    }
  }
  ivs = merge(ivs).filter(([s, e]) => e > fromMs && s < toMs);
  return ivs.slice(0, limit).map(([s, e]) => ({ start: iso(s), end: iso(e) }));
}

export function iso(ms: number): string {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');
}

const pad = (n: number) => String(n).padStart(2, '0');

/** ISO instant → `datetime-local` value ("YYYY-MM-DDTHH:mm") in `tz`. */
export function toLocalInput(isoStr: string | null | undefined, tz: string): string {
  if (!isoStr) return '';
  const ms = Date.parse(isoStr);
  if (!Number.isFinite(ms)) return '';
  const p = zonedParts(ms, tz);
  return `${p.y}-${pad(p.mo)}-${pad(p.d)}T${pad(p.h)}:${pad(p.mi)}`;
}

/** `datetime-local` value interpreted in `tz` → ISO UTC ("…Z"), or null when invalid. */
export function fromLocalInput(v: string, tz: string): string | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(v.trim());
  if (!m) return null;
  return iso(zonedToUtc(+m[1], +m[2], +m[3], +m[4], +m[5], tz));
}

/** Formats an instant in `tz` (date + time, short). */
export function formatInTz(
  isoStr: string | null | undefined,
  tz: string,
  locale: string,
  opts: Intl.DateTimeFormatOptions = { dateStyle: 'medium', timeStyle: 'short' },
): string {
  if (!isoStr) return '—';
  const ms = Date.parse(isoStr);
  if (!Number.isFinite(ms)) return '—';
  try {
    return new Intl.DateTimeFormat(locale, { ...opts, timeZone: tz }).format(ms);
  } catch {
    return new Intl.DateTimeFormat(locale, opts).format(ms);
  }
}

/** Localised short weekday names, index 1..7 = Mon..Sun. */
export function weekdayNames(locale: string, style: 'short' | 'long' = 'short'): string[] {
  const f = new Intl.DateTimeFormat(locale, { weekday: style, timeZone: 'UTC' });
  // 2024-01-01 is a Monday.
  return ['', ...[1, 2, 3, 4, 5, 6, 7].map((i) => f.format(Date.UTC(2024, 0, i)))];
}

/** "Sun, Sat" (in the locale's names, Monday-first order). */
export function weekdaysText(days: number[], locale: string): string {
  const names = weekdayNames(locale);
  return [...days]
    .sort((a, b) => a - b)
    .map((d) => names[d])
    .join(', ');
}

export type WindowGroups = {
  active: MaintenanceWindow[];
  upcoming: MaintenanceWindow[];
  recurring: MaintenanceWindow[];
  past: MaintenanceWindow[];
};

/** Groups windows for the list: Active now / Upcoming (once) / Recurring (weekly) / Past. */
export function groupWindows(list: MaintenanceWindow[], nowMs: number): WindowGroups {
  const g: WindowGroups = { active: [], upcoming: [], recurring: [], past: [] };
  for (const w of list) {
    if (w.active_now) g.active.push(w);
    else if (w.kind === 'weekly') g.recurring.push(w);
    else if (w.ends_at && Date.parse(w.ends_at) > nowMs) g.upcoming.push(w);
    else g.past.push(w);
  }
  const byStart = (a: MaintenanceWindow, b: MaintenanceWindow) =>
    (Date.parse(a.occurrence_start ?? a.starts_at ?? '') || Infinity) -
      (Date.parse(b.occurrence_start ?? b.starts_at ?? '') || Infinity) || a.id - b.id;
  g.upcoming.sort(byStart);
  g.recurring.sort(byStart);
  g.past.sort((a, b) => Date.parse(b.ends_at ?? '') - Date.parse(a.ends_at ?? '') || b.id - a.id);
  return g;
}

// ---------------------------------------------------------------------------
// Form mapping

export type MaintenanceForm = {
  name: string;
  enabled: boolean;
  scope: MaintenanceWindow['scope'];
  group_id: number | null;
  device_id: number | null;
  kind: MaintenanceWindow['kind'];
  /** datetime-local in settings.timezone */
  starts_local: string;
  ends_local: string;
  weekdays: number[];
  start_time: string;
  duration_min: string;
  comment: string;
};

export function maintenanceForm(
  w: MaintenanceWindow | null,
  tz: string,
  defaults: Partial<MaintenanceForm> = {},
): MaintenanceForm {
  return {
    name: w?.name ?? '',
    enabled: w?.enabled ?? true,
    scope: w?.scope ?? 'global',
    group_id: w?.group_id ?? null,
    device_id: w?.device_id ?? null,
    kind: w?.kind ?? 'once',
    starts_local: toLocalInput(w?.starts_at, tz),
    ends_local: toLocalInput(w?.ends_at, tz),
    weekdays: w?.weekdays ?? [],
    start_time: w?.start_time ?? '02:00',
    duration_min: w?.duration_min != null ? String(w.duration_min) : '60',
    comment: w?.comment ?? '',
    ...defaults,
  };
}

export type MaintenanceErrors = Partial<
  Record<'name' | 'target' | 'starts' | 'ends' | 'weekdays' | 'start_time' | 'duration', string>
>;

/** Builds the create/PATCH body (all scope/kind fields, other ids null). Errors are i18n keys. */
export function maintenanceToInput(
  f: MaintenanceForm,
  tz: string,
): { body: MaintenanceWindowInput; errors: MaintenanceErrors } {
  const errors: MaintenanceErrors = {};
  if (!f.name.trim()) errors.name = 'validation.required';
  const body: MaintenanceWindowInput = {
    name: f.name.trim(),
    enabled: f.enabled,
    scope: f.scope,
    group_id: f.scope === 'group' ? f.group_id : null,
    device_id: f.scope === 'device' ? f.device_id : null,
    kind: f.kind,
    comment: f.comment,
  };
  if (f.scope === 'group' && f.group_id === null) errors.target = 'validation.required';
  if (f.scope === 'device' && f.device_id === null) errors.target = 'validation.required';
  if (f.kind === 'once') {
    const s = fromLocalInput(f.starts_local, tz);
    const e = fromLocalInput(f.ends_local, tz);
    if (!s) errors.starts = 'validation.required';
    if (!e) errors.ends = 'validation.required';
    if (s && e) {
      const span = Date.parse(e) - Date.parse(s);
      if (span <= 0) errors.ends = 'maintenance.err.endBeforeStart';
      else if (span > 366 * DAY) errors.ends = 'maintenance.err.tooLong';
    }
    Object.assign(body, {
      starts_at: s,
      ends_at: e,
      weekdays: null,
      start_time: null,
      duration_min: null,
    });
  } else {
    const days = [...new Set(f.weekdays)].filter((d) => d >= 1 && d <= 7).sort((a, b) => a - b);
    if (!days.length) errors.weekdays = 'maintenance.err.weekdays';
    if (!parseHHMM(f.start_time)) errors.start_time = 'maintenance.err.time';
    const dur = /^\d+$/.test(f.duration_min.trim()) ? Number(f.duration_min) : NaN;
    if (!(dur >= 1 && dur <= 10080)) errors.duration = 'maintenance.err.duration';
    Object.assign(body, {
      starts_at: null,
      ends_at: null,
      weekdays: days,
      start_time: f.start_time.trim(),
      duration_min: Number.isFinite(dur) ? dur : null,
    });
  }
  return { body, errors };
}

/** The browser's IANA zone (sent by the setup wizard). */
export function browserTimeZone(): string | undefined {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || undefined;
  } catch {
    return undefined;
  }
}

/** IANA zones for the settings select (falls back to a short list on old browsers). */
export function timeZones(current?: string): string[] {
  let list: string[] = [];
  try {
    const f = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] })
      .supportedValuesOf;
    if (f) list = f('timeZone');
  } catch {
    list = [];
  }
  if (!list.length)
    list = ['UTC', 'Asia/Jerusalem', 'Europe/London', 'Europe/Berlin', 'America/New_York'];
  if (!list.includes('UTC')) list = ['UTC', ...list];
  if (current && !list.includes(current)) list = [current, ...list];
  return list;
}
