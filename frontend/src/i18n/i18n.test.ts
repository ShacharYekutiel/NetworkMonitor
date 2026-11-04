import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import en from './locales/en.json';
import he from './locales/he.json';
import { SENSOR_TYPES } from '../api/types';
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

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((f) => {
    const p = join(dir, f);
    if (statSync(p).isDirectory()) return sourceFiles(p);
    return /\.(ts|tsx)$/.test(f) && !/\.test\./.test(f) ? [p] : [];
  });
}

describe('i18n', () => {
  it('he and en have identical keys', () => {
    expect(Object.keys(heFlat).sort()).toEqual(Object.keys(enFlat).sort());
  });

  it('has no empty translations', () => {
    for (const [k, v] of Object.entries({ ...enFlat })) expect(v, `en:${k}`).not.toBe('');
    for (const [k, v] of Object.entries({ ...heFlat })) expect(v, `he:${k}`).not.toBe('');
  });

  it('uses the same interpolation variables in both languages', () => {
    const vars = (s: string) =>
      (s.match(/\{\{\s*\w+\s*\}\}/g) ?? []).map((x) => x.replace(/\s/g, '')).sort();
    for (const k of Object.keys(enFlat)) expect(vars(heFlat[k] ?? ''), k).toEqual(vars(enFlat[k]));
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
    ];
    for (const c of codes) expect(enFlat[`errors.${c}`], c).toBeTruthy();
  });

  it('defines every static key used in the source', () => {
    const root = resolve(__dirname, '..');
    const missing: string[] = [];
    for (const file of sourceFiles(root)) {
      const text = readFileSync(file, 'utf8');
      for (const m of text.matchAll(/\bt\(\s*'([a-zA-Z0-9_.]+)'/g)) {
        if (!(m[1] in enFlat)) missing.push(`${file}: ${m[1]}`);
      }
      for (const m of text.matchAll(/'(validation\.[a-zA-Z]+)'/g)) {
        if (!(m[1] in enFlat)) missing.push(`${file}: ${m[1]}`);
      }
    }
    expect(missing).toEqual([]);
  });
});
