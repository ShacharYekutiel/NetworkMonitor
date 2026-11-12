import type { ConfigField } from '../api/types';
import { namedValueMap, pickLang } from './valueMaps';

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

// [2b-1] snmp_custom enum value maps. The form keeps the rows as JSON text in RawConfig.
export type ValueMapStatus = 'up' | 'warning' | 'down';
export type ValueMapRow = { value: string; status: ValueMapStatus; text: string };
export type ValueMap = Record<string, { status: ValueMapStatus; text: string }>;
export const VALUE_MAP_MAX = 32;

export function valueMapToRows(v: unknown): ValueMapRow[] {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return [];
  return Object.entries(v as ValueMap)
    .map(([value, e]) => ({
      value,
      status: (e?.status ?? 'warning') as ValueMapStatus,
      text: String(e?.text ?? ''),
    }))
    .sort((a, b) => Number(a.value) - Number(b.value));
}

export function parseValueMapRows(raw: string | boolean | undefined): ValueMapRow[] {
  if (typeof raw !== 'string' || !raw) return [];
  try {
    const x = JSON.parse(raw) as unknown;
    return Array.isArray(x) ? (x as ValueMapRow[]) : [];
  } catch {
    return [];
  }
}

/** Rows → API object; error = i18n key (integer keys, distinct, ≤ 32, text ≤ 32 chars). */
export function rowsToValueMap(rows: ValueMapRow[]): { map: ValueMap; error: string | null } {
  const map: ValueMap = {};
  const used = rows.filter((r) => r.value.trim() !== '' || r.text.trim() !== '');
  if (used.length > VALUE_MAP_MAX) return { map, error: 'valueMap.err.tooMany' };
  for (const r of used) {
    const k = r.value.trim();
    if (!/^-?\d+$/.test(k)) return { map, error: 'valueMap.err.integer' };
    const key = String(Number(k));
    if (key in map) return { map, error: 'valueMap.err.duplicate' };
    if (r.text.length > 32) return { map, error: 'valueMap.err.text' };
    map[key] = { status: r.status, text: r.text.trim() };
  }
  return { map, error: null };
}

/**
 * The mapped status/text of a raw enum value (unmapped → value_map_default, "unexpected value").
 * [1.3.1] With `value_map_ref` the named table (texts in `lang`) is used for values the sensor's
 * own map does not list; `mib` = the table's technical name, `unknown` = the table's text for an
 * unknown code (it already contains the value).
 */
export function mapEnumValue(
  config: Record<string, unknown>,
  raw: number | null | undefined,
  lang = 'en',
): { status: ValueMapStatus; text: string | null; mib?: string; unknown?: boolean } | null {
  if (raw === null || raw === undefined || config.value_type !== 'enum') return null;
  const key = String(Math.round(raw));
  const map = (config.value_map ?? {}) as ValueMap;
  const hit = map[key];
  if (hit) return { status: hit.status, text: hit.text };
  const d = config.value_map_default;
  const dflt: ValueMapStatus = d === 'up' || d === 'down' ? d : 'warning';
  const named = namedValueMap(config.value_map_ref);
  if (named) {
    const c = named.codes.find((x) => x.value === key);
    if (c) return { status: c.status, text: pickLang(c.text, lang), mib: c.mib };
    return {
      status: dflt,
      text: pickLang(named.unknown, lang).replace('{code}', key),
      unknown: true,
    };
  }
  return { status: dflt, text: null };
}

/** [1.3.1] The rows a sensor's enum map shows: the named table's codes (localized, with the
 * MIB name) overridden by the sensor's own `value_map` entries (`own`). */
export function effectiveValueMapRows(
  config: Record<string, unknown>,
  lang = 'en',
): (ValueMapRow & { mib?: string; own?: boolean })[] {
  const own = valueMapToRows(config.value_map).map((r) => ({ ...r, own: true }));
  const named = namedValueMap(config.value_map_ref);
  if (!named) return own;
  const ownKeys = new Set(own.map((r) => r.value));
  const rows = named.codes
    .filter((c) => !ownKeys.has(c.value))
    .map((c) => ({
      value: c.value,
      status: c.status as ValueMapStatus,
      text: pickLang(c.text, lang),
      mib: c.mib,
    }));
  return [...rows, ...own].sort((a, b) => Number(a.value) - Number(b.value));
}

function toRaw(f: ConfigField, v: unknown): string | boolean {
  switch (f.type) {
    case 'value_map':
      return JSON.stringify(valueMapToRows(v));
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
      case 'float': {
        const s = String(v ?? '').trim();
        if (!s) {
          if (f.required) errors[f.key] = 'validation.required';
          else if (f.default !== undefined && f.default !== null) config[f.key] = f.default;
          break;
        }
        const n = Number(s);
        if (!/^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(s) || !Number.isFinite(n)) {
          errors[f.key] = 'validation.number';
          break;
        }
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
      case 'value_map': {
        const { map, error } = rowsToValueMap(parseValueMapRows(v));
        if (error) errors[f.key] = error;
        else if (f.required && !Object.keys(map).length) errors[f.key] = 'validation.required';
        else config[f.key] = map;
        break;
      }
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

/**
 * Which config fields the form shows (architecture.md § 9.5):
 * `snmp_custom` shows `percent_of_oid` only for gauge, `expected`/`match` only for string;
 * `snmp_disk` hides the informational `storage_index` (the backend fills it in).
 */
export function visibleConfigFields(
  type: string,
  fields: ConfigField[],
  raw: RawConfig,
): ConfigField[] {
  if (type === 'snmp_custom') {
    const vt = String(raw.value_type ?? 'gauge');
    return fields.filter((f) => {
      if (f.key === 'percent_of_oid') return vt === 'gauge';
      // [2b-2] how percent_of_oid is used (gauge only, like percent_of_oid)
      if (f.key === 'percent_base') return vt === 'gauge';
      if (f.key === 'expected' || f.key === 'match') return vt === 'string';
      // [2b-1] enum: value map + default status; the raw integer is used (no multiplier/unit).
      if (f.key === 'value_map' || f.key === 'value_map_default') return vt === 'enum';
      if (f.key === 'value_map_ref') return vt === 'enum'; // [1.3.1]
      if (f.key === 'multiplier' || f.key === 'unit') return vt !== 'enum';
      return true;
    });
  }
  if (type === 'snmp_disk') return fields.filter((f) => f.key !== 'storage_index');
  return fields;
}

/**
 * Builds the config from the visible fields only, and resets type-specific keys that no
 * longer apply (`percent_of_oid` is rejected by the API for non-gauge custom sensors).
 */
export function buildSensorConfig(
  type: string,
  fields: ConfigField[],
  raw: RawConfig,
): { config: Record<string, unknown>; errors: ConfigErrors } {
  const visible = visibleConfigFields(type, fields, raw);
  const res = configFromRaw(visible, raw);
  if (type === 'snmp_custom') {
    const vt = res.config.value_type ?? 'gauge';
    if (vt !== 'gauge' && fields.some((f) => f.key === 'percent_of_oid'))
      res.config.percent_of_oid = null;
    if (vt !== 'string' && fields.some((f) => f.key === 'expected')) res.config.expected = null;
    // [2b-2] percent_base is sent for gauges only ("sum" needs percent_of_oid, 422 otherwise).
    if (vt !== 'gauge') delete res.config.percent_base;
    else if (res.config.percent_base === 'sum' && !res.config.percent_of_oid)
      res.errors.percent_base = 'validation.percentBaseSum';
    // [2b-1] value_map is required non-empty for enum (422 otherwise).
    // [1.3.1] a named table (value_map_ref) may stand alone.
    if (vt === 'enum') {
      const m = res.config.value_map as ValueMap | undefined;
      if (!res.errors.value_map && (!m || !Object.keys(m).length) && !res.config.value_map_ref)
        res.errors.value_map = 'valueMap.err.empty';
    }
    if (vt !== 'enum' && fields.some((f) => f.key === 'value_map_ref'))
      res.config.value_map_ref = '';
    if (typeof res.config.oid === 'string') res.config.oid = res.config.oid.trim();
  }
  if (type === 'snmp_walk_count' && typeof res.config.oid === 'string')
    res.config.oid = res.config.oid.trim().replace(/^\./, '');
  return res;
}
