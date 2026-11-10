import { describe, expect, it } from 'vitest';
import type { SshCredentials, FlowSeries, KioskView } from '../api/types3';
import { FEATURE_NAMES } from '../api/types';
import {
  changeStat,
  highlight,
  parseUnifiedDiff,
  searchError,
  sideBySide,
  sshFormErrors,
  sshFormFrom,
  sshFormToInput,
} from './configBackup';
import {
  conversationsAllowed,
  effectiveSampling,
  flowRange,
  formatBits,
  formatBytes,
  formatCount,
  mirroredSeries,
  peakRate,
  samplingError,
  totalDropped,
} from './flows';
import {
  learningProgress,
  macMatches,
  normalizeMacPrefix,
  ouiOf,
  toggleSelection,
} from './newDevices';
import {
  isGuid,
  kerberosEnableState,
  oidcEnableState,
  oidcMissing,
  parseRealms,
  readSkip,
  realmsError,
  safeNext,
  shouldAutoKerberos,
  ssoErrorFromSearch,
  writeSkip,
} from './sso';
import {
  barPct,
  defaultPeriod,
  deliveryLabel,
  formatAvailability,
  moveItem,
  periodError,
  reportFormErrors,
  reportFormFrom,
  reportFormToInput,
  toggleSection,
} from './reports';
import {
  ackTokenFromHash,
  isStale,
  kioskTokenFromHash,
  kioskUrl,
  normalizePositions,
  parseAllowedSources,
  refreshError,
  visibleWidgets,
} from './kiosk';
import { qrInternals, qrMatrix, qrVersionFor } from './qr';
import {
  FEATURE_GROUPS,
  featureHints,
  featuresOfTab,
  featureTab,
  movedFeatures,
  ALL_OFF,
  SETTINGS_TABS,
} from './features';
import {
  flowStatusLines,
  kerberosStatusLines,
  newDeviceStatusLines,
  pollerStatusLines,
} from './features3';
import { availableKinds, timelineHref } from './timeline';
import { formToInput, ruleToForm } from './eventRules';
import { numFieldError, parseOverrides } from '../components/features/Phase3Settings';

const creds = (over: Partial<SshCredentials> = {}): SshCredentials => ({
  username: 'netmon-backup',
  port: 22,
  password: null,
  password_set: true,
  private_key: null,
  private_key_set: false,
  passphrase: null,
  passphrase_set: false,
  enable_password: null,
  enable_password_set: false,
  legacy_algorithms: false,
  credentials_unreadable: false,
  ...over,
});

describe('[3] SSH credentials form', () => {
  it('inherit is null; secrets only when typed, "" clears', () => {
    expect(sshFormToInput(sshFormFrom(null))).toBeNull();
    const f = sshFormFrom(creds());
    expect(sshFormToInput(f)).toEqual({
      username: 'netmon-backup',
      port: 22,
      legacy_algorithms: false,
    });
    f.password = { value: '', clear: true };
    f.enable_password = { value: 'en', clear: false };
    expect(sshFormToInput(f)).toMatchObject({ password: '', enable_password: 'en' });
    expect(sshFormToInput(f)).not.toHaveProperty('private_key');
  });
  it('validates username, port and that a password or key remains', () => {
    const f = sshFormFrom(null);
    f.own = true;
    expect(sshFormErrors(f, null)).toEqual(['ssh.err.username', 'ssh.err.secret']);
    f.username = 'admin';
    f.port = '70000';
    f.private_key = { value: 'not a key', clear: false };
    expect(sshFormErrors(f, null)).toEqual(['ssh.err.port', 'ssh.err.keyFormat']);
    f.port = '22';
    f.private_key = { value: '-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n', clear: false };
    expect(sshFormErrors(f, null)).toEqual([]);
    // clearing the only stored secret is refused
    const g = sshFormFrom(creds());
    g.password = { value: '', clear: true };
    expect(sshFormErrors(g, creds())).toEqual(['ssh.err.secret']);
    expect(sshFormErrors(sshFormFrom(creds()), creds())).toEqual([]);
  });
});

describe('[3] config diff and search', () => {
  const diff = [
    '--- v36',
    '+++ v37',
    '@@ -10,3 +10,4 @@',
    ' vlan 10',
    '-vlan 20',
    '+vlan 21',
    '+vlan 22',
    ' vlan 30',
    '',
  ].join('\n');
  it('numbers unified diff lines', () => {
    const rows = parseUnifiedDiff(diff);
    expect(rows.map((r) => [r.type, r.oldNo, r.newNo])).toEqual([
      ['meta', null, null],
      ['meta', null, null],
      ['hunk', null, null],
      ['context', 10, 10],
      ['del', 11, null],
      ['add', null, 11],
      ['add', null, 12],
      ['context', 12, 13],
    ]);
  });
  it('pairs deletions with additions side by side', () => {
    const pairs = sideBySide(parseUnifiedDiff(diff));
    expect(pairs[0].hunk).toBe('@@ -10,3 +10,4 @@');
    expect(pairs[2].left?.text).toBe('vlan 20');
    expect(pairs[2].right?.text).toBe('vlan 21');
    expect(pairs[3].left).toBeNull();
    expect(pairs[3].right?.text).toBe('vlan 22');
    expect(pairs[4].left?.text).toBe('vlan 30');
  });
  it('highlights matches (plain and regex, case-insensitive)', () => {
    expect(highlight(' VLAN 20 name x', 'vlan 20', false)).toEqual([
      { text: ' ', hit: false },
      { text: 'VLAN 20', hit: true },
      { text: ' name x', hit: false },
    ]);
    expect(highlight('a.b', '.', false).filter((s) => s.hit)).toHaveLength(1);
    expect(highlight('vlan 20', 'vlan \\d+', true)).toEqual([{ text: 'vlan 20', hit: true }]);
    expect(highlight('x', '(', true)).toEqual([{ text: 'x', hit: false }]);
  });
  it('validates the search box', () => {
    expect(searchError('v', false)).toBe('configBackup.searchShort');
    expect(searchError('x'.repeat(201), false)).toBe('configBackup.searchLong');
    expect(searchError('a(', true)).toBe('configBackup.searchRegex');
    expect(searchError('a(', false)).toBeNull();
    expect(changeStat(12, 3)).toBe('+12 −3');
  });
});

describe('[3] flows', () => {
  it('builds minute-aligned ranges and limits conversations to 24 h', () => {
    const now = Date.parse('2026-11-08T10:15:42Z');
    expect(flowRange('1h', now)).toEqual({
      since: '2026-11-08T09:15:00.000Z',
      until: '2026-11-08T10:15:00.000Z',
    });
    expect(conversationsAllowed('24h')).toBe(true);
    expect(conversationsAllowed('7d')).toBe(false);
  });
  it('formats volumes and rates', () => {
    expect(formatBytes(1_500_000_000)).toBe('1.5 GB');
    expect(formatBytes(999)).toBe('999 B');
    expect(formatBits(12_500_000)).toBe('12.5 Mbit/s');
    expect(formatBits(10_000_000_000)).toBe('10 Gbit/s');
    expect(formatCount(12345)).toBe('12.3k');
    expect(formatBytes(null)).toBe('—');
  });
  it('sampling override validation and effective rate', () => {
    expect(samplingError('')).toBeNull();
    expect(samplingError('4096')).toBeNull();
    expect(samplingError('0')).toBe('exporter.samplingInvalid');
    expect(samplingError('1.5')).toBe('exporter.samplingInvalid');
    expect(effectiveSampling({ sampling_override: null, sampling_seen: 512 })).toBe(512);
    expect(effectiveSampling({ sampling_override: 1000, sampling_seen: 512 })).toBe(1000);
    expect(effectiveSampling({ sampling_override: null, sampling_seen: null })).toBe(1);
  });
  it('mirrors outbound traffic and finds peaks', () => {
    const s: FlowSeries = {
      since: '',
      until: '',
      bucket_s: 60,
      source: 'minute',
      interface: null,
      series: [
        {
          key: 'a',
          label: 'https',
          direction: 'in',
          points: [
            ['2026-11-08T10:00:00Z', 100],
            ['2026-11-08T10:01:00Z', 50],
          ],
        },
        {
          key: 'b',
          label: 'dns',
          direction: 'in',
          points: [
            ['2026-11-08T10:00:00Z', 20],
            ['2026-11-08T10:01:00Z', 80],
          ],
        },
        { key: 'c', label: 'https', direction: 'out', points: [['2026-11-08T10:00:00Z', 30]] },
      ],
    };
    const m = mirroredSeries(s);
    expect(m[2].data[0]).toEqual([Date.parse('2026-11-08T10:00:00Z'), -30]);
    expect(m[0].data[0][1]).toBe(100);
    expect(peakRate(s, 'in')).toBe(130);
    expect(peakRate(s, 'out')).toBe(30);
  });
  it('sums drop counters', () => {
    expect(totalDropped(null)).toBe(0);
    expect(
      totalDropped({
        received_datagrams: 0,
        records: 0,
        stored_rows: 0,
        dropped: {
          rate_limited: 1,
          queue_full: 2,
          parse_error: 3,
          no_template: 0,
          unknown_exporter: 4,
          ignored_exporter: 0,
          over_budget: 0,
          unsupported: 0,
          exporter_limit: 0,
          aggregation_overflow: 0,
          db_error: 5,
        },
      }),
    ).toBe(15);
  });
});

describe('[3] new devices', () => {
  it('normalises allow-list prefixes like the server', () => {
    expect(normalizeMacPrefix('3C-A8-2A')).toEqual({ prefix: '3c:a8:2a', bits: 24 });
    expect(normalizeMacPrefix('001b21a')).toEqual({ prefix: '00:1b:21:a', bits: 28 });
    expect(normalizeMacPrefix('00:1b:21:aa:b')).toEqual({ prefix: '00:1b:21:aa:b', bits: 36 });
    expect(normalizeMacPrefix('001b.21aa.0001')).toEqual({ prefix: '00:1b:21:aa:00:01', bits: 48 });
    expect(normalizeMacPrefix('00:1b')).toBeNull();
    expect(normalizeMacPrefix('zz:1b:21')).toBeNull();
    expect(normalizeMacPrefix('00:1b:21:aa')).toBeNull();
  });
  it('OUI, matching and selection', () => {
    expect(ouiOf('00:1B:21:AA:00:01')).toBe('00:1b:21');
    expect(macMatches('3c:a8:2a:00:00:01', '3c:a8:2a')).toBe(true);
    expect(macMatches('3c:a8:2b:00:00:01', '3c:a8:2a')).toBe(false);
    expect(macMatches('00:1b:21:a9:00:01', '00:1b:21:a')).toBe(true);
    expect(toggleSelection(['a'], 'b', true)).toEqual(['a', 'b']);
    expect(toggleSelection(['a', 'b'], 'b', true)).toEqual(['a', 'b']);
    expect(toggleSelection(['a', 'b'], 'a', false)).toEqual(['b']);
  });
  it('learning progress', () => {
    const until = '2026-11-15T00:00:00Z';
    const now = Date.parse('2026-11-11T12:00:00Z');
    expect(learningProgress({ learning: true, learning_until: until }, 7, now)).toBeCloseTo(0.5, 5);
    expect(learningProgress({ learning: false, learning_until: until }, 7, now)).toBe(1);
    expect(
      learningProgress(
        { learning: true, learning_until: until },
        7,
        Date.parse('2026-11-01T00:00:00Z'),
      ),
    ).toBe(0);
  });
});

describe('[3] single sign-on', () => {
  it('configure-first switches unlock only for a validated configuration', () => {
    expect(kerberosEnableState({ on: false, keytabEntries: 0, ldapOn: true })).toBe('keytab');
    expect(kerberosEnableState({ on: false, keytabEntries: 2, ldapOn: false })).toBe('ldap');
    expect(kerberosEnableState({ on: false, keytabEntries: 2, ldapOn: true })).toBe('ok');
    expect(kerberosEnableState({ on: true, keytabEntries: 0, ldapOn: false })).toBe('ok');
    expect(oidcEnableState({ on: false, dirty: false, tested: false })).toBe('test');
    expect(oidcEnableState({ on: false, dirty: true, tested: true })).toBe('save');
    expect(oidcEnableState({ on: false, dirty: false, tested: true })).toBe('ok');
    expect(oidcEnableState({ on: true, dirty: true, tested: false })).toBe('ok');
  });
  it('reads the callback error code', () => {
    expect(ssoErrorFromSearch('?sso_error=sso_conflict')).toBe('sso_conflict');
    expect(ssoErrorFromSearch('?sso_error=whatever')).toBe('sso_failed');
    expect(ssoErrorFromSearch('?x=1')).toBeNull();
  });
  it('auto Kerberos only when ON, auto_login, no logout skip and no error', () => {
    const k = { auto_login: true };
    expect(shouldAutoKerberos({ kerberos: k, skip: false, ssoError: null })).toBe(true);
    expect(shouldAutoKerberos({ kerberos: null, skip: false, ssoError: null })).toBe(false);
    expect(
      shouldAutoKerberos({ kerberos: { auto_login: false }, skip: false, ssoError: null }),
    ).toBe(false);
    expect(shouldAutoKerberos({ kerberos: k, skip: true, ssoError: null })).toBe(false);
    expect(shouldAutoKerberos({ kerberos: k, skip: false, ssoError: 'sso_failed' })).toBe(false);
  });
  it('stores the logout skip flag in sessionStorage', () => {
    const m = new Map<string, string>();
    const st = {
      getItem: (k: string) => m.get(k) ?? null,
      setItem: (k: string, v: string) => void m.set(k, v),
      removeItem: (k: string) => void m.delete(k),
    };
    writeSkip(st, true);
    expect(m.get('netmon.sso.skip')).toBe('1');
    expect(readSkip(st)).toBe(true);
    writeSkip(st, false);
    expect(readSkip(st)).toBe(false);
    expect(readSkip(null)).toBe(false);
  });
  it('validates Kerberos realms and OIDC settings', () => {
    expect(parseRealms('mnidom.net, MNIDOM.NET ;lab.local')).toEqual(['MNIDOM.NET', 'LAB.LOCAL']);
    expect(realmsError([])).toBe('kerberos.realmsRequired');
    expect(realmsError(['A B'])).toBe('kerberos.realmsInvalid');
    expect(realmsError(Array.from({ length: 9 }, (_, i) => `R${i}`))).toBe(
      'kerberos.realmsTooMany',
    );
    expect(isGuid('00000000-0000-0000-0000-000000000000')).toBe(true);
    expect(isGuid('nope')).toBe(false);
    const ok = {
      tenant_id: '00000000-0000-0000-0000-000000000000',
      client_id: '11111111-1111-1111-1111-111111111111',
      secretSet: true,
      admin_values: ['NetMon.Admin'],
      viewer_values: [],
      role_source: 'app_roles' as const,
    };
    expect(oidcMissing(ok)).toEqual([]);
    expect(oidcMissing({ ...ok, role_source: 'groups' })).toEqual(['oidc.err.groupIds']);
    expect(oidcMissing({ ...ok, tenant_id: 'x', secretSet: false, admin_values: [] })).toEqual([
      'oidc.err.tenant',
      'oidc.err.secret',
      'oidc.err.values',
    ]);
  });
  it('keeps the OIDC next relative', () => {
    expect(safeNext('/devices/3')).toBe('/devices/3');
    expect(safeNext('//evil.example')).toBe('/');
    expect(safeNext('https://x')).toBe('/');
    expect(safeNext('/login')).toBe('/');
    expect(safeNext(undefined)).toBe('/');
  });
});

describe('[3] reports', () => {
  it('validates and converts the editor form', () => {
    const f = reportFormFrom(null, 'he');
    expect(reportFormErrors({ ...f, name: '' })).toEqual(['reports.err.name']);
    expect(reportFormErrors({ ...f, name: 'Weekly', sections: [] })).toEqual([
      'reports.err.sections',
    ]);
    expect(reportFormErrors({ ...f, name: 'W', send_time: '25:00' })).toEqual(['reports.err.time']);
    expect(reportFormErrors({ ...f, name: 'W', email_to: 'ops@x.net bad' })).toEqual([
      'reports.err.email',
    ]);
    expect(reportFormErrors({ ...f, name: 'W', schedule: 'monthly', month_day: 31 })).toEqual([
      'reports.err.monthDay',
    ]);
    const weekly = reportFormToInput({ ...f, name: ' W ', email_to: 'a@b.cc\na@b.cc' });
    expect(weekly).toMatchObject({
      name: 'W',
      schedule: 'weekly',
      weekday: 1,
      month_day: null,
      send_time: '07:47',
      email_to: ['a@b.cc'],
      language: 'he',
    });
    const none = reportFormToInput({ ...f, name: 'W', schedule: 'none' });
    expect(none).toMatchObject({ weekday: null, month_day: null, send_time: null });
  });
  it('orders sections', () => {
    expect(moveItem(['a', 'b', 'c'], 2, -1)).toEqual(['a', 'c', 'b']);
    expect(moveItem(['a', 'b'], 0, -1)).toEqual(['a', 'b']);
    expect(toggleSection(['availability'], 'top_talkers', true)).toEqual([
      'availability',
      'top_talkers',
    ]);
    expect(toggleSection(['availability', 'top_talkers'], 'availability', false)).toEqual([
      'top_talkers',
    ]);
  });
  it('default periods (weekly Mon–Mon, monthly the previous month)', () => {
    expect(defaultPeriod('weekly', new Date(2026, 10, 8, 12))).toEqual({
      start: '2026-10-26',
      end: '2026-11-02',
    });
    expect(defaultPeriod('weekly', new Date(2026, 10, 9, 8))).toEqual({
      start: '2026-11-02',
      end: '2026-11-09',
    });
    expect(defaultPeriod('monthly', new Date(2026, 3, 1, 8))).toEqual({
      start: '2026-03-01',
      end: '2026-04-01',
    });
    expect(defaultPeriod('none', new Date(2026, 10, 8, 12))).toEqual({
      start: '2026-11-01',
      end: '2026-11-08',
    });
  });
  it('validates the run range (≤ 92 days)', () => {
    expect(periodError('2026-11-02', '2026-11-09')).toBeNull();
    expect(periodError('2026-11-09', '2026-11-02')).toBe('reports.err.periodOrder');
    expect(periodError('2026-01-01', '2026-06-01')).toBe('reports.err.periodLong');
    expect(periodError('', '2026-06-01')).toBe('reports.err.period');
  });
  it('formats availability and deliveries', () => {
    expect(formatAvailability(98.8095)).toBe('98.81%');
    expect(formatAvailability(null)).toBeNull();
    expect(barPct(50, 200)).toBe(25);
    expect(barPct(5, 0)).toBe(0);
    expect(deliveryLabel('email:ops@mnidom.net', () => undefined)).toBe('ops@mnidom.net');
    expect(deliveryLabel('channel:4', (id) => (id === 4 ? 'NOC Teams' : undefined))).toBe(
      'NOC Teams',
    );
    expect(deliveryLabel('channel:5', () => undefined)).toBe('#5');
  });
});

describe('[3] kiosk and ack links', () => {
  const tok = `nmk_${'A1b2_-'.repeat(7)}x`;
  it('reads tokens from the fragment', () => {
    expect(kioskTokenFromHash(`#${tok}`)).toBe(tok);
    expect(kioskTokenFromHash('#nmk_short')).toBeNull();
    expect(kioskTokenFromHash('#a1.xyz')).toBeNull();
    const ack = `a1.${'A'.repeat(38)}`;
    expect(ack).toHaveLength(41);
    expect(ackTokenFromHash(`#${ack}`)).toBe(ack);
    expect(ackTokenFromHash(`#a2.${'A'.repeat(38)}`)).toBeNull();
    expect(ackTokenFromHash(`#a1.${'A'.repeat(37)}`)).toBeNull();
  });
  it('builds the URL, validates sources and refresh, detects stale data', () => {
    expect(kioskUrl({ token: tok, url: null }, 'https://nm.local/')).toBe(
      `https://nm.local/noc#${tok}`,
    );
    expect(kioskUrl({ token: tok, url: 'https://x/noc#t' }, 'https://nm.local')).toBe(
      'https://x/noc#t',
    );
    expect(parseAllowedSources('').error).toBe('kiosk.err.sourcesRequired');
    expect(parseAllowedSources('10.1.2.0/24\nbad').error).toBe('kiosk.err.sourcesInvalid');
    expect(parseAllowedSources('10.1.2.0/24, 10.0.0.5').list).toEqual(['10.1.2.0/24', '10.0.0.5']);
    expect(refreshError('9')).toBe('kiosk.err.refresh');
    expect(refreshError('30')).toBeNull();
    expect(isStale(1000, 30, 1000 + 90_001)).toBe(true);
    expect(isStale(1000, 30, 1000 + 89_000)).toBe(false);
    expect(isStale(null, 30, 1e12)).toBe(false);
  });
  it('shows configured widgets in order and lays out the map', () => {
    const v = {
      screen: {
        name: 'NOC',
        language: 'en',
        widgets: ['problems', 'summary', 'map'],
        refresh_s: 30,
      },
      summary: null,
      problems: [],
      map: { nodes: [], links: [] },
    } as unknown as KioskView;
    expect(visibleWidgets(v)).toEqual(['problems', 'map']);
    const pos = normalizePositions([
      { id: 1, x: 0, y: 0 },
      { id: 2, x: 100, y: 50 },
      { id: 3, x: null, y: null },
    ]);
    expect(pos.get(1)).toEqual({ x: 0, y: 0 });
    expect(pos.get(2)).toEqual({ x: 1, y: 1 });
    expect(pos.get(3)).toEqual({ x: 0.5, y: 0.5 });
  });
});

describe('[3] QR code (byte mode, level M)', () => {
  it('Reed–Solomon matches the published "HELLO WORLD" 1-M vector', () => {
    const data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17];
    expect(qrInternals.rsRemainder(data, 10)).toEqual([
      196, 35, 39, 119, 235, 215, 231, 226, 93, 23,
    ]);
  });
  it('format and version information', () => {
    const want = [
      '101010000010010',
      '101000100100101',
      '101111001111100',
      '101101101001011',
      '100010111111001',
      '100000011001110',
      '100111110010111',
      '100101010100000',
    ];
    for (let m = 0; m < 8; m++)
      expect(qrInternals.formatBits(m).toString(2).padStart(15, '0'), `mask ${m}`).toBe(want[m]);
    expect(qrInternals.versionBits(7)).toBe(0x07c94);
  });
  it('lays out a kiosk URL with finder, timing, dark module and both format copies', () => {
    const url = `https://netmonitor.mnidom.net/noc#nmk_${'x'.repeat(43)}`;
    expect(qrVersionFor(url.length)).toBe(5);
    expect(qrVersionFor(500)).toBeNull();
    for (let mask = 0; mask < 8; mask++) {
      const m = qrMatrix(url, mask)!;
      const n = m.length;
      expect(n).toBe(37);
      // finder pattern rows
      expect(m[0].slice(0, 7).every(Boolean)).toBe(true);
      expect(m[1].slice(0, 7).map(Number).join('')).toBe('1000001');
      expect(m[3].slice(0, 7).map(Number).join('')).toBe('1011101');
      expect(m[7].slice(0, 8).some(Boolean)).toBe(false); // separator
      // timing pattern
      for (let i = 8; i < n - 8; i++) expect(m[6][i]).toBe(i % 2 === 0);
      expect(m[n - 8][8]).toBe(true);
      // format bits, first copy (col 8 rows 0–5,7,8; row 8 col 7; row 8 cols 5..0) and second copy
      const fb = qrInternals.formatBits(mask);
      const bit = (i: number) => ((fb >>> i) & 1) === 1;
      const first = [0, 1, 2, 3, 4, 5].map((i) => m[i][8]);
      expect(first).toEqual([0, 1, 2, 3, 4, 5].map(bit));
      expect([m[7][8], m[8][8], m[8][7]]).toEqual([bit(6), bit(7), bit(8)]);
      for (let i = 9; i < 15; i++) expect(m[8][14 - i]).toBe(bit(i));
      for (let i = 0; i < 8; i++) expect(m[8][n - 1 - i]).toBe(bit(i));
      for (let i = 8; i < 15; i++) expect(m[n - 15 + i][8]).toBe(bit(i));
    }
    // automatic mask choice returns one of them
    expect(qrMatrix(url)).toHaveLength(37);
  });
});

describe('[3] Features page', () => {
  it('switches every phase-3 feature in its own tab; the overview lists them', () => {
    expect(featuresOfTab('sso')).toEqual(['sso_kerberos', 'sso_oidc']);
    expect(featuresOfTab('backup')).toEqual(['config_backup']);
    expect(featuresOfTab('flows')).toEqual(['flows']);
    expect(featuresOfTab('newDevices')).toEqual(['new_device_alerts']);
    expect(featuresOfTab('reports')).toEqual(['reports']);
    expect(featuresOfTab('noc')).toEqual(['noc']);
    expect(featuresOfTab('chatAck')).toEqual(['chat_ack']);
    expect(featuresOfTab('features')).toEqual(['lag', 'timeline']);
    expect(movedFeatures()).toHaveLength(15);
    const all = SETTINGS_TABS.flatMap((t) => featuresOfTab(t));
    expect([...all].sort()).toEqual([...FEATURE_NAMES].sort());
    expect(featureTab('chat_ack')).toBe('chatAck');
  });
  it('groups every feature exactly once under 5 headings', () => {
    const all = FEATURE_GROUPS.flatMap((g) => g.names);
    expect(FEATURE_GROUPS).toHaveLength(5);
    expect([...all].sort()).toEqual([...FEATURE_NAMES].sort());
    expect(new Set(all).size).toBe(17);
  });
  it('dependency hints', () => {
    expect(featureHints('sso_kerberos', ALL_OFF).map((h) => h.key)).toEqual([
      'features.hint.kerberosLdap',
      'features.hint.keytab',
    ]);
    expect(featureHints('sso_kerberos', { ...ALL_OFF, ldap: true }).map((h) => h.key)).toEqual([
      'features.hint.keytab',
    ]);
    expect(featureHints('config_backup', { ...ALL_OFF, syslog: true }).map((h) => h.key)).toEqual([
      'features.hint.sshCredentials',
    ]);
  });
  it('status lines', () => {
    const rx = (
      kind: 'netflow' | 'ipfix' | 'sflow',
      port: number,
      state: 'listening' | 'error' | 'disabled',
    ) => ({
      kind,
      port,
      enabled: true,
      state,
      error: state === 'error' ? 'address in use' : null,
      since: null,
    });
    const st = {
      receivers: [
        rx('netflow', 2055, 'listening'),
        rx('ipfix', 4739, 'listening'),
        rx('sflow', 6343, 'error'),
      ],
    } as unknown as Parameters<typeof flowStatusLines>[0];
    expect(flowStatusLines(st)).toEqual([
      {
        key: 'features.status.listening',
        params: { proto: 'UDP', port: '2055, 4739' },
        tone: 'ok',
      },
      {
        key: 'features.status.error',
        params: { proto: 'UDP', port: 6343, error: 'address in use' },
        tone: 'error',
      },
    ]);
    expect(
      newDeviceStatusLines(
        { learning: true, learning_until: '2026-11-15T00:00:00Z' } as never,
        () => '15.11.2026',
      ),
    ).toEqual([
      { key: 'features.status.learningUntil', params: { date: '15.11.2026' }, tone: 'muted' },
    ]);
    expect(
      kerberosStatusLines({
        keytab_set: true,
        keytab_entries: [{}, {}],
        state: 'ready',
        platform_supported: true,
        error: null,
      } as never)[0],
    ).toEqual({ key: 'features.status.keytab', params: { n: 2 }, tone: 'ok' });
    expect(pollerStatusLines([])).toEqual([{ key: 'features.status.noPoller', tone: 'muted' }]);
    expect(
      pollerStatusLines([
        {
          bot_username: 'NetMonOpsBot',
          channel_ids: [4],
          state: 'polling',
          last_error: null,
          updated_at: '',
        },
      ]),
    ).toEqual([{ key: 'features.status.pollerOk', params: { bot: 'NetMonOpsBot' }, tone: 'ok' }]);
  });
});

describe('[3] timeline, rules and settings helpers', () => {
  it('offers config/new_mac kinds when their features are on; the diff link points to the tab', () => {
    const base = { traps: false, syslog: false, audit: false, isAdmin: true };
    expect(availableKinds(base)).not.toContain('config');
    expect(availableKinds({ ...base, config_backup: true, new_device_alerts: true })).toEqual(
      expect.arrayContaining(['config', 'new_mac']),
    );
    const it0 = { kind: 'config', sensor_id: null } as never;
    expect(timelineHref(it0, 3)).toBe('/devices/3?tab=config');
  });
  it('config_backup rule actions carry no key and no clear fields', () => {
    const f = ruleToForm(null);
    const input = formToInput({
      ...f,
      name: 'Cisco config',
      kind: 'syslog',
      action: 'config_backup',
      keyRegex: '(x)',
      hasClearMatch: true,
      clearAfterMin: '10',
    });
    expect(input).toMatchObject({
      action: 'config_backup',
      key_regex: null,
      clear_match: null,
      clear_after_min: null,
    });
  });
  it('flow application overrides and numeric settings', () => {
    expect(parseOverrides('6 8443 admin-portal\n\n17 5353 mdns')).toEqual({
      list: [
        { protocol: 6, port: 8443, name: 'admin-portal' },
        { protocol: 17, port: 5353, name: 'mdns' },
      ],
      error: false,
    });
    expect(parseOverrides('300 80 x').error).toBe(true);
    expect(
      numFieldError({ key: 'newdev_forget_after_days', min: 30, max: 3650, zeroOk: true }, '0'),
    ).toBe(false);
    expect(
      numFieldError({ key: 'newdev_forget_after_days', min: 30, max: 3650, zeroOk: true }, '10'),
    ).toBe(true);
    expect(numFieldError({ key: 'config_backup_concurrency', min: 1, max: 16 }, '4')).toBe(false);
    expect(numFieldError({ key: 'config_backup_concurrency', min: 1, max: 16 }, 'x')).toBe(true);
  });
});
