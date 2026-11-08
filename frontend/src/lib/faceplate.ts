// [2b-2] Faceplate layout (architecture.md § 11.4). Pure and deterministic.
import type { Port } from '../api/types';

export type FaceplateCell = { if_index: number; label: string } | null;
export type FaceplateBlock = {
  key: string;
  module: number | null;
  uplink: boolean;
  /** equal length = number of columns */
  top: FaceplateCell[];
  bottom: FaceplateCell[];
  /** visual gap after every 6 columns */
  gapEvery: 6;
};
export type FaceplateMember = {
  member: number;
  /** "Member 2"; null when one member (the UI translates it from `member`) */
  label: string | null;
  /** main blocks by module asc, then "other", then uplink */
  blocks: FaceplateBlock[];
};
export type Faceplate = { mode: 'parsed' | 'fallback'; members: FaceplateMember[] };

export type NameRule = 'R1' | 'R2' | 'R3' | 'R4' | 'R5' | 'R6' | 'R7';
export type ParsedName = {
  rule: NameRule;
  member: number;
  module: number;
  port: number;
  /** `p` as text; R2/R5: letter + `p` (e.g. "A1") */
  label: string;
  /** the name's prefix (may be empty) */
  prefix: string;
  /** prefix hint of the uplink group (rule 1 of § 11.4) */
  uplinkHint: boolean;
};

const PREFIX = '[a-z][a-z\\- _]*?';
const R1 = new RegExp(`^(${PREFIX})?\\s*(\\d+)/(\\d+)/(\\d+)$`, 'i');
const R2 = /^(\d+)\/([a-z])(\d+)$/i;
const R3 = /^(\d+)\/(\d+)$/;
const R4 = new RegExp(`^(${PREFIX})\\s*(\\d+)/(\\d+)$`, 'i');
const R5 = /^([a-z])(\d+)$/i;
const R6 = /^(\d+)$/;
const R7 = new RegExp(`^(${PREFIX})\\s*(\\d+)$`, 'i');

const HINT_EXACT = ['te', 'fo', 'hu', 'twe', 'tf', 'xe', 'xge'];
const HINT_START = [
  'ten',
  'xgigabit',
  'twentyfive',
  'forty',
  'hundred',
  '25g',
  '40g',
  '50g',
  '100g',
];

/** Uplink prefix hint: `Ten-GigabitEthernet`, `XGigabitEthernet`, `FortyGigE`, `Te`, … */
export function isUplinkPrefix(prefix: string): boolean {
  const p = prefix.toLowerCase().replace(/[- _]/g, '');
  if (!p) return false;
  return HINT_EXACT.includes(p) || HINT_START.some((s) => p.startsWith(s));
}

function letterIndex(c: string): number {
  return c.toUpperCase().charCodeAt(0) - 64; // A = 1
}

/** Applies the name rules R1–R7 (first match). Null when no rule matches. */
export function parsePortName(raw: string): ParsedName | null {
  const name = raw.trim();
  const mk = (
    rule: NameRule,
    member: number,
    module: number,
    port: number,
    label: string,
    prefix = '',
  ): ParsedName => {
    const pre = prefix.trim();
    return { rule, member, module, port, label, prefix: pre, uplinkHint: isUplinkPrefix(pre) };
  };
  let m: RegExpMatchArray | null;
  if ((m = name.match(R1))) return mk('R1', +m[2], +m[3], +m[4], String(+m[4]), m[1] ?? '');
  if ((m = name.match(R2)))
    return mk('R2', +m[1], letterIndex(m[2]), +m[3], `${m[2].toUpperCase()}${+m[3]}`);
  if ((m = name.match(R3))) return mk('R3', +m[1], 0, +m[2], String(+m[2]));
  if ((m = name.match(R4))) return mk('R4', 1, +m[2], +m[3], String(+m[3]), m[1]);
  if ((m = name.match(R5)))
    return mk('R5', 1, letterIndex(m[1]), +m[2], `${m[1].toUpperCase()}${+m[2]}`);
  if ((m = name.match(R6))) return mk('R6', 1, 0, +m[1], String(+m[1]));
  if ((m = name.match(R7))) return mk('R7', 1, 0, +m[2], String(+m[2]), m[1]);
  return null;
}

/** The text a port's position is parsed from: ifName, else ifDescr. */
export function portLabelSource(p: Pick<Port, 'name' | 'descr'>): string {
  return (p.name || p.descr || '').trim();
}

const TEN_G = 10_000_000_000;

type Item = { port: Port; parsed: ParsedName };

function positional(key: string, ports: Port[]): FaceplateBlock {
  const sorted = [...ports].sort((a, b) => a.if_index - b.if_index);
  const cols = Math.ceil(sorted.length / 2);
  const top: FaceplateCell[] = Array(cols).fill(null);
  const bottom: FaceplateCell[] = Array(cols).fill(null);
  sorted.forEach((p, i) => {
    const cell = { if_index: p.if_index, label: String(i + 1) };
    if (i % 2 === 0) top[Math.floor(i / 2)] = cell;
    else bottom[Math.floor(i / 2)] = cell;
  });
  return { key, module: null, uplink: false, top, bottom, gapEvery: 6 };
}

function oddEvenBlock(
  key: string,
  module: number | null,
  uplink: boolean,
  items: Item[],
): FaceplateBlock {
  const pmin = Math.min(...items.map((x) => x.parsed.port));
  const base = Math.ceil(pmin / 2);
  const col = (p: number) => Math.ceil(p / 2) - base;
  const cols = Math.max(...items.map((x) => col(x.parsed.port))) + 1;
  const top: FaceplateCell[] = Array(cols).fill(null);
  const bottom: FaceplateCell[] = Array(cols).fill(null);
  for (const x of [...items].sort((a, b) => a.parsed.port - b.parsed.port)) {
    const cell = { if_index: x.port.if_index, label: x.parsed.label };
    if (x.parsed.port % 2 === 1) top[col(x.parsed.port)] = cell;
    else bottom[col(x.parsed.port)] = cell;
  }
  return { key, module, uplink, top, bottom, gapEvery: 6 };
}

/** The member's main module: the module with the most R1/R4 ports (ties → lowest). */
export function mainModule(items: Item[]): number | null {
  const counts = new Map<number, number>();
  for (const x of items)
    if (x.parsed.rule === 'R1' || x.parsed.rule === 'R4')
      counts.set(x.parsed.module, (counts.get(x.parsed.module) ?? 0) + 1);
  let best: number | null = null;
  for (const [mod, n] of [...counts.entries()].sort((a, b) => a[0] - b[0]))
    if (best === null || n > (counts.get(best) ?? 0)) best = mod;
  return best;
}

/** Whether a parsed port belongs to its member's uplink group (§ 11.4 rules 1–3). */
export function isUplinkPort(x: Item, main: number | null): boolean {
  if (x.parsed.uplinkHint) return true;
  if ((x.port.speed_bps ?? 0) >= TEN_G) return true;
  if ((x.parsed.rule === 'R1' || x.parsed.rule === 'R4') && main !== null)
    return x.parsed.module !== main;
  return false;
}

export function layoutFaceplate(ports: Port[]): Faceplate {
  const physical = ports.filter((p) => p.kind === 'physical');
  if (!physical.length) return { mode: 'parsed', members: [] };

  const parsed: Item[] = [];
  const unparsed: Port[] = [];
  const seen = new Set<string>();
  let duplicate = false;
  for (const port of physical) {
    const r = parsePortName(portLabelSource(port));
    if (!r) {
      unparsed.push(port);
      continue;
    }
    const k = `${r.member}/${r.module}/${r.port}`;
    if (seen.has(k)) duplicate = true;
    seen.add(k);
    parsed.push({ port, parsed: r });
  }

  if (duplicate || unparsed.length / physical.length > 0.2) {
    return {
      mode: 'fallback',
      members: [{ member: 1, label: null, blocks: [positional('fallback', physical)] }],
    };
  }

  const byMember = new Map<number, Item[]>();
  for (const x of parsed)
    byMember.set(x.parsed.member, [...(byMember.get(x.parsed.member) ?? []), x]);
  // Unparsed ports only (≤ 20 %): they still need a member to live in.
  if (!byMember.size) byMember.set(1, []);
  const memberIds = [...byMember.keys()].sort((a, b) => a - b);
  const single = memberIds.length === 1;

  const members: FaceplateMember[] = memberIds.map((member, mi) => {
    const items = byMember.get(member) ?? [];
    const main = mainModule(items);
    const up: Item[] = [];
    const modules = new Map<number, Item[]>();
    for (const x of items) {
      if (isUplinkPort(x, main)) up.push(x);
      else modules.set(x.parsed.module, [...(modules.get(x.parsed.module) ?? []), x]);
    }
    const blocks: FaceplateBlock[] = [...modules.keys()]
      .sort((a, b) => a - b)
      .map((mod) => oddEvenBlock(`m${member}-${mod}`, mod, false, modules.get(mod) ?? []));
    if (mi === 0 && unparsed.length) blocks.push(positional('other', unparsed));
    if (up.length) blocks.push(oddEvenBlock(`m${member}-uplink`, null, true, up));
    return { member, label: single ? null : `Member ${member}`, blocks };
  });
  return { mode: 'parsed', members };
}

/** Column indexes after which a visual gap is drawn (every `gapEvery` columns, not at the end). */
export function gapAfter(block: FaceplateBlock, col: number): boolean {
  return (col + 1) % block.gapEvery === 0 && col + 1 < block.top.length;
}
