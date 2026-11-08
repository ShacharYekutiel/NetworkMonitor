import { describe, expect, it } from 'vitest';
import { mkPort } from './portFixtures';
import {
  gapAfter,
  isUplinkPrefix,
  layoutFaceplate,
  parsePortName,
  type FaceplateBlock,
  type NameRule,
} from './faceplate';

const TEN_G = 10_000_000_000;
/** Labels of a row (null = spacer). */
const row = (cells: FaceplateBlock['top']) => cells.map((c) => (c ? c.label : null));
const idx = (cells: FaceplateBlock['top']) => cells.map((c) => (c ? c.if_index : null));

describe('parsePortName (architecture.md § 11.4 test vectors)', () => {
  const vectors: [string, NameRule, number, number, number, boolean][] = [
    ['1', 'R6', 1, 0, 1, false],
    ['52', 'R6', 1, 0, 52, false],
    ['A1', 'R5', 1, 1, 1, false],
    ['B24', 'R5', 1, 2, 24, false],
    ['1/1/1', 'R1', 1, 1, 1, false],
    ['2/1/48', 'R1', 2, 1, 48, false],
    ['GigabitEthernet1/0/1', 'R1', 1, 0, 1, false],
    ['Ten-GigabitEthernet1/0/49', 'R1', 1, 0, 49, true],
    ['XGigabitEthernet1/0/25', 'R1', 1, 0, 25, true],
    ['Gi1/0/1', 'R1', 1, 0, 1, false],
    ['Te1/1/1', 'R1', 1, 1, 1, true],
    ['GigabitEthernet0/1', 'R4', 1, 0, 1, false],
    ['Fa0/24', 'R4', 1, 0, 24, false],
    ['2/14', 'R3', 2, 0, 14, false],
    ['1/A1', 'R2', 1, 1, 1, false],
    ['Port 12', 'R7', 1, 0, 12, false],
    // more rule coverage
    ['ethernet5', 'R7', 1, 0, 5, false],
    ['lan1', 'R7', 1, 0, 1, false],
    ['TenGigabitEthernet1/1/4', 'R1', 1, 1, 4, true],
    ['FortyGigE1/0/53', 'R1', 1, 0, 53, true],
    ['xe-0/0/3', 'R1', 0, 0, 3, true],
    ['3/C7', 'R2', 3, 3, 7, false],
    ['  12  ', 'R6', 1, 0, 12, false],
  ];
  it.each(vectors)('%s → %s %i/%i/%i hint %s', (name, rule, member, module, port, hint) => {
    const r = parsePortName(name);
    expect(r).not.toBeNull();
    expect(r).toMatchObject({ rule, member, module, port, uplinkHint: hint });
  });

  it('labels: p as text, letter + p for R2/R5', () => {
    expect(parsePortName('A1')?.label).toBe('A1');
    expect(parsePortName('1/b3')?.label).toBe('B3');
    expect(parsePortName('GigabitEthernet1/0/07')?.label).toBe('7');
  });

  it('does not match unknown names', () => {
    for (const n of ['p-a', 'Trk1x', 'Vlan-interface', '', 'abc', '1/2/3/4', 'mgmt-x'])
      expect(parsePortName(n), n).toBeNull();
  });

  it('uplink prefix hints', () => {
    for (const p of [
      'Te',
      'te',
      'Fo',
      'Hu',
      'Twe',
      'Tf',
      'xe',
      'XGE',
      'Ten-GigabitEthernet',
      'TenGigabitEthernet',
      'XGigabitEthernet',
      'TwentyFiveGigE',
      'FortyGigE',
      'HundredGigE',
      '25GE',
      '40GE',
      '100GE',
      'ten_gig',
    ])
      expect(isUplinkPrefix(p), p).toBe(true);
    for (const p of [
      '',
      'Gi',
      'GigabitEthernet',
      'Fa',
      'FastEthernet',
      'Port',
      'eth',
      'lan',
      'M-GigabitEthernet',
    ])
      expect(isUplinkPrefix(p), p).toBe(false);
  });
});

describe('layoutFaceplate', () => {
  it('ports 1–10 with 9 and 10 at 10G (contract vector)', () => {
    const ports = Array.from({ length: 10 }, (_, i) =>
      mkPort(i + 1, String(i + 1), i >= 8 ? { speed_bps: TEN_G } : {}),
    );
    const fp = layoutFaceplate(ports);
    expect(fp.mode).toBe('parsed');
    expect(fp.members).toHaveLength(1);
    const [m] = fp.members;
    expect(m.label).toBeNull();
    expect(m.blocks.map((b) => b.key)).toEqual(['m1-0', 'm1-uplink']);
    expect(row(m.blocks[0].top)).toEqual(['1', '3', '5', '7']);
    expect(row(m.blocks[0].bottom)).toEqual(['2', '4', '6', '8']);
    expect(m.blocks[1]).toMatchObject({ uplink: true, module: null, gapEvery: 6 });
    expect(row(m.blocks[1].top)).toEqual(['9']);
    expect(row(m.blocks[1].bottom)).toEqual(['10']);
  });

  it('Comware with two members (contract vector)', () => {
    const ports = [
      ...[1, 2, 3, 4].map((p) => mkPort(p, `GigabitEthernet1/0/${p}`)),
      mkPort(49, 'Ten-GigabitEthernet1/0/49', { speed_bps: null }),
      mkPort(50, 'Ten-GigabitEthernet1/0/50', { speed_bps: null }),
      mkPort(101, 'GigabitEthernet2/0/1'),
      mkPort(102, 'GigabitEthernet2/0/2'),
      mkPort(200, 'Vlan-interface1', { kind: 'vlan' }),
      mkPort(400, 'M-GigabitEthernet0/0/0', { kind: 'mgmt' }),
    ];
    const fp = layoutFaceplate(ports);
    expect(fp.mode).toBe('parsed');
    expect(fp.members.map((m) => [m.member, m.label])).toEqual([
      [1, 'Member 1'],
      [2, 'Member 2'],
    ]);
    const [m1, m2] = fp.members;
    expect(m1.blocks.map((b) => b.key)).toEqual(['m1-0', 'm1-uplink']);
    expect(row(m1.blocks[0].top)).toEqual(['1', '3']);
    expect(row(m1.blocks[0].bottom)).toEqual(['2', '4']);
    expect(idx(m1.blocks[1].top)).toEqual([49]);
    expect(idx(m1.blocks[1].bottom)).toEqual([50]);
    expect(m2.blocks.map((b) => b.key)).toEqual(['m2-0']);
    expect(idx(m2.blocks[0].top)).toEqual([101]);
    expect(idx(m2.blocks[0].bottom)).toEqual([102]);
  });

  it('HPE 2930F-48G-4SFP+: 48 copper + 4 SFP+ uplinks', () => {
    const ports = Array.from({ length: 52 }, (_, i) =>
      mkPort(i + 1, String(i + 1), i >= 48 ? { speed_bps: TEN_G } : {}),
    );
    const [m] = layoutFaceplate(ports).members;
    expect(m.blocks[0].top).toHaveLength(24);
    expect(row(m.blocks[0].top).slice(0, 3)).toEqual(['1', '3', '5']);
    expect(row(m.blocks[0].bottom).slice(-1)).toEqual(['48']);
    expect(row(m.blocks[1].top)).toEqual(['49', '51']);
    expect(row(m.blocks[1].bottom)).toEqual(['50', '52']);
    // gaps after every 6 columns, never after the last one
    const gaps = m.blocks[0].top.map((_, i) => gapAfter(m.blocks[0], i));
    expect(gaps.filter(Boolean)).toHaveLength(3);
    expect(gaps[5]).toBe(true);
    expect(gaps[23]).toBe(false);
  });

  it('an SFP+ port without link that reports speed 0 stays in the main block', () => {
    const ports = [mkPort(1, '1'), mkPort(2, '2'), mkPort(3, '3', { speed_bps: 0 })];
    const [m] = layoutFaceplate(ports).members;
    expect(m.blocks.map((b) => b.key)).toEqual(['m1-0']);
  });

  it('Cisco: modules other than the main module form the uplink group', () => {
    const ports = [
      ...[1, 2, 3, 4].map((p) => mkPort(p, `Gi1/0/${p}`)),
      mkPort(10, 'Gi1/1/1'),
      mkPort(11, 'Gi1/1/2'),
    ];
    const [m] = layoutFaceplate(ports).members;
    expect(m.blocks.map((b) => b.key)).toEqual(['m1-0', 'm1-uplink']);
    expect(idx(m.blocks[1].top)).toEqual([10]);
    expect(idx(m.blocks[1].bottom)).toEqual([11]);
  });

  it('main module ties go to the lowest module', () => {
    const ports = [mkPort(1, 'Gi0/1'), mkPort(2, 'Gi0/2'), mkPort(3, 'Gi1/1'), mkPort(4, 'Gi1/2')];
    const [m] = layoutFaceplate(ports).members;
    expect(m.blocks.map((b) => b.key)).toEqual(['m1-0', 'm1-uplink']);
    expect(idx(m.blocks[1].top)).toEqual([3]);
  });

  it('R5 chassis modules: one main block per letter, ascending', () => {
    const ports = [
      mkPort(30, 'B1'),
      mkPort(31, 'B2'),
      mkPort(1, 'A1'),
      mkPort(2, 'A2'),
      mkPort(3, 'A3'),
    ];
    const [m] = layoutFaceplate(ports).members;
    expect(m.blocks.map((b) => [b.key, b.module])).toEqual([
      ['m1-1', 1],
      ['m1-2', 2],
    ]);
    expect(row(m.blocks[0].top)).toEqual(['A1', 'A3']);
    expect(row(m.blocks[0].bottom)).toEqual(['A2', null]);
  });

  it('ArubaOS stack (R3): members sorted, odd on top, even below', () => {
    const ports = [
      ...[1, 2, 3, 4].map((p) => mkPort(100 + p, `2/${p}`)),
      ...[1, 2, 3, 4].map((p) => mkPort(p, `1/${p}`)),
    ];
    const fp = layoutFaceplate(ports);
    expect(fp.members.map((m) => m.member)).toEqual([1, 2]);
    expect(idx(fp.members[1].blocks[0].top)).toEqual([101, 103]);
    expect(idx(fp.members[1].blocks[0].bottom)).toEqual([102, 104]);
  });

  it('a block starting at an even port leaves the first top cell empty', () => {
    const ports = [mkPort(2, '2'), mkPort(3, '3'), mkPort(4, '4')];
    const [b] = layoutFaceplate(ports).members[0].blocks;
    expect(row(b.top)).toEqual([null, '3']);
    expect(row(b.bottom)).toEqual(['2', '4']);
  });

  it('gaps in the numbering become empty cells', () => {
    const ports = [mkPort(1, '1'), mkPort(2, '2'), mkPort(7, '7')];
    const [b] = layoutFaceplate(ports).members[0].blocks;
    expect(row(b.top)).toEqual(['1', null, null, '7']);
    expect(row(b.bottom)).toEqual(['2', null, null, null]);
  });

  it('only physical ports are laid out', () => {
    const fp = layoutFaceplate([
      mkPort(1, '1'),
      mkPort(11, 'Trk1', { kind: 'lag' }),
      mkPort(12, 'DEFAULT_VLAN', { kind: 'vlan' }),
      mkPort(13, 'mgmt', { kind: 'mgmt' }),
    ]);
    expect(fp.mode).toBe('parsed');
    expect(idx(fp.members[0].blocks[0].top)).toEqual([1]);
    expect(layoutFaceplate([mkPort(11, 'Trk1', { kind: 'lag' })])).toEqual({
      mode: 'parsed',
      members: [],
    });
  });

  it('10 ports named p-a … p-j → fallback (contract vector)', () => {
    const ports = 'abcdefghij'.split('').map((c, i) => mkPort(20 - i, `p-${c}`));
    const fp = layoutFaceplate(ports);
    expect(fp.mode).toBe('fallback');
    expect(fp.members).toHaveLength(1);
    expect(fp.members[0].label).toBeNull();
    const [b] = fp.members[0].blocks;
    expect(fp.members[0].blocks).toHaveLength(1);
    expect(b.uplink).toBe(false);
    // sorted by if_index; i even → top, odd → bottom, column floor(i/2)
    expect(idx(b.top)).toEqual([11, 13, 15, 17, 19]);
    expect(idx(b.bottom)).toEqual([12, 14, 16, 18, 20]);
    expect(row(b.top)).toEqual(['1', '3', '5', '7', '9']);
  });

  it('fallback has no uplink split', () => {
    const ports = [mkPort(1, 'x'), mkPort(2, 'y'), mkPort(3, 'z', { speed_bps: TEN_G })];
    const fp = layoutFaceplate(ports);
    expect(fp.mode).toBe('fallback');
    expect(fp.members[0].blocks.map((b) => b.uplink)).toEqual([false]);
  });

  it('duplicate (member, module, port) → fallback', () => {
    const ports = [mkPort(1, '1'), mkPort(2, '2'), mkPort(3, 'Port 1')];
    expect(layoutFaceplate(ports).mode).toBe('fallback');
    // the same position from two rules (R1 and R3 give 1/0/x vs 1/0/x)
    expect(layoutFaceplate([mkPort(1, '1/1'), mkPort(2, 'Gi1/0/1')]).mode).toBe('fallback');
  });

  it('20 % threshold: exactly 20 % unparsed stays parsed with an "other" block', () => {
    const ports = [
      ...Array.from({ length: 8 }, (_, i) => mkPort(i + 1, String(i + 1))),
      mkPort(40, 'weird-b'),
      mkPort(30, 'weird-a'),
    ];
    const fp = layoutFaceplate(ports);
    expect(fp.mode).toBe('parsed');
    const keys = fp.members[0].blocks.map((b) => b.key);
    expect(keys).toEqual(['m1-0', 'other']);
    const other = fp.members[0].blocks[1];
    expect(idx(other.top)).toEqual([30]);
    expect(idx(other.bottom)).toEqual([40]);
  });

  it('20 % threshold: more than 20 % unparsed → fallback', () => {
    const ports = [
      ...Array.from({ length: 7 }, (_, i) => mkPort(i + 1, String(i + 1))),
      mkPort(30, 'weird-a'),
      mkPort(31, 'weird-b'),
      mkPort(32, 'weird-c'),
    ];
    expect(layoutFaceplate(ports).mode).toBe('fallback');
  });

  it('"other" goes to the first member, before its uplink block', () => {
    const ports = [
      ...[1, 2, 3, 4, 5, 6].map((p) => mkPort(p, `1/${p}`)),
      mkPort(7, '1/7', { speed_bps: TEN_G }),
      ...[1, 2].map((p) => mkPort(100 + p, `2/${p}`)),
      mkPort(200, 'odd-one'),
    ];
    const fp = layoutFaceplate(ports);
    expect(fp.members[0].blocks.map((b) => b.key)).toEqual(['m1-0', 'other', 'm1-uplink']);
    expect(fp.members[1].blocks.map((b) => b.key)).toEqual(['m2-0']);
  });

  it('is deterministic regardless of input order', () => {
    const ports = Array.from({ length: 12 }, (_, i) =>
      mkPort(i + 1, `GigabitEthernet1/0/${i + 1}`),
    );
    expect(layoutFaceplate([...ports].reverse())).toEqual(layoutFaceplate(ports));
  });

  it('parses ifDescr when ifName is empty', () => {
    const fp = layoutFaceplate([mkPort(1, '', { descr: '1' }), mkPort(2, '', { descr: '2' })]);
    expect(fp.mode).toBe('parsed');
    expect(idx(fp.members[0].blocks[0].top)).toEqual([1]);
  });
});
