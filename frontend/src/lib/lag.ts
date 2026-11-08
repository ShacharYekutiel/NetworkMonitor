// [2b-3] LAG membership in the ports view (architecture.md § 12.6). Pure.
import type { Port } from '../api/types';

/** Fixed palette of 6 per-LAG colours (by LAG ifIndex order). */
export const LAG_PALETTE = ['#7c3aed', '#0891b2', '#db2777', '#65a30d', '#ea580c', '#2563eb'];

export function portName(p: Pick<Port, 'name' | 'descr' | 'if_index'>): string {
  return p.name || p.descr || String(p.if_index);
}

export type LagInfo = { if_index: number; name: string; color: string; members: Port[] };

/** LAGs that have at least one member, sorted by ifIndex, with their colour and member ports. */
export function lagGroups(ports: Port[]): LagInfo[] {
  const byIndex = new Map(ports.map((p) => [p.if_index, p]));
  const members = new Map<number, Map<number, Port>>();
  const names = new Map<number, string>();
  const add = (lag: number, m: Port) => {
    if (!members.has(lag)) members.set(lag, new Map());
    members.get(lag)!.set(m.if_index, m);
  };
  for (const p of ports) {
    if (p.lag) {
      add(p.lag.if_index, p);
      names.set(p.lag.if_index, p.lag.name);
    }
    if (p.kind === 'lag')
      for (const mi of p.lag_members ?? []) {
        const m = byIndex.get(mi);
        if (m) add(p.if_index, m);
      }
  }
  return [...members.keys()]
    .sort((a, b) => a - b)
    .map((lag, i) => {
      const row = byIndex.get(lag);
      return {
        if_index: lag,
        name: row ? portName(row) : (names.get(lag) ?? String(lag)),
        color: LAG_PALETTE[i % LAG_PALETTE.length],
        members: [...members.get(lag)!.values()].sort((a, b) => a.if_index - b.if_index),
      };
    });
}

/** Colour of each member port (ifIndex → colour). */
export function lagColorByMember(groups: LagInfo[]): Map<number, string> {
  const out = new Map<number, string>();
  for (const g of groups) for (const m of g.members) out.set(m.if_index, g.color);
  return out;
}

/** "Members: 23, 24" — member names of a LAG row, in ifIndex order. */
export function lagMemberNames(lag: Pick<Port, 'lag_members'>, ports: Port[]): string[] {
  const byIndex = new Map(ports.map((p) => [p.if_index, p]));
  return [...(lag.lag_members ?? [])]
    .sort((a, b) => a - b)
    .map((i) => {
      const p = byIndex.get(i);
      return p ? portName(p) : String(i);
    });
}
