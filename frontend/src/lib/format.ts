import i18n from '../i18n';

function locale(): string {
  return i18n.language === 'he' ? 'he-IL' : 'en-GB';
}

export function formatNumber(v: number, maxFrac = 2): string {
  return new Intl.NumberFormat(locale(), { maximumFractionDigits: maxFrac }).format(v);
}

const SI = ['', 'k', 'M', 'G', 'T', 'P'];
const BIN = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];

/** Formats a channel value with its unit (bit/s and B are scaled). */
export function formatValue(v: number | null | undefined, unit: string): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  switch (unit) {
    case 'bit/s': {
      let i = 0;
      let x = v;
      while (Math.abs(x) >= 1000 && i < SI.length - 1) {
        x /= 1000;
        i++;
      }
      return `${formatNumber(x, x < 10 ? 2 : 1)} ${SI[i]}bit/s`;
    }
    case 'B': {
      let i = 0;
      let x = v;
      while (Math.abs(x) >= 1024 && i < BIN.length - 1) {
        x /= 1024;
        i++;
      }
      return `${formatNumber(x, x < 10 ? 2 : 1)} ${BIN[i]}`;
    }
    case 's':
      return v >= 3600 ? formatDuration(v) : `${formatNumber(v, 1)} s`;
    case 'code':
      return String(Math.round(v));
    case '%':
      return `${formatNumber(v, 1)}%`;
    case 'd':
      return `${formatNumber(v, 1)} ${i18n.t('units.days')}`;
    default:
      // snmp_custom units are free text ("", "clients", "°C"); no trailing space when empty.
      return unit ? `${formatNumber(v)} ${unit}` : formatNumber(v);
  }
}

/**
 * Text of a sensor channel value. `snmp_custom` in string mode stores 1 for a match, so it
 * shows a word instead of the number.
 */
export function sensorValueText(
  s: { type: string; config: Record<string, unknown> },
  key: string,
  value: number | null | undefined,
  unit: string,
): string {
  if (s.type === 'snmp_custom' && s.config.value_type === 'string' && key === 'value') {
    return value === null || value === undefined ? '—' : i18n.t('sensors.textOk');
  }
  return formatValue(value, unit);
}

/**
 * Human duration with the two most significant units, e.g. "3d 4h" / "3 ימים 4 שע׳".
 * Units are i18n plurals (`units.durD|durH|durM`), so Hebrew reads naturally in RTL.
 */
export function formatDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const parts: string[] = [];
  if (d) {
    parts.push(i18n.t('units.durD', { count: d }));
    if (h) parts.push(i18n.t('units.durH', { count: h }));
  } else if (h) {
    parts.push(i18n.t('units.durH', { count: h }));
    if (m) parts.push(i18n.t('units.durM', { count: m }));
  } else {
    parts.push(i18n.t('units.durM', { count: m }));
  }
  return parts.join(' ');
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return new Intl.DateTimeFormat(locale(), { dateStyle: 'short', timeStyle: 'medium' }).format(d);
}

export function formatRelative(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return '—';
  const diff = Math.round((t - now) / 1000);
  const rtf = new Intl.RelativeTimeFormat(locale(), { numeric: 'auto' });
  const abs = Math.abs(diff);
  if (abs < 60) return rtf.format(diff, 'second');
  if (abs < 3600) return rtf.format(Math.round(diff / 60), 'minute');
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), 'hour');
  return rtf.format(Math.round(diff / 86400), 'day');
}
