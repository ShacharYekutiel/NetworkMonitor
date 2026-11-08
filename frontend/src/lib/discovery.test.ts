import { describe, expect, it } from 'vitest';
import { QueryClient } from '@tanstack/react-query';
import i18n from '../i18n';
import { qk } from '../api/endpoints';
import type {
  ConfigField,
  DiscoveryResult,
  DiscoveryScan,
  ImportResponse,
  Page,
  ScanOptions,
} from '../api/types';
import {
  applyScanProgress,
  compareIp,
  filterResults,
  mergeResult,
  NO_FILTERS,
  parsePorts,
  progressPct,
  scanOptionsForm,
  scanOptionsToInput,
  scheduleChoice,
  scheduleFromChoice,
} from './discovery';
import { applyLiveMessage, InvalidationBatcher } from './liveCache';
import { buildSensorConfig, rawFromConfig, visibleConfigFields } from './sensorConfig';
import { isTelegramChatId, isTelegramToken } from './telegram';
import { countMerged, parseIpv6, parseTarget, summarizeTargets } from './targets';
import {
  applySummary,
  describeEntry,
  groupSkipped,
  importRows,
  isolateLtrRuns,
  l10n,
  skipReasonKey,
} from './templates';
import { errorMessage } from './errors';
import { ApiError } from '../api/client';
import { sensorValueText, storageUsedPct } from './format';
import { bufferResult, clearBuffered, withBuffered } from './discovery';
import { retryIds } from './templates';

const opts: ScanOptions = {
  ping: true,
  ping_timeout_ms: 1000,
  snmp: true,
  snmp_port: null,
  snmp_timeout_s: 1,
  extra_communities: null,
  extra_communities_count: 1,
  extra_v3: [],
  extra_v3_count: 0,
  tcp: true,
  tcp_ports: [22, 80],
  tcp_timeout_ms: 1000,
  dns: true,
  fingerprint: true,
  fingerprint_timeout_ms: 2000,
};

function scan(p: Partial<DiscoveryScan> = {}): DiscoveryScan {
  return {
    id: 57,
    profile_id: null,
    profile_name: null,
    trigger: 'manual',
    status: 'running',
    group_id: 1,
    targets: ['192.168.1.0/24'],
    options: opts,
    total_hosts: 254,
    scanned_hosts: 10,
    found_hosts: 2,
    new_hosts: 1,
    error: null,
    created_by: 'admin',
    started_at: '2026-11-04T12:00:00Z',
    finished_at: null,
    ...p,
  };
}

function result(p: Partial<DiscoveryResult> = {}): DiscoveryResult {
  return {
    id: 1,
    scan_id: 57,
    ip: '192.168.1.1',
    hostname: null,
    mac: null,
    ping_ok: true,
    ping_rtt_ms: 0.4,
    snmp_ok: false,
    snmp_version: null,
    snmp_credential: null,
    sys_name: null,
    sys_descr: null,
    sys_object_id: null,
    open_ports: [],
    device_class: 'unknown',
    vendor: null,
    suggested_template: 'generic_ping',
    existing_device_id: null,
    is_new: true,
    imported_device_id: null,
    fingerprint: null,
    model: null,
    classified_by: 'ports',
    confidence: 'low',
    ...p,
  };
}

describe('target parsing hints', () => {
  it('counts CIDR hosts without network/broadcast and normalises host bits', () => {
    const p = parseTarget('192.168.1.5/24');
    expect(p.ok && p.kind).toBe('cidr');
    expect(p.ok && p.hosts).toBe(254);
    expect(p.ok && p.normalized).toBe('192.168.1.0/24');
    expect(
      parseTarget('10.0.0.0/31').ok && (parseTarget('10.0.0.0/31') as { hosts: number }).hosts,
    ).toBe(2);
    expect((parseTarget('10.0.0.7/32') as { hosts: number }).hosts).toBe(1);
    expect(parseTarget('10.0.0.0/7')).toMatchObject({ ok: false, error: 'prefix' });
  });

  it('parses short and full ranges', () => {
    expect(parseTarget('192.168.10.1-30')).toMatchObject({ ok: true, kind: 'range', hosts: 30 });
    expect(parseTarget('192.168.1.250-192.168.2.5')).toMatchObject({ ok: true, hosts: 12 });
    expect(parseTarget('192.168.1.50-10')).toMatchObject({ ok: false, error: 'order' });
    expect(parseTarget('192.168.1.1-256')).toMatchObject({ ok: false, error: 'invalid' });
  });

  it('accepts single IPv4/IPv6 and hostnames, rejects garbage', () => {
    expect(parseTarget('127.0.0.21')).toMatchObject({ ok: true, kind: 'ipv4' });
    expect(parseTarget('fd00::1')).toMatchObject({ ok: true, kind: 'ipv6' });
    expect(parseTarget('nas.lan')).toMatchObject({ ok: true, kind: 'hostname' });
    expect(parseTarget('10.0.0.256')).toMatchObject({ ok: false, error: 'invalid' });
    expect(parseTarget('fd00::/64')).toMatchObject({ ok: false });
    expect(parseTarget('-bad-.lan')).toMatchObject({ ok: false, error: 'invalid' });
  });

  it('rejects multicast, 0/8, 240/4 and ::, but allows loopback', () => {
    expect(parseTarget('224.0.0.1')).toMatchObject({ ok: false, error: 'rejected' });
    expect(parseTarget('255.255.255.255')).toMatchObject({ ok: false, error: 'rejected' });
    expect(parseTarget('0.1.2.3')).toMatchObject({ ok: false, error: 'rejected' });
    expect(parseTarget('ff02::1')).toMatchObject({ ok: false, error: 'rejected' });
    expect(parseTarget('::')).toMatchObject({ ok: false, error: 'rejected' });
    expect(parseTarget('127.0.0.0/24')).toMatchObject({ ok: true, hosts: 254 });
  });

  it('parses IPv6 forms', () => {
    expect(parseIpv6('::1')).toEqual([0, 0, 0, 0, 0, 0, 0, 1]);
    expect(parseIpv6('::ffff:10.0.0.1')).toEqual([0, 0, 0, 0, 0, 0xffff, 0x0a00, 1]);
    expect(parseIpv6('1:2:3:4:5:6:7:8')).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
    expect(parseIpv6('1::2::3')).toBeNull();
    expect(parseIpv6('12345::')).toBeNull();
  });

  it('de-duplicates overlapping entries and enforces the 4096 cap', () => {
    expect(
      countMerged([
        [1, 10],
        [5, 20],
        [30, 30],
      ]),
    ).toBe(21);
    const s = summarizeTargets('192.168.1.0/24\n192.168.1.10-20, nas.lan NAS.lan\n192.168.1.1');
    expect(s.totalHosts).toBe(255);
    expect(s.valid).toBe(true);
    expect(summarizeTargets('10.0.0.0/20').tooManyHosts).toBe(false); // 4094 fits
    const big = summarizeTargets('10.0.0.0/16');
    expect(big.tooManyHosts).toBe(true);
    expect(big.totalHosts).toBe(65534);
    expect(big.valid).toBe(false);
    const many = summarizeTargets(Array.from({ length: 65 }, (_, i) => `10.0.1.${i + 1}`));
    expect(many.tooManyEntries).toBe(true);
    expect(summarizeTargets('').valid).toBe(false);
    expect(summarizeTargets('foo..bar').errors).toHaveLength(1);
  });
});

describe('discovery live reducers', () => {
  it('applies progress and finished, never resurrecting a finished scan', () => {
    const s = scan();
    const p = applyScanProgress(s, {
      scan_id: 57,
      status: 'running',
      total_hosts: 254,
      scanned_hosts: 31,
      found_hosts: 6,
      new_hosts: 2,
    })!;
    expect(p).toMatchObject({ scanned_hosts: 31, found_hosts: 6, new_hosts: 2 });
    expect(applyScanProgress(s, { ...p, scan_id: 99, status: 'running' } as never)).toBe(s);
    const f = applyScanProgress(
      p,
      {
        scan_id: 57,
        status: 'completed',
        total_hosts: 254,
        scanned_hosts: 254,
        found_hosts: 11,
        new_hosts: 3,
        error: null,
      },
      '2026-11-04T12:05:00Z',
    )!;
    expect(f).toMatchObject({ status: 'completed', finished_at: '2026-11-04T12:05:00Z' });
    const late = applyScanProgress(f, {
      scan_id: 57,
      status: 'running',
      total_hosts: 254,
      scanned_hosts: 200,
      found_hosts: 9,
      new_hosts: 3,
    });
    expect(late).toBe(f);
    expect(progressPct(p)).toBe(12);
    expect(progressPct({ total_hosts: 0, scanned_hosts: 0 })).toBe(0);
  });

  it('merges results in inet order and replaces duplicates', () => {
    expect(['10.0.0.10', '10.0.0.9', 'fd00::1', '9.9.9.9'].sort(compareIp)).toEqual([
      '9.9.9.9',
      '10.0.0.9',
      '10.0.0.10',
      'fd00::1',
    ]);
    let list = mergeResult([], result({ id: 2, ip: '10.0.0.10' }));
    list = mergeResult(list, result({ id: 1, ip: '10.0.0.9' }));
    list = mergeResult(list, result({ id: 1, ip: '10.0.0.9', hostname: 'x' }));
    expect(list.map((r) => r.id)).toEqual([1, 2]);
    expect(list[0].hostname).toBe('x');
  });

  it('updates the query cache from discovery.* WS messages', () => {
    const qc = new QueryClient();
    const batch = new InvalidationBatcher(qc, 10_000);
    qc.setQueryData(qk.discoveryScan(57), scan());
    qc.setQueryData(qk.discoveryResults(57), [result({ id: 5, ip: '192.168.1.20' })]);
    const page: Page<DiscoveryScan> = { items: [scan()], total: 1, page: 1, page_size: 100 };
    qc.setQueryData(qk.discoveryScans({}), page);

    applyLiveMessage(
      qc,
      {
        type: 'discovery.progress',
        ts: 'x',
        data: {
          scan_id: 57,
          status: 'running',
          total_hosts: 254,
          scanned_hosts: 100,
          found_hosts: 3,
          new_hosts: 2,
        },
      },
      batch,
    );
    expect(qc.getQueryData<DiscoveryScan>(qk.discoveryScan(57))?.scanned_hosts).toBe(100);
    expect(
      qc.getQueryData<Page<DiscoveryScan>>(qk.discoveryScans({}))?.items[0].scanned_hosts,
    ).toBe(100);

    applyLiveMessage(
      qc,
      {
        type: 'discovery.result',
        ts: 'x',
        data: { scan_id: 57, result: result({ id: 6, ip: '192.168.1.3' }) },
      },
      batch,
    );
    expect(qc.getQueryData<DiscoveryResult[]>(qk.discoveryResults(57))?.map((r) => r.ip)).toEqual([
      '192.168.1.3',
      '192.168.1.20',
    ]);
    // Results of a scan we are not looking at are not cached.
    applyLiveMessage(
      qc,
      { type: 'discovery.result', ts: 'x', data: { scan_id: 58, result: result({ scan_id: 58 }) } },
      batch,
    );
    expect(qc.getQueryData(qk.discoveryResults(58))).toBeUndefined();

    applyLiveMessage(
      qc,
      {
        type: 'discovery.finished',
        ts: '2026-11-04T12:09:00Z',
        data: {
          scan_id: 57,
          status: 'cancelled',
          total_hosts: 254,
          scanned_hosts: 120,
          found_hosts: 3,
          new_hosts: 2,
          error: null,
        },
      },
      batch,
    );
    expect(qc.getQueryData<DiscoveryScan>(qk.discoveryScan(57))?.status).toBe('cancelled');
    const keys = [...(batch as unknown as { keys: Map<string, unknown> }).keys.keys()];
    expect(keys).toContain(JSON.stringify(qk.discoveryScan(57)));
    expect(keys).toContain(JSON.stringify(qk.discoveryResults(57)));

    applyLiveMessage(
      qc,
      {
        type: 'config.changed',
        ts: 'x',
        data: { entity: 'discovery_profile', id: 2, action: 'updated' },
      },
      batch,
    );
    expect([...(batch as unknown as { keys: Map<string, unknown> }).keys.keys()]).toContain(
      JSON.stringify(qk.discoveryProfiles),
    );
    batch.dispose();
  });

  it('filters results like the server filters', () => {
    const list = [
      result({ id: 1, ip: '10.0.0.1', is_new: true, device_class: 'router', sys_name: 'OPNsense' }),
      result({
        id: 2,
        ip: '10.0.0.2',
        is_new: false,
        existing_device_id: 4,
        device_class: 'printer',
      }),
      result({ id: 3, ip: '10.0.0.3', is_new: false, hostname: 'nas.lan', device_class: 'nas' }),
    ];
    expect(filterResults(list, NO_FILTERS)).toHaveLength(3);
    expect(filterResults(list, { ...NO_FILTERS, onlyNew: true }).map((r) => r.id)).toEqual([1]);
    expect(filterResults(list, { ...NO_FILTERS, hideExisting: true }).map((r) => r.id)).toEqual([
      1, 3,
    ]);
    expect(
      filterResults(list, { ...NO_FILTERS, classes: ['nas', 'printer'] }).map((r) => r.id),
    ).toEqual([2, 3]);
    expect(filterResults(list, { ...NO_FILTERS, q: 'opn' }).map((r) => r.id)).toEqual([1]);
    expect(filterResults(list, { ...NO_FILTERS, q: 'NAS.L' }).map((r) => r.id)).toEqual([3]);
  });
});

describe('scan options form', () => {
  it('builds a request with defaults and secrets for a new scan', () => {
    const f = scanOptionsForm();
    f.extra_communities = ['printers', '', ' monitor '];
    f.snmp_port = '1161';
    const { options, errors } = scanOptionsToInput(f);
    expect(errors).toEqual({});
    expect(options).toMatchObject({
      snmp_port: 1161,
      extra_communities: ['printers', 'monitor'],
      extra_v3: [],
      tcp_ports: [22, 23, 53, 80, 443, 445, 631, 3389, 8080, 8443, 9100],
    });
  });

  it('keeps stored secrets when editing a profile unless replaced', () => {
    const f = scanOptionsForm(opts);
    expect(f.stored_communities).toBe(1);
    expect(f.extra_communities).toEqual(['']);
    const { options } = scanOptionsToInput(f);
    expect('extra_communities' in options).toBe(false);
    expect('extra_v3' in options).toBe(false);
    const replaced = scanOptionsToInput({ ...f, replace_secrets: true }).options;
    expect(replaced.extra_communities).toEqual([]);
  });

  it('validates options', () => {
    const f = { ...scanOptionsForm(), ping: false, snmp: false, tcp: false, tcp_ports: '0, 80' };
    const e = scanOptionsToInput(f).errors;
    expect(e.probes).toBe('discovery.err.probes');
    expect(
      scanOptionsToInput({ ...scanOptionsForm(), tcp_ports: '22, x' }).errors.tcp_ports,
    ).toBeTruthy();
    expect(
      scanOptionsToInput({ ...scanOptionsForm(), snmp_port: '70000' }).errors.snmp_port,
    ).toBeTruthy();
    expect(parsePorts('22 80,80;443')).toEqual([22, 80, 443]);
    const v3 = scanOptionsForm();
    v3.extra_v3 = [
      {
        v3_username: 'mon',
        v3_security_level: 'authNoPriv',
        v3_auth_protocol: 'SHA',
        v3_auth_key: 'k',
        v3_priv_protocol: 'AES128',
        v3_priv_key: 'p',
      },
    ];
    expect(scanOptionsToInput(v3).options.extra_v3).toEqual([
      {
        v3_username: 'mon',
        v3_security_level: 'authNoPriv',
        v3_auth_protocol: 'SHA',
        v3_auth_key: 'k',
        v3_priv_protocol: null,
        v3_priv_key: null,
      },
    ]);
  });

  it('maps schedules', () => {
    expect(scheduleChoice(false, 24)).toBe('off');
    expect(scheduleChoice(true, 24)).toBe('daily');
    expect(scheduleChoice(true, 168)).toBe('weekly');
    expect(scheduleChoice(true, 6)).toBe('hours');
    expect(scheduleFromChoice('hours', '6')).toEqual({
      schedule_enabled: true,
      schedule_every_hours: 6,
    });
    expect(scheduleFromChoice('hours', '721')).toBeNull();
    expect(scheduleFromChoice('off', '6')).toEqual({
      schedule_enabled: false,
      schedule_every_hours: 6,
    });
  });
});

describe('template result mapping', () => {
  const t = i18n.getFixedT('en');
  it('summarises apply results and groups skip reasons', () => {
    const r = {
      device_id: 9,
      template_id: 'opnsense',
      created: [{ sensor_id: 101, name: 'CPU Load', type: 'snmp_cpu' as const }],
      skipped: [
        { name: 'Ping', type: 'ping' as const, reason: 'exists' as const, message: '' },
        {
          name: 'Interfaces',
          type: 'snmp_interface' as const,
          reason: 'max_reached' as const,
          message: '3 more',
        },
        {
          name: 'Memory',
          type: 'snmp_memory' as const,
          reason: 'probe_failed' as const,
          message: '',
        },
      ],
    };
    expect(applySummary(r)).toEqual({ created: 1, skipped: 2, existing: 1 });
    expect(groupSkipped(r.skipped).map((g) => g.reason)).toEqual([
      'probe_failed',
      'max_reached',
      'exists',
    ]);
    expect(skipReasonKey('no_rows')).toBe('templates.skip.no_rows');
    expect(skipReasonKey('weird')).toBe('templates.skip.other');
    for (const reason of [
      'exists',
      'probe_failed',
      'snmp_error',
      'max_reached',
      'no_rows',
      'other',
    ])
      expect(i18n.getFixedT('he')(`templates.skip.${reason}`)).not.toMatch(/^templates\./);
  });

  it('maps import responses to one row per requested item', () => {
    const items = [
      { result_id: 812, template_id: 'opnsense', name: null },
      { result_id: 815, template_id: 'unifi_ap', name: 'AP Office' },
      { result_id: 900, template_id: 'generic_ping' },
    ];
    const res: ImportResponse = {
      created: [
        {
          result_id: 812,
          device_id: 21,
          device_name: 'OPNsense',
          template_id: 'opnsense',
          sensors_created: 9,
          sensors_skipped: [],
        },
      ],
      errors: [
        {
          result_id: 815,
          code: 'device_exists',
          message: 'A device with this host already exists',
          device_id: 4,
        },
      ],
    };
    const rows = importRows(items, res, [
      result({ id: 812, ip: '192.168.1.1' }),
      result({ id: 815, ip: '192.168.1.5' }),
    ]);
    expect(rows.map((r) => r.kind)).toEqual(['created', 'error', 'missing']);
    expect(rows[0]).toMatchObject({ ip: '192.168.1.1', device_id: 21, sensors_created: 9 });
    expect(rows[1]).toMatchObject({ ip: '192.168.1.5', code: 'device_exists', device_id: 4 });
    expect(rows[2].ip).toBe('#900');
  });

  it('isolates LTR tokens inside Hebrew text', () => {
    expect(isolateLtrRuns('Disk /volume1')).toBe('Disk /volume1');
    expect(isolateLtrRuns('דיסק /volume1')).toBe('דיסק \u2066/volume1\u2069');
    expect(isolateLtrRuns('שמות התואמים ל-^eth0$')).toBe('שמות התואמים ל-\u2066^eth0$\u2069');
    expect(isolateLtrRuns('דיסק C:\\')).toBe('דיסק \u2066C:\\\u2069');
  });

  it('describes entries in the current language', () => {
    expect(l10n({ en: 'Ping', he: 'פינג' }, 'he')).toBe('פינג');
    expect(l10n({ en: 'Ping', he: '' }, 'he')).toBe('Ping');
    expect(
      describeEntry(
        {
          kind: 'snmp_interfaces',
          oper_up_only: true,
          exclude_if_types: [24],
          include_name_regex: null,
          exclude_name_regex: null,
          max: 16,
          channels: [],
        },
        'en',
        t,
      ),
    ).toBe('Interfaces that are up (max 16)');
    expect(
      describeEntry(
        {
          kind: 'sensor',
          type: 'snmp_cpu',
          name: { en: 'CPU Load', he: 'עומס מעבד' },
          config: {},
          channels: [],
          interval_s: null,
          probe: true,
        },
        'en',
        t,
      ),
    ).toBe('CPU Load (SNMP CPU) — if available');
  });
});

describe('snmp_custom / snmp_disk forms', () => {
  const fields: ConfigField[] = [
    { key: 'oid', type: 'str', required: true, default: null, min: null, max: null, options: null },
    {
      key: 'value_type',
      type: 'enum',
      required: false,
      default: 'gauge',
      min: null,
      max: null,
      options: ['gauge', 'counter', 'string'],
    },
    { key: 'unit', type: 'str', required: false, default: '', min: null, max: null, options: null },
    {
      key: 'multiplier',
      type: 'float',
      required: false,
      default: 1.0,
      min: -1e12,
      max: 1e12,
      options: null,
    },
    {
      key: 'label',
      type: 'str',
      required: false,
      default: '',
      min: null,
      max: null,
      options: null,
    },
    {
      key: 'percent_of_oid',
      type: 'str_nullable',
      required: false,
      default: null,
      min: null,
      max: null,
      options: null,
    },
    {
      key: 'expected',
      type: 'str_nullable',
      required: false,
      default: null,
      min: null,
      max: null,
      options: null,
    },
    {
      key: 'match',
      type: 'enum',
      required: false,
      default: 'equals',
      min: null,
      max: null,
      options: ['equals', 'contains'],
    },
  ];

  it('shows percent_of_oid only for gauge and expected/match only for string', () => {
    const raw = rawFromConfig(fields);
    expect(visibleConfigFields('snmp_custom', fields, raw).map((f) => f.key)).toEqual([
      'oid',
      'value_type',
      'unit',
      'multiplier',
      'label',
      'percent_of_oid',
    ]);
    expect(
      visibleConfigFields('snmp_custom', fields, { ...raw, value_type: 'string' }).map(
        (f) => f.key,
      ),
    ).toContain('expected');
  });

  it('parses floats and nulls fields that do not apply', () => {
    const raw = {
      ...rawFromConfig(fields),
      oid: ' 1.3.6.1.2.1.1.3.0 ',
      multiplier: '0.1',
      value_type: 'counter',
      percent_of_oid: '1.3.6.1.2',
    };
    const { config, errors } = buildSensorConfig('snmp_custom', fields, raw);
    expect(errors).toEqual({});
    expect(config).toMatchObject({
      oid: '1.3.6.1.2.1.1.3.0',
      multiplier: 0.1,
      percent_of_oid: null,
      expected: null,
    });
    expect(
      buildSensorConfig('snmp_custom', fields, { ...raw, multiplier: '1e3' }).config.multiplier,
    ).toBe(1000);
    expect(
      buildSensorConfig('snmp_custom', fields, { ...raw, multiplier: 'abc' }).errors.multiplier,
    ).toBe('validation.number');
    expect(buildSensorConfig('snmp_custom', fields, { ...raw, oid: '' }).errors.oid).toBe(
      'validation.required',
    );
  });

  it('hides the informational storage_index of snmp_disk', () => {
    const disk: ConfigField[] = [
      {
        key: 'storage_descr',
        type: 'str',
        required: true,
        default: null,
        min: null,
        max: null,
        options: null,
      },
      {
        key: 'storage_index',
        type: 'int_nullable',
        required: false,
        default: null,
        min: null,
        max: null,
        options: null,
      },
    ];
    expect(visibleConfigFields('snmp_disk', disk, {}).map((f) => f.key)).toEqual(['storage_descr']);
    expect(buildSensorConfig('snmp_disk', disk, { storage_descr: 'C:\\' }).config).toEqual({
      storage_descr: 'C:\\',
    });
  });

  it('formats custom values', () => {
    void i18n.changeLanguage('en');
    expect(
      sensorValueText(
        { type: 'snmp_custom', config: { value_type: 'gauge' } },
        'value',
        12,
        'clients',
      ),
    ).toBe('12 clients');
    expect(
      sensorValueText({ type: 'snmp_custom', config: { value_type: 'gauge' } }, 'value', 12, ''),
    ).toBe('12');
    expect(
      sensorValueText({ type: 'snmp_custom', config: { value_type: 'string' } }, 'value', 1, ''),
    ).toBe('Matches');
    expect(sensorValueText({ type: 'snmp_disk', config: {} }, 'disk_free_bytes', 2048, 'B')).toBe(
      '2 KiB',
    );
  });
});

describe('telegram + new errors', () => {
  it('validates bot tokens and chat ids', () => {
    expect(isTelegramToken('123456789:AAEhBP0av28rmzBYNkKz3x2ZxM9qO9vLdIo')).toBe(true);
    expect(isTelegramToken('123456:TEST_TOKEN_valid_0123456789abcdef')).toBe(true);
    expect(isTelegramToken('12:short')).toBe(false);
    expect(isTelegramChatId('-1001234567890')).toBe(true);
    expect(isTelegramChatId('@netmon_test')).toBe(true);
    expect(isTelegramChatId('@ab')).toBe(false);
    expect(isTelegramChatId('chat')).toBe(false);
  });

  it('translates the Phase 2a error codes', () => {
    const t = i18n.getFixedT('en');
    expect(
      errorMessage(t, new ApiError(502, 'telegram_error', 'Bad Request: chat not found')),
    ).toBe('Sending the Telegram message failed: Bad Request: chat not found');
    expect(
      errorMessage(t, new ApiError(422, 'too_many_hosts', 'x', [{ count: 65534, max: 4096 }])),
    ).toBe('The targets expand to 65534 hosts; the limit is 4096.');
    expect(errorMessage(t, new ApiError(409, 'scan_running', 'x', [{ scan_id: 12 }]))).toBe(
      'A discovery scan is already running.',
    );
    const he = i18n.getFixedT('he');
    expect(errorMessage(he, new ApiError(409, 'scan_not_running', 'x'))).toBe(
      'הסריקה הזו כבר אינה פועלת.',
    );
  });

  it('uses Hebrew plural forms', () => {
    const he = i18n.getFixedT('he');
    expect(he('discovery.hostsCount', { count: 1 })).toBe('מארח אחד');
    expect(he('discovery.hostsCount', { count: 2 })).toBe('שני מארחים');
    expect(he('discovery.hostsCount', { count: 254 })).toBe('254 מארחים');
    expect(i18n.getFixedT('en')('discovery.sensorsCreated', { count: 1 })).toBe('1 sensor created');
  });
});

describe('fix round', () => {
  it('buffers live results that arrive before the results query has data', () => {
    const qc = new QueryClient();
    const batch = new InvalidationBatcher(qc, 10_000);
    const r1 = result({ id: 901, scan_id: 77, ip: '10.9.0.5' });
    const r2 = result({ id: 902, scan_id: 77, ip: '10.9.0.2' });
    // No cached list yet (reload during a running scan): nothing is dropped.
    applyLiveMessage(
      qc,
      { type: 'discovery.result', ts: 'x', data: { scan_id: 77, result: r1 } },
      batch,
    );
    applyLiveMessage(
      qc,
      { type: 'discovery.result', ts: 'x', data: { scan_id: 77, result: r2 } },
      batch,
    );
    expect(qc.getQueryData(qk.discoveryResults(77))).toBeUndefined();
    // The fetch lands with r1 (newer copy) and another row: merged by id, fetched rows win.
    const fetched = [
      { ...r1, imported_device_id: 5 },
      result({ id: 903, scan_id: 77, ip: '10.9.0.9' }),
    ];
    const merged = withBuffered(77, fetched);
    expect(merged.map((r) => r.id)).toEqual([902, 901, 903]);
    expect(merged.find((r) => r.id === 901)?.imported_device_id).toBe(5);
    expect(withBuffered(77, merged)).toHaveLength(3); // idempotent, no duplicates
    applyLiveMessage(
      qc,
      {
        type: 'discovery.finished',
        ts: 'x',
        data: {
          scan_id: 77,
          status: 'completed',
          total_hosts: 1,
          scanned_hosts: 1,
          found_hosts: 3,
          new_hosts: 0,
          error: null,
        },
      },
      batch,
    );
    expect(withBuffered(77, [])).toEqual([]);
    bufferResult(r1);
    clearBuffered(77);
    batch.dispose();
  });

  it('marks time-budget and internal import errors as retryable', () => {
    const items = [
      { result_id: 1, template_id: 'generic_ping' },
      { result_id: 2, template_id: 'generic_ping' },
      { result_id: 3, template_id: 'generic_ping' },
      { result_id: 4, template_id: 'generic_ping' },
    ];
    const rows = importRows(
      items,
      {
        created: [],
        errors: [
          {
            result_id: 1,
            code: 'internal_error',
            message: 'Import time budget exceeded; retry the remaining hosts',
            device_id: null,
          },
          { result_id: 2, code: 'internal_error', message: 'Internal error', device_id: null },
          { result_id: 3, code: 'device_exists', message: 'x', device_id: 9 },
        ],
      },
      [],
    );
    expect(rows[0]).toMatchObject({ kind: 'error', retry: true, timedOut: true });
    expect(rows[1]).toMatchObject({ kind: 'error', retry: true, timedOut: false });
    expect(rows[2]).toMatchObject({ kind: 'error', retry: false });
    expect(retryIds(rows)).toEqual([1, 2, 4]);
    expect(i18n.getFixedT('he')('discovery.notImportedRetry')).toBe('לא יובא — נסו שוב');
  });

  it('extra communities are a list of rows', () => {
    const f = scanOptionsForm();
    expect(f.extra_communities).toEqual(['']);
    expect(scanOptionsToInput(f).options.extra_communities).toEqual([]);
    const six = { ...f, extra_communities: ['a', 'b', 'c', 'd', 'e', 'f'] };
    expect(scanOptionsToInput(six).errors.extra_communities).toBe('discovery.err.communities');
  });

  it('storage usage with unknown sizes', () => {
    expect(storageUsedPct({ size_bytes: 200, used_bytes: 50 })).toBe(25);
    expect(storageUsedPct({ size_bytes: null, used_bytes: 50 })).toBeNull();
    expect(storageUsedPct({ size_bytes: 100, used_bytes: null })).toBeNull();
    expect(storageUsedPct({ size_bytes: 0, used_bytes: 0 })).toBeNull();
  });
});
