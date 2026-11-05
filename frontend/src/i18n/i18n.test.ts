import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import en from './locales/en.json';
import he from './locales/he.json';
import {
  CLASSIFIED_BY,
  CONFIDENCES,
  DEPENDENCY_MODES,
  DEVICE_CLASSES,
  EVENT_KINDS,
  LINK_SOURCES,
  MAINTENANCE_SCOPES,
  NOTIFICATION_TYPES,
  SENSOR_TYPES,
  SKIP_REASONS,
  ALERT_REASONS,
  CONNECTED_KINDS,
  HEALTH_CATEGORIES,
  IF_STATUSES,
  INVENTORY_MODES,
  POE_DETECTIONS,
  PORT_KINDS,
  PORT_STATES,
} from '../api/types';
import { PORT_COLUMNS } from '../lib/ports';
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
      // [2b-1]
      'dependency_cycle',
      'not_acknowledgeable',
      'topology_refresh_running',
      'link_not_manual',
      // [2b-2]
      'ports_disabled',
      'ports_refresh_running',
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

  it('covers the Phase 2b-1 enums and keys in both languages', () => {
    const keys = [
      'status.unreachable',
      'nav.maintenance',
      'nav.map',
      'eventKind.acknowledge',
      'eventKind.maintenance',
      'errors.dependency_cycle_path',
      ...CONFIDENCES.map((c) => `confidence.${c}`),
      ...CLASSIFIED_BY.map((c) => `classifiedBy.${c}`),
      ...DEPENDENCY_MODES.map((m) => `dependency.modes.${m}`),
      'dependency.modes.inheritParent',
      ...MAINTENANCE_SCOPES.map((s) => `maintenance.scopes.${s}`),
      ...['once', 'weekly'].map((k) => `maintenance.kinds.${k}`),
      ...['active', 'upcoming', 'recurring', 'past'].flatMap((s) => [
        `maintenance.sections.${s}`,
        `maintenance.empty.${s}`,
      ]),
      ...LINK_SOURCES.map((s) => `topology.source.${s}`),
      ...['running', 'completed', 'failed'].map((s) => `topology.refreshStatus.${s}`),
      ...['device', 'neighbor'].map((k) => `topology.bKind.${k}`),
      'sensorConfig.snmp_custom.value_map',
      'sensorConfig.snmp_custom.value_map_default',
      'sensorConfigOption.snmp_custom.value_type.enum',
      'escalation.help',
      'escalation.afterHint',
      'escalation.channelsHint',
      'escalation.repeatHint',
    ];
    for (const k of keys) {
      expect(enFlat[k], `en:${k}`).toBeTruthy();
      expect(heFlat[k], `he:${k}`).toBeTruthy();
    }
    expect(heFlat['status.unreachable']).toBe('לא נגיש');
    expect(enFlat['status.unreachable']).toBe('Unreachable');
  });

  it('has the 2b-1 plurals in every category (en: one/other, he: one/two/other)', () => {
    const plurals = [
      'dashboard.andMore',
      'dashboard.maintenanceActive',
      'dashboard.maintenanceDevices',
      'maintenance.coveredDevices',
      'fingerprint.redirects',
      'topology.refreshFailed',
      'topology.deleteStale',
      'topology.deleteStaleConfirm',
      'topology.staleDeleted',
      'topology.stale',
      'topology.applySelected',
      'topology.applied',
    ];
    for (const [lang, flat] of [
      ['en', enFlat],
      ['he', heFlat],
    ] as const) {
      const cats = new Intl.PluralRules(lang).resolvedOptions().pluralCategories;
      for (const base of plurals) {
        expect(base in flat, `${lang}:${base} plain`).toBe(false);
        for (const c of cats) expect(flat[`${base}_${c}`], `${lang}:${base}_${c}`).toBeTruthy();
      }
    }
  });

  it('renders 2b-1 plurals correctly', async () => {
    const i18n = (await import('./index')).default;
    await i18n.changeLanguage('he');
    expect(i18n.t('dashboard.maintenanceDevices', { count: 1 })).toBe('התקן אחד בתחזוקה');
    expect(i18n.t('dashboard.maintenanceDevices', { count: 2 })).toBe('2 התקנים בתחזוקה');
    expect(i18n.t('dashboard.maintenanceDevices', { count: 12 })).toBe('12 התקנים בתחזוקה');
    await i18n.changeLanguage('en');
    expect(i18n.t('topology.staleDeleted', { count: 1 })).toBe('1 stale link deleted.');
    expect(i18n.t('topology.staleDeleted', { count: 3 })).toBe('3 stale links deleted.');
  });

  it('covers the Phase 2b-2 enums and keys in both languages', () => {
    const keys = [
      'nav.find',
      'sensorType.snmp_walk_count',
      'templates.skip.not_applicable',
      'templates.verified',
      'templates.unverified',
      'templates.source.builtin',
      'templates.source.user',
      'templates.overrides',
      'templates.reload',
      'device.portInventory',
      'device.arpCollect',
      'sensorConfig.snmp_custom.percent_base',
      ...['total', 'sum'].map((o) => `sensorConfigOption.snmp_custom.percent_base.${o}`),
      ...['oid', 'count_values', 'label', 'max_rows'].map(
        (k) => `sensorConfig.snmp_walk_count.${k}`,
      ),
      ...['rows', 'matched', 'unmatched'].map((c) => `channel.${c}`),
      ...HEALTH_CATEGORIES.map((c) => `health.${c}`),
      ...INVENTORY_MODES.map((m) => `inventoryMode.${m}`),
      ...PORT_STATES.map((s) => `ports.state.${s}`),
      ...PORT_KINDS.map((k) => `ports.kind.${k}`),
      ...IF_STATUSES.map((s) => `ports.ifStatus.${s}`),
      ...CONNECTED_KINDS.map((k) => `ports.connectedKind.${k}`),
      ...POE_DETECTIONS.map((d) => `ports.poeDetection.${d}`),
      ...ALERT_REASONS.map((r) => `ports.alertReason.${r}`),
      ...['neighbor', 'mac_count'].map((r) => `ports.uplinkReason.${r}`),
      ...['access', 'trunk'].map((m) => `ports.mode.${m}`),
      ...['full', 'half', 'unknown'].map((d) => `ports.duplex.${d}`),
      ...['port', ...PORT_COLUMNS].map((c) => `ports.col.${c}`),
      ...['faceplate', 'list'].map((v) => `ports.view.${v}`),
      ...[
        'empty',
        'connected',
        'alert',
        'emptyAlert',
        'disabled',
        'client',
        'ap',
        'switch',
        'trunk',
        'macCount',
      ].map((l) => `ports.legend.${l}`),
      ...['fdbEmpty', 'noArp', 'poeUnmapped'].map((h) => `ports.hint.${h}`),
      ...['interval', 'threshold', 'retention', 'reverseDns'].map((k) => `settings.ports.${k}`),
    ];
    for (const k of keys) {
      expect(enFlat[k], `en:${k}`).toBeTruthy();
      expect(heFlat[k], `he:${k}`).toBeTruthy();
    }
    expect(heFlat['sensorType.snmp_walk_count']).toBe('ספירת שורות SNMP');
    expect(enFlat['sensorType.snmp_walk_count']).toBe('SNMP row count');
  });

  it('renders the 2b-2 plurals', async () => {
    const i18n = (await import('./index')).default;
    await i18n.changeLanguage('he');
    expect(i18n.t('ports.macCount', { count: 1 })).toBe('MAC אחד');
    expect(i18n.t('ports.macCount', { count: 2 })).toBe('2 כתובות MAC');
    expect(i18n.t('ports.uplinkMacs', { count: 7 })).toBe('נלמדו 7 כתובות MAC (Uplink)');
    await i18n.changeLanguage('en');
    expect(i18n.t('ports.macCount', { count: 1 })).toBe('1 MAC');
    expect(i18n.t('ports.macCount', { count: 7 })).toBe('7 MACs');
    expect(i18n.t('find.count', { count: 2 })).toBe('2 locations');
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
