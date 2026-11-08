// [3] Reports helpers (architecture.md § 13.6, data-model § [3] Reports). Pure.
import type { Language } from '../api/types';
import type { Report, ReportInput, ReportSchedule, ReportSection } from '../api/types3';
import { REPORT_SECTIONS } from '../api/types3';

export type ReportForm = {
  name: string;
  enabled: boolean;
  language: Language;
  scope_group_ids: number[];
  scope_device_ids: number[];
  sections: ReportSection[];
  schedule: ReportSchedule;
  weekday: number;
  month_day: number;
  send_time: string;
  email_to: string;
  channel_ids: number[];
};

export function reportFormFrom(r: Report | null | undefined, lang: Language): ReportForm {
  return {
    name: r?.name ?? '',
    enabled: r?.enabled ?? true,
    language: r?.language ?? lang,
    scope_group_ids: r?.scope_group_ids ?? [],
    scope_device_ids: r?.scope_device_ids ?? [],
    sections: r?.sections ?? ['availability', 'top_alerts'],
    schedule: r?.schedule ?? 'weekly',
    weekday: r?.weekday ?? 1,
    month_day: r?.month_day ?? 1,
    send_time: r?.send_time ?? '07:47',
    email_to: (r?.email_to ?? []).join('\n'),
    channel_ids: r?.channel_ids ?? [],
  };
}

export function parseEmails(text: string): string[] {
  return [
    ...new Set(
      text
        .split(/[\s,;]+/)
        .map((x) => x.trim())
        .filter(Boolean),
    ),
  ];
}
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const HHMM_RE = /^([01]\d|2[0-3]):[0-5]\d$/;

/** i18n keys of the problems (422 rules of data-model § reports). */
export function reportFormErrors(f: ReportForm): string[] {
  const errs: string[] = [];
  const name = f.name.trim();
  if (!name || name.length > 128) errs.push('reports.err.name');
  if (!f.sections.length) errs.push('reports.err.sections');
  if (f.schedule === 'weekly' && !(f.weekday >= 1 && f.weekday <= 7))
    errs.push('reports.err.weekday');
  if (f.schedule === 'monthly' && !(f.month_day >= 1 && f.month_day <= 28))
    errs.push('reports.err.monthDay');
  if (f.schedule !== 'none' && !HHMM_RE.test(f.send_time)) errs.push('reports.err.time');
  const emails = parseEmails(f.email_to);
  if (emails.length > 50) errs.push('reports.err.tooManyEmails');
  if (emails.some((e) => !EMAIL_RE.test(e))) errs.push('reports.err.email');
  return errs;
}

export function reportFormToInput(f: ReportForm): ReportInput {
  const weekly = f.schedule === 'weekly';
  const monthly = f.schedule === 'monthly';
  return {
    name: f.name.trim(),
    enabled: f.enabled,
    language: f.language,
    scope_group_ids: f.scope_group_ids,
    scope_device_ids: f.scope_device_ids,
    sections: f.sections.filter((s, i, a) => REPORT_SECTIONS.includes(s) && a.indexOf(s) === i),
    schedule: f.schedule,
    weekday: weekly ? f.weekday : null,
    month_day: monthly ? f.month_day : null,
    send_time: weekly || monthly ? f.send_time : null,
    email_to: parseEmails(f.email_to),
    channel_ids: f.channel_ids,
  };
}

/** Moves an item one step up (-1) or down (+1); out-of-range moves return the same order. */
export function moveItem<T>(arr: T[], index: number, dir: -1 | 1): T[] {
  const j = index + dir;
  if (index < 0 || index >= arr.length || j < 0 || j >= arr.length) return arr;
  const out = arr.slice();
  [out[index], out[j]] = [out[j], out[index]];
  return out;
}

/** Toggles a section, keeping the canonical order for newly added ones at the end. */
export function toggleSection(
  list: ReportSection[],
  s: ReportSection,
  on: boolean,
): ReportSection[] {
  if (on) return list.includes(s) ? list : [...list, s];
  return list.filter((x) => x !== s);
}

const pad = (n: number) => String(n).padStart(2, '0');
/** `YYYY-MM-DD` of a local date. */
export function localDate(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/**
 * Default "Run now" range in local dates [start, end): the last full week (Mon–Mon) for weekly,
 * the last full month for monthly, else the last 7 days ending today 00:00.
 */
export function defaultPeriod(schedule: ReportSchedule, now: Date): { start: string; end: string } {
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  if (schedule === 'monthly') {
    const end = new Date(today.getFullYear(), today.getMonth(), 1);
    const start = new Date(end.getFullYear(), end.getMonth() - 1, 1);
    return { start: localDate(start), end: localDate(end) };
  }
  if (schedule === 'weekly') {
    const dow = (today.getDay() + 6) % 7; // Monday = 0
    const end = new Date(today.getFullYear(), today.getMonth(), today.getDate() - dow);
    const start = new Date(end.getFullYear(), end.getMonth(), end.getDate() - 7);
    return { start: localDate(start), end: localDate(end) };
  }
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 7);
  return { start: localDate(start), end: localDate(today) };
}

/** Local `YYYY-MM-DD` → ISO of local midnight; null when invalid. */
export function dateToIso(d: string): string | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(d);
  if (!m) return null;
  const dt = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return Number.isNaN(dt.getTime()) ? null : dt.toISOString();
}

export const MAX_PERIOD_DAYS = 92;
/** null when OK, else an i18n key: start < end, ≤ 92 days. */
export function periodError(start: string, end: string): string | null {
  const a = dateToIso(start);
  const b = dateToIso(end);
  if (!a || !b) return 'reports.err.period';
  const ms = Date.parse(b) - Date.parse(a);
  if (ms <= 0) return 'reports.err.periodOrder';
  if (ms > MAX_PERIOD_DAYS * 86_400_000 + 3_600_000) return 'reports.err.periodLong';
  return null;
}

/** "99.40 %" with two decimals; null → null (shown as n/a). */
export function formatAvailability(v: number | null | undefined): string | null {
  if (v === null || v === undefined || Number.isNaN(v)) return null;
  return `${v.toFixed(2)}%`;
}

export function availabilityTone(v: number | null | undefined): 'ok' | 'warn' | 'bad' | 'na' {
  if (v === null || v === undefined) return 'na';
  if (v >= 99.9) return 'ok';
  if (v >= 99) return 'warn';
  return 'bad';
}

/** Width of an HTML/CSS bar (0–100). */
export function barPct(v: number, max: number): number {
  if (!max || max <= 0 || !Number.isFinite(v)) return 0;
  return Math.max(0, Math.min(100, (v / max) * 100));
}

/** Delivery target as text: "email:ops@x" → address, "channel:4" → the channel's name. */
export function deliveryLabel(
  target: string,
  channelName: (id: number) => string | undefined,
): string {
  if (target.startsWith('email:')) return target.slice(6);
  if (target.startsWith('channel:')) {
    const id = Number(target.slice(8));
    return channelName(id) ?? `#${id}`;
  }
  return target;
}
