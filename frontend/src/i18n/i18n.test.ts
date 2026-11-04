import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import en from './locales/en.json';
import he from './locales/he.json';
import {
  DEVICE_CLASSES,
  EVENT_KINDS,
  NOTIFICATION_TYPES,
  SENSOR_TYPES,
  SKIP_REASONS,
} from '../api/types';
import { STATUSES } from '../lib/status';

type Tree = { [k: string]: string | Tree };

function flatten(obj: Tree, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (typeof v === 'string') out[key] = v;
    else Object.assign(out, flatten(v, key));
  }
  return out;
}

const enFlat = flatten(en as Tree);
const heFlat = flatten(he as Tree);

const PLURAL_RE = /_(zero|one|two|few|many|other)$/;
/** Key set with i18next plural suffixes folded into their base key. */
function baseKeys(flat: Record<string, string>): string[] {
  return [...new Set(Object.keys(flat).map((k) => k.replace(PLURAL_RE, '')))].sort();
}
function pluralBases(flat: Record<string, string>): string[] {
  return [
    ...new Set(
      Object.keys(flat)
        .filter((k) => PLURAL_RE.test(k))
        .map((k) => k.replace(PLURAL_RE, '')),
    ),
  ];
}
/** A key exists either as a plain key or as plural forms. */
function hasKey(flat: Record<string, string>, key: string): boolean {
  return key in flat || `${key}_other` in flat;
}

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((f) => {
    const p = join(dir, f);
    if (statSync(p).isDirectory()) return sourceFiles(p);
    return /\.(ts|tsx)$/.test(f) && !/\.test\./.test(f) ? [p] : [];
  });
}

describe('i18n', () => {
  it('he and en have identical keys (plural forms folded)', () => {
    expect(baseKeys(heFlat)).toEqual(baseKeys(enFlat));
  });

  it('plural keys define every plural category of their language', () => {
    for (const [lang, flat] of [
      ['en', enFlat],
      ['he', heFlat],
    ] as const) {
      const cats = new Intl.PluralRules(lang).resolvedOptions().pluralCategories;
      for (const base of pluralBases(flat)) {
        expect(base in flat, `${lang}:${base} must not also exist as a plain key`).toBe(false);
        for (const c of cats) expect(flat[`${base}_${c}`], `${lang}:${base}_${c}`).toBeTruthy();
      }
    }
  });

  it('has no empty translations', () => {
    for (const [k, v] of Object.entries({ ...enFlat })) expect(v, `en:${k}`).not.toBe('');
    for (const [k, v] of Object.entries({ ...heFlat })) expect(v, `he:${k}`).not.toBe('');
  });

  it('uses the same interpolation variables in both languages', () => {
    const vars = (s: string) =>
      (s.match(/\{\{\s*\w+\s*\}\}/g) ?? []).map((x) => x.replace(/\s/g, '')).sort();
    for (const k of Object.keys(enFlat)) {
      if (PLURAL_RE.test(k)) continue;
      expect(vars(heFlat[k] ?? ''), k).toEqual(vars(enFlat[k]));
    }
    // Plural forms may drop {{count}} (e.g. Hebrew "יומיים"), but never add other variables.
    for (const flat of [enFlat, heFlat])
      for (const k of Object.keys(flat).filter((x) => PLURAL_RE.test(x)))
        for (const v of vars(flat[k])) expect(v, k).toBe('{{count}}');
  });

  it('covers every status, sensor type and API error code', () => {
    for (const s of STATUSES) expect(enFlat[`status.${s}`]).toBeTruthy();
    for (const s of SENSOR_TYPES) expect(enFlat[`sensorType.${s}`]).toBeTruthy();
    const codes = [
      'bad_request',
      'group_cycle',
      'invalid_sensor_type',
      'unauthenticated',
      'invalid_credentials',
      'forbidden',
      'setup_required',
      'not_found',
      'conflict',
      'setup_already_done',
      'last_admin',
      'unsupported_media_type',
      'validation_error',
      'rate_limited',
      'snmp_error',
      'smtp_error',
      'webhook_error',
      'internal_error',
      'sensor_paused',
      // [2a]
      'telegram_error',
      'scan_running',
      'scan_not_running',
      'too_many_hosts',
      'device_exists',
    ];
    for (const c of codes) {
      expect(enFlat[`errors.${c}`], c).toBeTruthy();
      expect(heFlat[`errors.${c}`], c).toBeTruthy();
    }
  });

  it('covers the Phase 2a enums in both languages', () => {
    const keys = [
      ...DEVICE_CLASSES.map((c) => `deviceClass.${c}`),
      ...SKIP_REASONS.map((r) => `templates.skip.${r}`),
      ...EVENT_KINDS.map((k) => `eventKind.${k}`),
      ...NOTIFICATION_TYPES.map((n) => `channels.${n}`),
      ...['running', 'completed', 'cancelled', 'failed'].map((s) => `discovery.status.${s}`),
      ...['manual', 'scheduled'].map((s) => `discovery.trigger.${s}`),
      ...['invalid', 'rejected', 'prefix', 'order'].map((e) => `discovery.targetError.${e}`),
      ...[
        'ram',
        'virtual_memory',
        'fixed_disk',
        'removable_disk',
        'network_disk',
        'flash',
        'other',
      ].map((x) => `storage.types.${x}`),
      ...['value', 'disk_used_pct', 'disk_used_bytes', 'disk_free_bytes', 'disk_total_bytes'].map(
        (c) => `channel.${c}`,
      ),
      ...[
        'oid',
        'value_type',
        'unit',
        'multiplier',
        'label',
        'percent_of_oid',
        'expected',
        'match',
      ].map((k) => `sensorConfig.snmp_custom.${k}`),
      'sensorConfig.snmp_disk.storage_descr',
    ];
    for (const k of keys) {
      expect(enFlat[k], `en:${k}`).toBeTruthy();
      expect(heFlat[k], `he:${k}`).toBeTruthy();
    }
    // The Hebrew class names given by the contract (api.md § discovery notification payload).
    expect(heFlat['deviceClass.router']).toBe('נתב/חומת אש');
    expect(heFlat['deviceClass.access_point']).toBe('נקודת גישה');
    expect(heFlat['sensorType.snmp_custom']).toBe('OID SNMP מותאם');
    expect(heFlat['sensorType.snmp_disk']).toBe('שימוש בדיסק');
  });

  it('defines every static key used in the source', () => {
    const root = resolve(__dirname, '..');
    const missing: string[] = [];
    for (const file of sourceFiles(root)) {
      const text = readFileSync(file, 'utf8');
      for (const m of text.matchAll(/\bt\(\s*'([a-zA-Z0-9_.]+)'/g)) {
        if (!hasKey(enFlat, m[1])) missing.push(`${file}: ${m[1]}`);
      }
      for (const m of text.matchAll(/\b(?:i18nKey|k)="([a-zA-Z0-9_.]+)"/g)) {
        if (!hasKey(enFlat, m[1])) missing.push(`${file}: ${m[1]}`);
      }
      for (const m of text.matchAll(/'(validation\.[a-zA-Z]+)'/g)) {
        if (!(m[1] in enFlat)) missing.push(`${file}: ${m[1]}`);
      }
    }
    expect(missing).toEqual([]);
  });
});
