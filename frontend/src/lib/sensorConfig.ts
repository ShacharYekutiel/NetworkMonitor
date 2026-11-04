import type { ConfigField } from '../api/types';

/** Raw form values: strings for text inputs, booleans for checkboxes. */
export type RawConfig = Record<string, string | boolean>;

export function headersToText(h: unknown): string {
  if (!h || typeof h !== 'object') return '';
  return Object.entries(h as Record<string, string>)
    .map(([k, v]) => `${k}: ${v}`)
    .join('\n');
}

export function textToHeaders(s: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of s.split('\n')) {
    const i = line.indexOf(':');
    if (i <= 0) continue;
    const k = line.slice(0, i).trim();
    if (k) out[k] = line.slice(i + 1).trim();
  }
  return out;
}

function toRaw(f: ConfigField, v: unknown): string | boolean {
  switch (f.type) {
    case 'bool':
      return v === undefined || v === null ? false : Boolean(v);
    case 'int_list':
      return Array.isArray(v) ? v.join(', ') : '';
    case 'headers':
      return headersToText(v);
    default:
      return v === undefined || v === null ? '' : String(v);
  }
}

/** Initial raw values from `config` (falls back to field defaults). */
export function rawFromConfig(fields: ConfigField[], config?: Record<string, unknown>): RawConfig {
  const raw: RawConfig = {};
  for (const f of fields) {
    const v = config && f.key in config ? config[f.key] : f.default;
    raw[f.key] = toRaw(f, v);
  }
  return raw;
}

export type ConfigErrors = Record<string, string>;

/**
 * Converts raw values into a typed config. Error values are i18n keys
 * (`validation.required`, `validation.integer`, `validation.range`, `validation.intList`).
 */
export function configFromRaw(
  fields: ConfigField[],
  raw: RawConfig,
): { config: Record<string, unknown>; errors: ConfigErrors } {
  const config: Record<string, unknown> = {};
  const errors: ConfigErrors = {};
  const checkRange = (f: ConfigField, n: number) =>
    (f.min !== null && n < f.min) || (f.max !== null && n > f.max);

  for (const f of fields) {
    const v = raw[f.key];
    switch (f.type) {
      case 'bool':
        config[f.key] = Boolean(v);
        break;
      case 'int':
      case 'int_nullable': {
        const s = String(v ?? '').trim();
        if (!s) {
          if (f.type === 'int_nullable') config[f.key] = null;
          else if (f.required) errors[f.key] = 'validation.required';
          break;
        }
        if (!/^-?\d+$/.test(s)) {
          errors[f.key] = 'validation.integer';
          break;
        }
        const n = Number(s);
        if (checkRange(f, n)) errors[f.key] = 'validation.range';
        else config[f.key] = n;
        break;
      }
      case 'int_list': {
        const s = String(v ?? '').trim();
        if (!s) {
          config[f.key] = null;
          break;
        }
        const parts = s.split(/[\s,]+/).filter(Boolean);
        if (!parts.every((p) => /^\d+$/.test(p))) {
          errors[f.key] = 'validation.intList';
          break;
        }
        config[f.key] = parts.map(Number);
        break;
      }
      case 'headers':
        config[f.key] = textToHeaders(String(v ?? ''));
        break;
      case 'str_nullable': {
        const s = String(v ?? '');
        if (!s.trim()) {
          if (f.required) errors[f.key] = 'validation.required';
          else config[f.key] = null;
        } else config[f.key] = s;
        break;
      }
      case 'enum':
      case 'str':
      default: {
        const s = String(v ?? '');
        if (f.required && !s.trim()) errors[f.key] = 'validation.required';
        else config[f.key] = s;
      }
    }
  }
  return { config, errors };
}
