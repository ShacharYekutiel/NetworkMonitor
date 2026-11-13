import { afterEach, describe, expect, it } from 'vitest';
import type { NamedValueMap } from '../api/types';
import { buildSensorConfig, effectiveValueMapRows, mapEnumValue } from './sensorConfig';
import { localizeMessage, namedValueMap, pickLang, setNamedValueMaps } from './valueMaps';
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

describe('[1.3.2] localizeMessage: named-map status messages in the viewer language', () => {
  const HE = 'PSU 2: ספק כוח תקול (psuError, 51)';
  const EN = 'PSU 2: Power supply fault (psuError, 51)';
  const load = () => setNamedValueMaps({ hh3c_error_status: HH3C });

  it('he → en and en → he, and leaves the viewer-language message alone', () => {
    load();
    expect(localizeMessage(HE, 'en')).toBe(EN);
    expect(localizeMessage(EN, 'he')).toBe(HE);
    expect(localizeMessage(EN, 'en-US')).toBe(EN);
    expect(localizeMessage(HE, 'he')).toBe(HE);
    expect(localizeMessage('Fan 1: תקין (normal, 2)', 'en')).toBe('Fan 1: Normal (normal, 2)');
  });

  it('unknown code, both ways', () => {
    load();
    expect(localizeMessage('PSU 2: קוד תקלה לא מוכר (77)', 'en')).toBe(
      'PSU 2: Unknown fault code (77)',
    );
    expect(localizeMessage('PSU 2: Unknown fault code (77)', 'he')).toBe(
      'PSU 2: קוד תקלה לא מוכר (77)',
    );
    expect(localizeMessage('Unknown fault code (77)', 'en')).toBe('Unknown fault code (77)');
  });

  it('labels with ":" or parentheses, and a prefixed (unreachable) message', () => {
    load();
    expect(localizeMessage('sw1: PSU (rear): 2: ספק כוח תקול (psuError, 51)', 'en')).toBe(
      'sw1: PSU (rear): 2: Power supply fault (psuError, 51)',
    );
    expect(localizeMessage('PSU (slot 1, 2): Power supply fault (psuError, 51)', 'he')).toBe(
      'PSU (slot 1, 2): ספק כוח תקול (psuError, 51)',
    );
    expect(
      localizeMessage('Unreachable (parent core1 down): PSU 2: ספק כוח תקול (psuError, 51)', 'en'),
    ).toBe('Unreachable (parent core1 down): PSU 2: Power supply fault (psuError, 51)');
    // no label at all
    expect(localizeMessage('ספק כוח תקול (psuError, 51)', 'en')).toBe(
      'Power supply fault (psuError, 51)',
    );
  });

  it('a text the server has since reworded is replaced by the (mib, code) pair alone', () => {
    load();
    expect(localizeMessage('PSU 2: תקלה בספק הכוח (psuError, 51)', 'en')).toBe(EN);
  });

  it('strings that do not match are returned unchanged', () => {
    load();
    for (const m of [
      '',
      'Request timed out',
      'PSU 2: custom text (51)',
      'PSU 2: ספק כוח תקול',
      'PSU 2: ספק כוח תקול (psuError, 52)', // pair is not in the table
      'PSU 2: something (otherMib, 51)',
      '(psuError, 51)',
      'a (b, c) $1 [x] {code} \\ ((( ',
      'ifOperStatus: down (2)',
    ])
      expect(localizeMessage(m, 'en')).toBe(m);
    expect(localizeMessage(null, 'en')).toBe('');
    expect(localizeMessage(undefined, 'he')).toBe('');
  });

  it('maps not loaded: unchanged', () => {
    expect(localizeMessage(HE, 'en')).toBe(HE);
    expect(localizeMessage('PSU 2: קוד תקלה לא מוכר (77)', 'en')).toBe(
      'PSU 2: קוד תקלה לא מוכר (77)',
    );
  });
});
