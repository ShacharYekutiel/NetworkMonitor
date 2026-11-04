/**
 * Client-side parsing of discovery scan targets (data-model.md § Scan targets).
 * Used only for hints while typing; the backend validates again (422 / too_many_hosts).
 */

export const MAX_HOSTS = 4096;
export const MAX_TARGETS = 64;

export type TargetKind = 'cidr' | 'range' | 'ipv4' | 'ipv6' | 'hostname';
export type TargetError = 'invalid' | 'rejected' | 'prefix' | 'order';

export type ParsedTarget =
  | {
      text: string;
      ok: true;
      kind: TargetKind;
      /** Hosts this entry expands to (before de-duplication across entries). */
      hosts: number;
      /** Set when the backend will normalise the entry (CIDR host bits). */
      normalized: string | null;
      /** IPv4 interval [first, last] (inclusive) after exclusions, for de-duplication. */
      v4?: [number, number];
      /** De-duplication key for IPv6 / hostnames. */
      key?: string;
    }
  | { text: string; ok: false; error: TargetError };

export type TargetsSummary = {
  entries: ParsedTarget[];
  /** Distinct hosts after de-duplication across entries (errors excluded). */
  totalHosts: number;
  errors: Extract<ParsedTarget, { ok: false }>[];
  tooManyHosts: boolean;
  tooManyEntries: boolean;
  /** True when there is at least one entry and nothing blocks submitting. */
  valid: boolean;
};

/** Splits the textarea value: one target per line; commas and spaces also separate. */
export function splitTargets(text: string): string[] {
  return text
    .split(/[\s,;]+/)
    .map((x) => x.trim())
    .filter(Boolean);
}

export function parseIpv4(s: string): number | null {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(s);
  if (!m) return null;
  let n = 0;
  for (let i = 1; i <= 4; i++) {
    const part = m[i];
    if (part.length > 1 && part.startsWith('0')) return null; // no ambiguous octal-looking octets
    const o = Number(part);
    if (o > 255) return null;
    n = n * 256 + o;
  }
  return n;
}

export function ipv4ToString(n: number): string {
  return [n >>> 24, (n >>> 16) & 255, (n >>> 8) & 255, n & 255].join('.');
}

/** Returns the 8 groups of an IPv6 address, or null. Accepts an embedded IPv4 tail. */
export function parseIpv6(s: string): number[] | null {
  if (!s.includes(':') || /[^0-9a-fA-F:.]/.test(s)) return null;
  let str = s;
  let tail: number[] = [];
  if (str.includes('.')) {
    const i = str.lastIndexOf(':');
    const v4 = parseIpv4(str.slice(i + 1));
    if (v4 === null) return null;
    tail = [v4 >>> 16, v4 & 0xffff];
    str = str.slice(0, i + 1);
    if (!str.endsWith('::')) str = str.slice(0, -1);
  }
  const halves = str.split('::');
  if (halves.length > 2) return null;
  const parse = (h: string) => (h === '' ? [] : h.split(':'));
  const head = parse(halves[0]);
  const rest = halves.length === 2 ? parse(halves[1]) : [];
  if ([...head, ...rest].some((g) => !/^[0-9a-fA-F]{1,4}$/.test(g))) return null;
  const h = head.map((g) => parseInt(g, 16));
  const r = rest.map((g) => parseInt(g, 16));
  const total = h.length + r.length + tail.length;
  if (halves.length === 1) return total === 8 ? [...h, ...tail] : null;
  if (total > 7) return null;
  return [...h, ...new Array<number>(8 - total).fill(0), ...r, ...tail];
}

const HOST_LABEL = /^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$/;

export function isHostname(s: string): boolean {
  const h = s.endsWith('.') ? s.slice(0, -1) : s;
  if (!h || h.length > 253) return false;
  const labels = h.split('.');
  if (!labels.every((l) => HOST_LABEL.test(l))) return false;
  // All-numeric names look like (broken) IPv4 addresses; never treat them as hostnames.
  return !labels.every((l) => /^\d+$/.test(l));
}

/** IPv4 blocks that discovery refuses: 0.0.0.0/8, 224.0.0.0/4 (multicast), 240.0.0.0/4. */
const REJECTED_V4: [number, number][] = [
  [0, 0x00ffffff],
  [0xe0000000, 0xefffffff],
  [0xf0000000, 0xffffffff],
];

function intersectsRejected(a: number, b: number): boolean {
  return REJECTED_V4.some(([lo, hi]) => a <= hi && b >= lo);
}

export function parseTarget(text: string): ParsedTarget {
  const bad = (error: TargetError): ParsedTarget => ({ text, ok: false, error });

  // CIDR
  if (text.includes('/')) {
    const [addr, prefixStr, ...extra] = text.split('/');
    if (extra.length || !/^\d{1,2}$/.test(prefixStr ?? '')) return bad('invalid');
    const ip = parseIpv4(addr);
    if (ip === null) return bad('invalid'); // IPv6 CIDR is not supported either
    const prefix = Number(prefixStr);
    if (prefix < 8 || prefix > 32) return bad('prefix');
    const size = 2 ** (32 - prefix);
    const net = Math.floor(ip / size) * size;
    const bcast = net + size - 1;
    if (intersectsRejected(net, bcast)) return bad('rejected');
    const first = prefix <= 30 ? net + 1 : net;
    const last = prefix <= 30 ? bcast - 1 : bcast;
    const canonical = `${ipv4ToString(net)}/${prefix}`;
    return {
      text,
      ok: true,
      kind: 'cidr',
      hosts: last - first + 1,
      normalized: canonical !== text ? canonical : null,
      v4: [first, last],
    };
  }

  // Ranges
  const dash = text.indexOf('-');
  if (dash > 0 && parseIpv4(text.slice(0, dash)) !== null) {
    const start = parseIpv4(text.slice(0, dash))!;
    const right = text.slice(dash + 1);
    let end: number | null;
    if (/^\d{1,3}$/.test(right)) {
      const last = Number(right);
      end = last <= 255 ? Math.floor(start / 256) * 256 + last : null;
    } else {
      end = parseIpv4(right);
    }
    if (end === null) return bad('invalid');
    if (end < start) return bad('order');
    if (intersectsRejected(start, end)) return bad('rejected');
    return {
      text,
      ok: true,
      kind: 'range',
      hosts: end - start + 1,
      normalized: null,
      v4: [start, end],
    };
  }

  const v4 = parseIpv4(text);
  if (v4 !== null) {
    if (intersectsRejected(v4, v4)) return bad('rejected');
    return { text, ok: true, kind: 'ipv4', hosts: 1, normalized: null, v4: [v4, v4] };
  }

  const v6 = parseIpv6(text);
  if (v6 !== null) {
    if (v6.every((g) => g === 0) || v6[0] >= 0xff00) return bad('rejected');
    return {
      text,
      ok: true,
      kind: 'ipv6',
      hosts: 1,
      normalized: null,
      key: v6.map((g) => g.toString(16)).join(':'),
    };
  }

  if (isHostname(text)) {
    return {
      text,
      ok: true,
      kind: 'hostname',
      hosts: 1,
      normalized: null,
      key: text.toLowerCase().replace(/\.$/, ''),
    };
  }
  return bad('invalid');
}

/** Number of distinct addresses covered by possibly overlapping inclusive intervals. */
export function countMerged(intervals: [number, number][]): number {
  const sorted = [...intervals].sort((a, b) => a[0] - b[0]);
  let total = 0;
  let cur: [number, number] | null = null;
  for (const [a, b] of sorted) {
    if (!cur || a > cur[1] + 1) {
      if (cur) total += cur[1] - cur[0] + 1;
      cur = [a, b];
    } else if (b > cur[1]) cur[1] = b;
  }
  if (cur) total += cur[1] - cur[0] + 1;
  return total;
}

export function summarizeTargets(input: string | string[]): TargetsSummary {
  const list = Array.isArray(input) ? input : splitTargets(input);
  const entries = list.map(parseTarget);
  const errors = entries.filter((e): e is Extract<ParsedTarget, { ok: false }> => !e.ok);
  const v4: [number, number][] = [];
  const keys = new Set<string>();
  for (const e of entries) {
    if (!e.ok) continue;
    if (e.v4) v4.push(e.v4);
    else if (e.key) keys.add(`${e.kind}:${e.key}`);
  }
  const totalHosts = countMerged(v4) + keys.size;
  const tooManyHosts = totalHosts > MAX_HOSTS;
  const tooManyEntries = list.length > MAX_TARGETS;
  return {
    entries,
    totalHosts,
    errors,
    tooManyHosts,
    tooManyEntries,
    valid: list.length > 0 && !errors.length && !tooManyHosts && !tooManyEntries,
  };
}
