// [1.4.1] ECharts puts the string a tooltip `formatter` returns into `innerHTML`. Every value
// that is not a fixed literal (names, units, translated texts, API strings) goes through
// `escapeHtml` before it is concatenated into such a string.

const ENTITIES: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
  '`': '&#96;',
};

/** Text → HTML text / attribute value (never markup). */
export function escapeHtml(value: unknown): string {
  return String(value ?? '').replace(/[&<>"'`]/g, (c) => ENTITIES[c]);
}

/** A colour for an inline `style`: anything that is not a plain colour token becomes a neutral one. */
export function safeColor(value: unknown): string {
  const s = String(value ?? '');
  return /^(#[0-9a-f]{3,8}|[a-z]{3,20}|rgba?\([\d\s.,%]+\))$/i.test(s) ? s : 'currentColor';
}

/** Tooltip of the sensor history chart (the unit of an `snmp_custom` channel is free text). */
export function historyTooltipHtml(p: {
  when: string;
  avg: string | null;
  min: string;
  max: string;
  labels: { avg: string; min: string; max: string };
}): string {
  if (p.avg === null) return escapeHtml(p.when);
  return (
    `${escapeHtml(p.when)}<br/>${escapeHtml(p.labels.avg)}: <b>${escapeHtml(p.avg)}</b>` +
    `<br/>${escapeHtml(p.labels.min)}: ${escapeHtml(p.min)}` +
    `<br/>${escapeHtml(p.labels.max)}: ${escapeHtml(p.max)}`
  );
}

/** Tooltip of the syslog / trap histogram. */
export function histogramTooltipHtml(
  head: string,
  rows: { name: string; color: string; value: number }[],
): string {
  return [
    escapeHtml(head),
    ...rows.map(
      (r) =>
        `<span style="color:${safeColor(r.color)}">●</span> ${escapeHtml(r.name)}: <b>${escapeHtml(r.value)}</b>`,
    ),
  ].join('<br/>');
}

/** Tooltip of the assistant usage chart (the currency is a free-text setting). */
export function usageTooltipHtml(p: {
  day: string;
  tokens: string;
  questions: number;
  cost: string | null;
  labels: { tokens: string; questions: string };
}): string {
  return (
    `${escapeHtml(p.day)}<br/>${escapeHtml(p.labels.tokens)}: <b>${escapeHtml(p.tokens)}</b>` +
    `<br/>${escapeHtml(p.labels.questions)}: ${escapeHtml(p.questions)}` +
    (p.cost !== null ? `<br/>≈ ${escapeHtml(p.cost)}` : '')
  );
}
