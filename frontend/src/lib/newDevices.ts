// [3] New-device alerts helpers (architecture.md § 13.4, data-model § [3] New-device alerts). Pure.
import type { KnownMac, NewDeviceStatus } from '../api/types3';

const BITS: Record<number, 24 | 28 | 36 | 48> = { 6: 24, 7: 28, 9: 36, 12: 48 };

/**
 * Normalises an allow-list prefix like the server: MAC, OUI or 28/36-bit prefix; separators
 * `:` `-` `.` or none. Returns lower-case hex with colons between octets, or null when invalid.
 */
export function normalizeMacPrefix(
  input: string,
): { prefix: string; bits: 24 | 28 | 36 | 48 } | null {
  const hex = input
    .trim()
    .toLowerCase()
    .replace(/[:.\-\s]/g, '');
  if (!/^[0-9a-f]+$/.test(hex)) return null;
  const bits = BITS[hex.length];
  if (!bits) return null;
  const parts: string[] = [];
  for (let i = 0; i < hex.length; i += 2) parts.push(hex.slice(i, i + 2));
  return { prefix: parts.join(':'), bits };
}

/** The OUI (first three octets) of a MAC, for "Allow vendor". */
export function ouiOf(mac: string): string {
  const n = normalizeMacPrefix(mac);
  if (!n) return mac;
  return n.prefix.split(':').slice(0, 3).join(':');
}

/** Whether a MAC matches an allow-list prefix (the MAC's first `bits` bits). */
export function macMatches(mac: string, prefix: string): boolean {
  const m = mac.toLowerCase().replace(/[^0-9a-f]/g, '');
  const p = prefix.toLowerCase().replace(/[^0-9a-f]/g, '');
  return m.length === 12 && p.length > 0 && m.startsWith(p);
}

/** Learning progress 0–1 from the end of the learning period and its length in days. */
export function learningProgress(
  s: Pick<NewDeviceStatus, 'learning' | 'learning_until'>,
  learningDays: number,
  now: number,
): number {
  if (!s.learning || !s.learning_until) return 1;
  const end = Date.parse(s.learning_until);
  if (Number.isNaN(end) || learningDays <= 0) return 1;
  const start = end - learningDays * 86_400_000;
  return Math.min(1, Math.max(0, (now - start) / (end - start)));
}

/** Where a MAC was first seen ("sw-1 / Gi1/0/7 · VLAN 20"). */
export function firstLocation(m: KnownMac): string {
  const parts = [m.first_device_name, m.first_port_name].filter(Boolean).join(' / ');
  return m.first_vlan !== null && m.first_vlan !== undefined
    ? `${parts}${parts ? ' · ' : ''}VLAN ${m.first_vlan}`
    : parts;
}

/** Selection toggling for bulk acknowledge (keeps order of first selection). */
export function toggleSelection(sel: string[], mac: string, on: boolean): string[] {
  if (on) return sel.includes(mac) ? sel : [...sel, mac];
  return sel.filter((x) => x !== mac);
}

export const MAX_ACK_BATCH = 500;
