import { describe, expect, it } from 'vitest';
import { ApiError } from '../api/client';
import { FEATURE_NAMES } from '../api/types';
import type { EventRule, Port, Settings, SyslogMessage, Trap, ApiToken } from '../api/types';
import { changeRows, auditTone, changeText } from './audit';
import {
  formToInput,
  IF_INDEX_OID,
  matchToForm,
  regexGroups,
  reorderPatches,
  ruleFromSyslog,
  ruleFromTrap,
  ruleToForm,
  validateRule,
} from './eventRules';
import {
  ALL_OFF,
  disabledFeature,
  featureHints,
  featureStatus,
  featuresOfTab,
  featureTab,
  ingestOn,
  ldapMissing,
  movedFeatures,
  normalizeFeatures,
  SETTINGS_TABS,
} from './features';
import { parseSources, v3Input, v3RowError, v3Rows, newV3Row } from './ingestSettings';
import { lagColorByMember, lagGroups, lagMemberNames, LAG_PALETTE } from './lag';
import {
  baseDnFromDomain,
  isLdapServer,
  ldapEnableState,
  ldapForm,
  ldapInput,
  ldapKey,
  looksLikeDn,
  parseLogin,
} from './ldap';
import {
  emptyFilter,
  isIp,
  isIpOrCidr,
  isOid,
  liveAllowed,
  matchesSyslog,
  matchesTrap,
  mergeNewest,
  parseList,
  rangeOf,
  severityTone,
  syslogQuery,
  ticksToSeconds,
  trapQuery,
} from './logs';
import { newTarget, validateTarget } from './siem';
import { isLegacyTeamsHost, teamsUrlError, urlHost } from './teams';
import { availableKinds, groupByDay, timelineHref } from './timeline';
import { daysLeft, expiresDaysOk, sortTokens, tokenRoles } from './tokens';

const NOW = Date.parse('2026-11-08T12:00:00Z');

function syslog(p: Partial<SyslogMessage> = {}): SyslogMessage {
  return {
    id: 1,
    time: '2026-11-08T11:59:00Z',
    device_time: null,
    source_ip: '10.0.0.12',
    device_id: 3,
    device_name: 'sw1',
    transport: 'udp',
    format: 'rfc5424',
    facility: 23,
    severity: 3,
    hostname: 'FF5945-IRF',
    program: 'IFNET',
    pid: '',
    msgid: 'PHY_UPDOWN',
    structured_data: null,
    message: 'Physical state on the interface Ten-GigabitEthernet1/0/49 changed to down.',
    truncated: false,
    matched_rule_ids: [4],
    ...p,
  };
}
function trap(p: Partial<Trap> = {}): Trap {
  return {
    id: 7,
    time: '2026-11-08T11:59:00Z',
    source_ip: '10.0.0.2',
    agent_addr: null,
    device_id: 3,
    device_name: 'sw1',
    version: 'v2c',
    pdu: 'trap',
    security_name: 'public',
    trap_oid: '1.3.6.1.6.3.1.1.5.3',
    trap_name: 'linkDown',
    uptime_ticks: 123456,
    summary: 'linkDown ifIndex=5 GigabitEthernet1/0/5',
    varbinds: [
      {
        oid: '1.3.6.1.2.1.2.2.1.1.5',
        name: 'ifIndex.5',
        type: 'Integer',
        value: '5',
        display: '5',
      },
    ],
    matched_rule_ids: [],
    ...p,
  };
}

describe('[2b-3] features', () => {
  it('switches each feature in exactly one tab: its own settings tab, else Features', () => {
    expect(featuresOfTab('ldap')).toEqual(['ldap']);
    expect(featuresOfTab('siem')).toEqual(['siem_forwarding']);
    expect(featuresOfTab('audit')).toEqual(['audit']);
    expect(featuresOfTab('ingest')).toEqual(['traps', 'syslog', 'event_alerts', 'flapping']);
    expect(featuresOfTab('features')).toEqual(['lag', 'timeline']);
    // owner decision: Teams and API tokens are always available, not toggles
    expect(FEATURE_NAMES).toHaveLength(9);
    expect(FEATURE_NAMES as string[]).not.toContain('teams');
    expect(FEATURE_NAMES as string[]).not.toContain('api_tokens');
    expect(featuresOfTab('general')).toEqual([]);
    const all = SETTINGS_TABS.flatMap((t) => featuresOfTab(t));
    expect([...all].sort()).toEqual([...FEATURE_NAMES].sort());
    expect(movedFeatures()).not.toContain('lag');
    expect(movedFeatures().every((n) => featureTab(n) !== 'features')).toBe(true);
    expect(movedFeatures().length + featuresOfTab('features').length).toBe(FEATURE_NAMES.length);
  });
  it('defaults every feature to off and fills missing keys', () => {
    expect(Object.values(ALL_OFF).every((v) => v === false)).toBe(true);
    const f = normalizeFeatures({ traps: true, ldap: 'yes' as unknown as boolean });
    expect(f.traps).toBe(true);
    expect(f.ldap).toBe(false);
    expect(f.timeline).toBe(false);
    expect('api_tokens' in f).toBe(false);
    expect(normalizeFeatures(undefined)).toEqual(ALL_OFF);
  });
  it('reads the feature of a 409 feature_disabled error', () => {
    const e = new ApiError(409, 'feature_disabled', 'off', [{ feature: 'traps' }]);
    expect(disabledFeature(e)).toBe('traps');
    expect(disabledFeature(new ApiError(409, 'conflict', 'x'))).toBeNull();
    expect(disabledFeature(new Error('x'))).toBeNull();
  });
  it('gives dependency hints', () => {
    const f = { ...ALL_OFF, event_alerts: true };
    expect(featureHints('event_alerts', f).map((h) => h.key)).toContain(
      'features.hint.needsIngest',
    );
    expect(featureHints('event_alerts', { ...f, syslog: true })).toEqual([]);
    expect(ingestOn({ ...ALL_OFF, traps: true })).toBe(true);
  });
  it('builds receiver status lines', () => {
    const f = { ...ALL_OFF, traps: true, syslog: true };
    const ingest = {
      receivers: [
        { kind: 'trap', port: 162, enabled: true, state: 'listening', error: null, since: null },
        {
          kind: 'syslog_udp',
          port: 514,
          enabled: true,
          state: 'error',
          error: 'Address in use',
          since: null,
        },
        {
          kind: 'syslog_tcp',
          port: 0,
          enabled: false,
          state: 'disabled',
          error: null,
          since: null,
        },
      ],
      forwarders: [],
    } as unknown as Parameters<typeof featureStatus>[2];
    expect(featureStatus('traps', f, ingest)).toEqual([
      { key: 'features.status.listening', params: { proto: 'UDP', port: 162 }, tone: 'ok' },
    ]);
    const s = featureStatus('syslog', f, ingest);
    expect(s[0]).toMatchObject({ key: 'features.status.error', tone: 'error' });
    expect(s[1]).toMatchObject({ key: 'features.status.socketOff' });
    expect(featureStatus('traps', ALL_OFF, ingest)).toEqual([]);
  });
  it('lists LDAP fields that block turning the feature on', () => {
    const l = {
      servers: [],
      security: 'ldaps',
      tls_verify: true,
      domain: '',
      netbios_domain: '',
      base_dn: '',
      bind_dn: '',
      bind_password: null,
      bind_password_set: false,
      bind_password_unreadable: false,
      admin_group_dn: '',
      viewer_group_dn: '',
      timeout_s: 5,
    } as Settings['ldap'] & object;
    expect(ldapMissing(l)).toEqual(['bind_dn', 'bind_password', 'servers', 'base_dn', 'group_dn']);
    expect(
      ldapMissing({
        ...l,
        domain: 'mnidom.net',
        bind_dn: 'svc@mnidom.net',
        bind_password_set: true,
        viewer_group_dn: 'CN=V,DC=mnidom,DC=net',
      }),
    ).toEqual([]);
  });
});

describe('[2b-3] logs', () => {
  it('computes preset and custom ranges (≤ 31 days)', () => {
    expect(rangeOf({ preset: '1h', from: '', to: '' }, NOW)).toEqual({
      ok: true,
      since: '2026-11-08T11:00:00.000Z',
    });
    const r = rangeOf({ preset: 'custom', from: '2026-11-01T00:00', to: '2026-11-02T00:00' }, NOW);
    expect(r.ok).toBe(true);
    expect(
      rangeOf({ preset: 'custom', from: '2026-11-02T00:00', to: '2026-11-01T00:00' }, NOW),
    ).toEqual({ ok: false, error: 'order' });
    expect(
      rangeOf({ preset: 'custom', from: '2026-09-01T00:00', to: '2026-11-01T00:00' }, NOW),
    ).toEqual({ ok: false, error: 'tooLong' });
    expect(rangeOf({ preset: 'custom', from: '', to: '' }, NOW)).toEqual({
      ok: false,
      error: 'invalid',
    });
    expect(liveAllowed({ preset: '24h', to: '' })).toBe(true);
    expect(liveAllowed({ preset: 'custom', to: '2026-11-02T00:00' })).toBe(false);
  });
  it('builds query parameters', () => {
    const f = emptyFilter({
      severityMax: 3,
      facilities: [23, 4],
      programs: ['IFNET'],
      q: '  down ',
    });
    expect(syslogQuery(f, { since: 'S' })).toEqual({
      device_id: undefined,
      group_id: undefined,
      unknown: undefined,
      source_ip: undefined,
      severity_max: 3,
      facility: [4, 23],
      program: ['IFNET'],
      hostname: undefined,
      rule_id: undefined,
      q: 'down',
      since: 'S',
      until: undefined,
    });
    expect(trapQuery(emptyFilter({ trapOid: '.1.3.6.1.6.3.1.1.5' }), { since: 'S' }).trap_oid).toBe(
      '1.3.6.1.6.3.1.1.5',
    );
  });
  it('filters live syslog records like the API', () => {
    const f = emptyFilter({ severityMax: 3, programs: ['ifnet'], q: 'TEN-GIGABIT' });
    expect(matchesSyslog(syslog(), f)).toBe(true);
    expect(matchesSyslog(syslog({ severity: 4 }), f)).toBe(false);
    expect(matchesSyslog(syslog({ program: 'SHELL' }), f)).toBe(false);
    expect(matchesSyslog(syslog(), emptyFilter({ facilities: [16] }))).toBe(false);
    expect(matchesSyslog(syslog(), emptyFilter({ unknown: true }))).toBe(false);
    expect(matchesSyslog(syslog({ device_id: null }), emptyFilter({ unknown: true }))).toBe(true);
    expect(matchesSyslog(syslog(), emptyFilter({ groupId: 1 }), new Set([3]))).toBe(true);
    expect(matchesSyslog(syslog(), emptyFilter({ groupId: 1 }), new Set([9]))).toBe(false);
    expect(matchesSyslog(syslog(), emptyFilter({ ruleId: 4 }))).toBe(true);
    expect(matchesSyslog(syslog(), emptyFilter({ hostname: 'ff5945-irf' }))).toBe(true);
  });
  it('filters live traps', () => {
    expect(matchesTrap(trap(), emptyFilter({ trapOid: '1.3.6.1.6.3.1.1.5' }))).toBe(true);
    expect(matchesTrap(trap(), emptyFilter({ trapOid: '1.3.6.1.4' }))).toBe(false);
    expect(matchesTrap(trap(), emptyFilter({ q: 'ifindex=5' }))).toBe(true);
    expect(matchesTrap(trap(), emptyFilter({ deviceId: 4 }))).toBe(false);
  });
  it('merges live batches newest first without duplicates', () => {
    const a = [syslog({ id: 1, time: '2026-11-08T11:00:00Z' })];
    const out = mergeNewest(
      [
        syslog({ id: 3, time: '2026-11-08T11:02:00Z' }),
        syslog({ id: 1 }),
        syslog({ id: 2, time: '2026-11-08T11:01:00Z' }),
      ],
      a,
    );
    expect(out.map((x) => x.id)).toEqual([3, 2, 1]);
    expect(mergeNewest([syslog({ id: 9 })], a, 1).map((x) => x.id)).toEqual([9]);
  });
  it('validates addresses and OIDs', () => {
    expect(isIp('10.0.0.1')).toBe(true);
    expect(isIp('fe80::1')).toBe(true);
    expect(isIp('10.0.0.256')).toBe(false);
    expect(isIp('host.local')).toBe(false);
    expect(isIpOrCidr('10.9.0.0/16')).toBe(true);
    expect(isIpOrCidr('10.9.0.0/33')).toBe(false);
    expect(isIpOrCidr('2001:db8::/32')).toBe(true);
    expect(isOid('1.3.6.1.2.1.2.2.1.7')).toBe(true);
    expect(isOid('.1.3.6')).toBe(true);
    expect(isOid('ifIndex')).toBe(false);
    expect(parseList('a, b;c  a')).toEqual(['a', 'b', 'c']);
    expect(ticksToSeconds(123456)).toBe(1234);
  });
  it('maps severities to the contract colours', () => {
    expect([0, 1, 2, 3, 4, 5, 6, 7].map(severityTone)).toEqual([
      'red',
      'red',
      'red',
      'orange',
      'amber',
      'sky',
      'sky',
      'slate',
    ]);
  });
});

const BUILTIN_LINKDOWN: EventRule = {
  id: 1,
  name: 'Link down',
  kind: 'trap',
  enabled: false,
  builtin: 'trap_link_down',
  priority: 1000,
  scope: 'global',
  group_id: null,
  device_id: null,
  scope_name: null,
  match: {
    trap_oids: ['1.3.6.1.6.3.1.1.5.3'],
    varbinds: [{ oid: '1.3.6.1.2.1.2.2.1.7', op: 'ne', value: '2' }],
    ports: 'important',
  },
  action: 'warning',
  key_varbind_oid: IF_INDEX_OID,
  key_regex: null,
  clear_match: { trap_oids: ['1.3.6.1.6.3.1.1.5.4'], varbinds: [], ports: 'any' },
  clear_after_min: 1440,
  active_conditions: 0,
  last_matched_at: null,
  created_at: '',
  updated_at: '',
};

describe('[2b-3] event rules', () => {
  it('round-trips a built-in rule through the form', () => {
    const input = formToInput(ruleToForm(BUILTIN_LINKDOWN));
    expect(input.match).toEqual(BUILTIN_LINKDOWN.match);
    expect(input.clear_match).toEqual(BUILTIN_LINKDOWN.clear_match);
    expect(input.key_varbind_oid).toBe(IF_INDEX_OID);
    expect(input.clear_after_min).toBe(1440);
    expect(validateRule(ruleToForm(BUILTIN_LINKDOWN))).toEqual({});
  });
  it('drops key and clearing for ignore rules', () => {
    const f = ruleToForm(BUILTIN_LINKDOWN);
    f.action = 'ignore';
    const i = formToInput(f);
    expect(i.key_varbind_oid).toBeNull();
    expect(i.clear_match).toBeNull();
    expect(i.clear_after_min).toBeNull();
    expect(validateRule(f)).toEqual({});
  });
  it('requires a way to clear warning/down conditions', () => {
    const f = ruleToForm(null, 'syslog');
    f.name = 'x';
    f.clearAfterMin = '';
    expect(validateRule(f).clear).toBe('eventRules.err.needClear');
    f.clearAfterMin = '20000';
    expect(validateRule(f).clearAfterMin).toBe('eventRules.err.clearAfter');
  });
  it('validates regexes, OIDs, scope and the key group', () => {
    const f = ruleToForm(null, 'syslog');
    f.name = 'x';
    f.match.text = '(';
    f.match.textRegex = true;
    f.keyRegex = 'interface \\S+';
    f.scope = 'device';
    const e = validateRule(f);
    expect(e['match.text']).toBe('eventRules.err.regex');
    expect(e.key).toBe('eventRules.err.keyGroup');
    expect(e.scope).toBe('eventRules.err.scopeDevice');
    const t = ruleToForm(null, 'trap');
    t.name = 'y';
    t.match.trapOids = 'linkDown';
    t.match.varbinds = [{ oid: 'x', op: 'eq', value: '1' }];
    expect(validateRule(t)['match.trapOids']).toBe('eventRules.err.oid');
    expect(validateRule(t)['match.varbinds.0.oid']).toBe('eventRules.err.oid');
  });
  it('counts capture groups, Python named groups included', () => {
    expect(regexGroups('interface (\\S+)')).toBe(1);
    expect(regexGroups('(?P<port>\\d+)')).toBe(1);
    expect(regexGroups('(?:a)(b)(c)')).toBe(2);
    expect(regexGroups('(')).toBeNull();
  });
  it('exists/absent varbind conditions send no value', () => {
    const f = ruleToForm(null, 'trap');
    f.match.varbinds = [{ oid: '1.3.6.1.2.1.2.2.1.1', op: 'exists', value: 'ignored' }];
    expect((formToInput(f).match as { varbinds: unknown[] }).varbinds).toEqual([
      { oid: '1.3.6.1.2.1.2.2.1.1', op: 'exists', value: null },
    ]);
  });
  it('reorders with a single PATCH when a free priority exists', () => {
    const rules = [
      { id: 1, priority: 100 },
      { id: 2, priority: 200 },
      { id: 3, priority: 300 },
    ];
    expect(reorderPatches(rules, 2, 0)).toEqual([{ id: 3, priority: 90 }]);
    expect(reorderPatches(rules, 0, 2)).toEqual([{ id: 1, priority: 310 }]);
    expect(reorderPatches(rules, 0, 1)).toEqual([{ id: 1, priority: 250 }]);
    expect(reorderPatches(rules, 1, 1)).toEqual([]);
  });
  it('renumbers when neighbours leave no gap', () => {
    const rules = [
      { id: 1, priority: 10 },
      { id: 2, priority: 11 },
      { id: 3, priority: 12 },
    ];
    expect(reorderPatches(rules, 2, 1)).toEqual([
      { id: 3, priority: 20 },
      { id: 2, priority: 30 },
    ]);
    // ties are ordered by id, like the server
    expect(
      reorderPatches(
        [
          { id: 5, priority: 0 },
          { id: 4, priority: 0 },
        ],
        1,
        0,
      ),
    ).toEqual([
      { id: 5, priority: 10 },
      { id: 4, priority: 20 },
    ]);
  });
  it('prefills rules from a message / trap', () => {
    const s = ruleFromSyslog(syslog());
    expect(s.kind).toBe('syslog');
    expect(s.match.programs).toBe('IFNET');
    expect(s.match.severityMax).toBe(3);
    expect(s.scope).toBe('device');
    expect(s.deviceId).toBe(3);
    const t = ruleFromTrap(trap());
    expect(t.match.trapOids).toBe('1.3.6.1.6.3.1.1.5.3');
    expect(t.keyVarbindOid).toBe(IF_INDEX_OID);
    expect(matchToForm(null).trapOids).toBe('');
  });
});

function port(p: Partial<Port>): Port {
  return {
    if_index: 1,
    name: '1',
    descr: '',
    kind: 'physical',
    lag: null,
    lag_members: [],
    ...p,
  } as Port;
}

describe('[2b-3] LAG', () => {
  const ports = [
    port({ if_index: 23, name: '23', lag: { if_index: 101, name: 'Trk1', attached: true } }),
    port({ if_index: 24, name: '24', lag: { if_index: 101, name: 'Trk1', attached: false } }),
    port({ if_index: 5, name: '5', lag: { if_index: 100, name: 'Trk0', attached: null } }),
    port({ if_index: 101, name: 'Trk1', kind: 'lag', lag_members: [24, 23] }),
    port({ if_index: 100, name: 'Trk0', kind: 'lag', lag_members: [5] }),
    port({ if_index: 102, name: 'Trk2', kind: 'lag', lag_members: [] }),
  ];
  it('assigns palette colours by LAG ifIndex order', () => {
    const g = lagGroups(ports);
    expect(g.map((x) => [x.if_index, x.name, x.members.map((m) => m.if_index)])).toEqual([
      [100, 'Trk0', [5]],
      [101, 'Trk1', [23, 24]],
    ]);
    expect(g[0].color).toBe(LAG_PALETTE[0]);
    expect(g[1].color).toBe(LAG_PALETTE[1]);
    expect(lagColorByMember(g).get(24)).toBe(LAG_PALETTE[1]);
    expect(lagColorByMember(g).has(101)).toBe(false);
  });
  it('names LAG members', () => {
    expect(lagMemberNames(ports[3], ports)).toEqual(['23', '24']);
    expect(lagMemberNames({ lag_members: [99] }, ports)).toEqual(['99']);
  });
});

describe('[2b-3] Active Directory', () => {
  it('configure-first switch: unlocked only by a successful test of the stored settings', () => {
    const l = {
      servers: ['dc1.mnidom.net'],
      security: 'ldaps',
      tls_verify: true,
      domain: 'mnidom.net',
      netbios_domain: 'MNIDOM',
      base_dn: '',
      bind_dn: 'svc@mnidom.net',
      bind_password: null,
      bind_password_set: true,
      bind_password_unreadable: false,
      admin_group_dn: 'CN=A,DC=mnidom,DC=net',
      viewer_group_dn: '',
      timeout_s: 5,
    } as unknown as Parameters<typeof ldapForm>[0];
    const form = ldapForm(l);
    const key = ldapKey(ldapInput(form));
    expect(ldapEnableState({ on: false, form, stored: l, verifiedKey: null })).toBe('test');
    expect(ldapEnableState({ on: false, form, stored: l, verifiedKey: key })).toBe('ok');
    // unsaved edits (or a typed password) need a save first
    const dirty = { ...form, domain: 'other.net' };
    expect(ldapEnableState({ on: false, form: dirty, stored: l, verifiedKey: key })).toBe('save');
    const pw = { ...form, password: 'x' };
    expect(ldapEnableState({ on: false, form: pw, stored: l, verifiedKey: key })).toBe('save');
    // a test of other values does not unlock; turning OFF is always possible
    const other = ldapKey(ldapInput(dirty));
    expect(ldapEnableState({ on: false, form, stored: l, verifiedKey: other })).toBe('test');
    expect(ldapEnableState({ on: true, form: dirty, stored: l, verifiedKey: null })).toBe('ok');
    // the secret is not part of the key
    expect(ldapKey({ ...ldapInput(form), bind_password: 'secret' })).toBe(key);
  });
  it('parses login names like the server (data-model vectors)', () => {
    expect(parseLogin('jdoe', 'MNIDOM', 'mnidom.net')).toEqual({ kind: 'sam', sam: 'jdoe' });
    expect(parseLogin('MNIDOM\\jdoe', 'MNIDOM', 'mnidom.net')).toEqual({
      kind: 'sam',
      sam: 'jdoe',
    });
    expect(parseLogin('mnidom\\jdoe', 'MNIDOM', 'mnidom.net')).toEqual({
      kind: 'sam',
      sam: 'jdoe',
    });
    expect(parseLogin('OTHER\\jdoe', 'MNIDOM', 'mnidom.net')).toEqual({
      kind: 'foreign',
      domain: 'OTHER',
    });
    expect(parseLogin('John.Doe@MNIDOM.NET', 'MNIDOM', 'mnidom.net')).toEqual({
      kind: 'upn',
      upn: 'John.Doe@MNIDOM.NET',
      sam: 'John.Doe',
    });
    expect(parseLogin('x@other.com', 'MNIDOM', 'mnidom.net')).toEqual({
      kind: 'upn',
      upn: 'x@other.com',
      sam: null,
    });
  });
  it('validates servers and DNs', () => {
    expect(isLdapServer('dc1.mnidom.net')).toBe(true);
    expect(isLdapServer('dc2.mnidom.net:636')).toBe(true);
    expect(isLdapServer('[2001:db8::1]:636')).toBe(true);
    expect(isLdapServer('ldap://dc1')).toBe(false);
    expect(isLdapServer('dc1:99999')).toBe(false);
    expect(looksLikeDn('CN=NetMon-Admins,OU=Groups,DC=mnidom,DC=net')).toBe(true);
    expect(looksLikeDn('CN=A\\,B,DC=x')).toBe(true);
    expect(looksLikeDn('NetMon-Admins')).toBe(false);
    expect(baseDnFromDomain('mnidom.net')).toBe('DC=mnidom,DC=net');
  });
  it('never echoes the bind password: omitted keeps, "" clears, text replaces', () => {
    const f = ldapForm(undefined);
    expect('bind_password' in ldapInput(f)).toBe(false);
    expect(ldapInput({ ...f, clearPassword: true }).bind_password).toBe('');
    expect(ldapInput({ ...f, password: 's3cret' }).bind_password).toBe('s3cret');
    expect(ldapInput({ ...f, servers: 'dc1\n dc2, dc3' }).servers).toEqual(['dc1', 'dc2', 'dc3']);
  });
});

describe('[2b-3] Teams, tokens, audit, SIEM', () => {
  it('detects legacy Office 365 connector URLs', () => {
    expect(isLegacyTeamsHost(urlHost('https://mnidom.webhook.office.com/webhookb2/x'))).toBe(true);
    expect(isLegacyTeamsHost('outlook.office.com')).toBe(true);
    expect(isLegacyTeamsHost('prod-12.westeurope.logic.azure.com')).toBe(false);
    expect(teamsUrlError('', true)).toBe('validation.required');
    expect(teamsUrlError('', false)).toBeNull();
    expect(teamsUrlError('http://x', true)).toBe('teams.urlHttps');
    expect(teamsUrlError(`https://x/${'a'.repeat(2050)}`, true)).toBe('teams.urlLong');
    expect(
      teamsUrlError('https://prod-12.westeurope.logic.azure.com/workflows/1', true),
    ).toBeNull();
  });
  it('limits token roles and expiry', () => {
    expect(tokenRoles('viewer')).toEqual(['viewer']);
    expect(tokenRoles('admin')).toEqual(['viewer', 'admin']);
    expect(expiresDaysOk('90')).toBe(true);
    expect(expiresDaysOk('0')).toBe(false);
    expect(expiresDaysOk('366')).toBe(false);
    const tk = (id: number, state: ApiToken['state'], created_at: string) =>
      ({ id, state, created_at }) as ApiToken;
    expect(
      sortTokens([tk(1, 'revoked', 'b'), tk(2, 'active', 'a'), tk(3, 'active', 'c')]).map(
        (x) => x.id,
      ),
    ).toEqual([3, 2, 1]);
    expect(daysLeft('2026-11-18T12:00:00Z', NOW)).toBe(10);
  });
  it('renders audit diffs without secret values', () => {
    expect(changeRows({ name: ['a', 'b'], 'ldap.bind_password': ['***', '***'] })).toEqual([
      { field: 'ldap.bind_password', old: '***', new: '***', secret: true },
      { field: 'name', old: 'a', new: 'b', secret: false },
    ]);
    expect(changeText(null)).toBe('—');
    expect(changeText({ a: 1 })).toBe('{"a":1}');
    expect(auditTone('auth.login_failed')).toBe('bad');
    expect(auditTone('device.delete')).toBe('warn');
    expect(auditTone('device.update')).toBe('normal');
  });
  it('validates SIEM targets', () => {
    const t = { ...newTarget(), name: 'QRadar', host: 'siem.mnidom.net' };
    expect(validateTarget(t, '', '6514')).toEqual({});
    expect(validateTarget({ ...t, kinds: [] }, '10.0.0.0/8', '0')).toEqual({
      port: 'siem.err.port',
      kinds: 'siem.err.kinds',
    });
    expect(validateTarget(t, 'nonsense', '514').sources).toBe('siem.err.sources');
  });
});

describe('[2b-3] trap v3 users and ignored sources', () => {
  it('keeps stored keys for the same username and clears otherwise', () => {
    const rows = v3Rows([
      {
        username: 'netmon-trap',
        security_level: 'authPriv',
        auth_protocol: 'SHA',
        auth_key: null,
        auth_key_set: true,
        priv_protocol: 'AES128',
        priv_key: null,
        priv_key_set: true,
      },
    ]);
    expect(v3Input(rows)).toEqual([
      {
        username: 'netmon-trap',
        security_level: 'authPriv',
        auth_protocol: 'SHA',
        priv_protocol: 'AES128',
      },
    ]);
    expect(v3RowError(rows[0], rows)).toBeNull();
    const renamed = [{ ...rows[0], username: 'other' }];
    expect(v3RowError(renamed[0], renamed)).toBe('ingestSettings.v3.errAuthKey');
    const noAuth = [{ ...rows[0], security_level: 'noAuthNoPriv' as const }];
    expect(v3Input(noAuth)[0]).toMatchObject({ auth_protocol: null, auth_key: '', priv_key: '' });
    const fresh = { ...newV3Row(), username: 'u', authKey: 'short' };
    expect(v3RowError(fresh, [fresh])).toBe('ingestSettings.v3.errKeyLength');
    expect(v3RowError(rows[0], [rows[0], rows[0]])).toBe('ingestSettings.v3.errDuplicate');
  });
  it('parses the ignored sources list', () => {
    expect(parseSources('10.9.0.0/16\n10.0.0.5, bad')).toEqual({
      list: ['10.9.0.0/16', '10.0.0.5', 'bad'],
      invalid: ['bad'],
      tooMany: false,
    });
  });
});

describe('[2b-3] timeline', () => {
  it('offers trap/syslog/audit kinds only when available', () => {
    expect(availableKinds({ traps: false, syslog: false, audit: true, isAdmin: false })).toEqual([
      'status',
      'notification',
      'ack',
      'maintenance',
      'port',
    ]);
    expect(availableKinds({ traps: true, syslog: true, audit: true, isAdmin: true })).toContain(
      'audit',
    );
  });
  it('links rows', () => {
    const base = {
      id: 1,
      time: '',
      title: '',
      status: null,
      old_status: null,
      severity: null,
      sensor_name: null,
      message: '',
      actor: null,
    };
    expect(
      timelineHref({ ...base, kind: 'status', sensor_id: 7, ref: { type: 'event', id: 1 } }, 3),
    ).toBe('/sensors/7');
    expect(
      timelineHref({ ...base, kind: 'trap', sensor_id: null, ref: { type: 'trap', id: 1 } }, 3),
    ).toBeNull();
    expect(
      timelineHref({ ...base, kind: 'audit', sensor_id: null, ref: { type: 'audit', id: 1 } }, 3),
    ).toBe('/audit?device_id=3');
  });
  it('groups by local day', () => {
    const d1 = new Date(2026, 10, 8, 10).toISOString();
    const d2 = new Date(2026, 10, 8, 9).toISOString();
    const d3 = new Date(2026, 10, 7, 23).toISOString();
    const g = groupByDay([{ time: d1 }, { time: d2 }, { time: d3 }]);
    expect(g.map((x) => [x.day, x.items.length])).toEqual([
      ['2026-11-08', 2],
      ['2026-11-07', 1],
    ]);
  });
});
