import { afterEach, describe, expect, it } from 'vitest';
import type { NamedValueMap } from '../api/types';
import { buildSensorConfig, effectiveValueMapRows, mapEnumValue } from './sensorConfig';
import { namedValueMap, pickLang, setNamedValueMaps } from './valueMaps';
import type { ConfigField } from '../api/types';

const HH3C: NamedValueMap = {
  title: { en: 'HPE Comware hardware fault codes', he: 'קודי תקלת חומרה של HPE Comware' },
  codes: [
    { value: '2', status: 'up', mib: 'normal', text: { en: 'Normal', he: 'תקין' } },
    {
      value: '51',
      status: 'down',
      mib: 'psuError',
      text: { en: 'Power supply fault', he: 'ספק כוח תקול' },
    },
  ],
  unknown: { en: 'Unknown fault code ({code})', he: 'קוד תקלה לא מוכר ({code})' },
};

const cfg = {
  value_type: 'enum',
  value_map: { '91': { status: 'warning', text: 'known issue' } },
  value_map_default: 'down',
  value_map_ref: 'hh3c_error_status',
};

afterEach(() => setNamedValueMaps({}));

describe('[1.3.1] named value maps', () => {
  it('maps codes in the viewer language, own entries first', () => {
    setNamedValueMaps({ hh3c_error_status: HH3C });
    expect(namedValueMap('hh3c_error_status')).toBe(HH3C);
    expect(namedValueMap(undefined)).toBeUndefined();
    expect(pickLang(HH3C.title, 'he')).toBe('קודי תקלת חומרה של HPE Comware');
    expect(mapEnumValue(cfg, 51, 'he')).toEqual({
      status: 'down',
      text: 'ספק כוח תקול',
      mib: 'psuError',
    });
    expect(mapEnumValue(cfg, 51, 'en')?.text).toBe('Power supply fault');
    expect(mapEnumValue(cfg, 91, 'en')).toEqual({ status: 'warning', text: 'known issue' });
    expect(mapEnumValue(cfg, 77, 'en')).toEqual({
      status: 'down',
      text: 'Unknown fault code (77)',
      unknown: true,
    });
    const rows = effectiveValueMapRows(cfg, 'en');
    expect(rows.map((r) => [r.value, r.text, r.mib ?? null, r.own ?? false])).toEqual([
      ['2', 'Normal', 'normal', false],
      ['51', 'Power supply fault', 'psuError', false],
      ['91', 'known issue', null, true],
    ]);
  });

  it('without the table loaded: own map only, unexpected value', () => {
    expect(mapEnumValue(cfg, 51, 'en')).toEqual({ status: 'down', text: null });
    expect(effectiveValueMapRows(cfg, 'en').map((r) => r.value)).toEqual(['91']);
  });

  it('a named table needs no own value map in the form', () => {
    const f = (key: string, type: ConfigField['type'], options: string[] | null = null) =>
      ({ key, type, required: false, default: null, min: null, max: null, options }) as ConfigField;
    const fields = [
      f('oid', 'str'),
      f('value_type', 'enum', ['gauge', 'enum']),
      f('value_map', 'value_map'),
      f('value_map_default', 'enum', ['up', 'warning', 'down']),
      f('value_map_ref', 'enum', ['', 'hh3c_error_status']),
    ];
    const raw = {
      oid: '1.3.6.1.4.1.25506.2.6.1.1.1.1.19.212',
      value_type: 'enum',
      value_map: '[]',
      value_map_default: 'down',
      value_map_ref: 'hh3c_error_status',
    };
    const ok = buildSensorConfig('snmp_custom', fields, raw);
    expect(ok.errors).toEqual({});
    expect(ok.config.value_map_ref).toBe('hh3c_error_status');
    const bad = buildSensorConfig('snmp_custom', fields, { ...raw, value_map_ref: '' });
    expect(bad.errors.value_map).toBe('valueMap.err.empty');
    const gauge = buildSensorConfig('snmp_custom', fields, { ...raw, value_type: 'gauge' });
    expect(gauge.config.value_map_ref).toBe('');
  });
});
