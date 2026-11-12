/**
 * [1.3.1] Named enum value maps (shared code tables, e.g. the HPE Comware hardware fault codes).
 * The table lives in the backend (netmon/poller/value_maps.py) and comes with
 * GET /api/sensor-types (`value_maps`); this module keeps the last copy so value texts can be
 * shown in the viewer's language anywhere.
 */
import type { NamedValueMap } from '../api/types';

let maps: Record<string, NamedValueMap> = {};
let version = 0;
const listeners = new Set<() => void>();

export function setNamedValueMaps(m: Record<string, NamedValueMap> | undefined): void {
  if (!m || typeof m !== 'object' || m === maps) return;
  maps = m;
  version += 1;
  listeners.forEach((l) => l());
}

/** [1.3.2] for useSyncExternalStore: components re-render once the maps arrive */
export function subscribeNamedValueMaps(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

export function namedValueMapsVersion(): number {
  return version;
}

export function namedValueMap(ref: unknown): NamedValueMap | undefined {
  return typeof ref === 'string' && ref ? maps[ref] : undefined;
}

export function pickLang(t: { en: string; he?: string } | undefined, lang: string): string {
  if (!t) return '';
  return (lang.startsWith('he') ? t.he : undefined) || t.en || '';
}

/** "(<mibName>, <code>)" — the technical suffix the backend puts after a named-map text */
const PAIR_RE = /\(([A-Za-z][\w.-]*), (-?\d+)\)/g;

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * [1.3.2] The backend writes a sensor's status message once, in the server's default language
 * (the notifications use it as is), so a message that came from a named value map reaches the
 * UI in that language: "<label>: <text> (<mibName>, <code>)", or for a code the table does not
 * know "<label>: <unknown text with the code>". This rewrites the text part into the viewer's
 * language (both directions), wherever it stands in the string (e.g. after an "Unreachable
 * (parent X down): " prefix). Everything else, and every string while the maps are not loaded,
 * is returned unchanged.
 */
export function localizeMessage(message: string | null | undefined, lang: string): string {
  if (typeof message !== 'string' || !message) return message ?? '';
  const all = Object.values(maps);
  if (all.length === 0) return message;

  let out = '';
  let last = 0;
  for (const m of message.matchAll(PAIR_RE)) {
    const at = m.index ?? 0;
    const head = message.slice(last, at);
    let done = false;
    // an entry whose en/he text stands right before the pair wins; else the pair alone decides
    // (a text the server has since reworded) and the text is what follows the last ": "
    for (const exact of [true, false]) {
      for (const map of all) {
        if (!Array.isArray(map?.codes)) continue;
        for (const c of map.codes) {
          if (c.mib !== m[1] || String(c.value) !== m[2]) continue;
          const target = pickLang(c.text, lang);
          if (!target) continue;
          let cut = -1;
          if (exact) {
            for (const known of [c.text?.en, c.text?.he]) {
              if (known && head.endsWith(`${known} `)) cut = head.length - known.length - 1;
            }
          } else if (head.endsWith(' ') && head.trim()) {
            const sep = head.lastIndexOf(': ', head.length - 2);
            if (sep >= 0) cut = sep + 2;
            else if (last === 0) cut = 0;
          }
          if (cut < 0) continue;
          out += `${head.slice(0, cut)}${target} `;
          done = true;
          break;
        }
        if (done) break;
      }
      if (done) break;
    }
    if (!done) out += head;
    out += m[0];
    last = at + m[0].length;
  }
  out += message.slice(last);

  for (const map of all) {
    const target = pickLang(map?.unknown, lang);
    if (!target) continue;
    for (const src of [map.unknown.en, map.unknown.he]) {
      if (!src || src === target || !src.includes('{code}')) continue;
      const re = new RegExp(src.split('{code}').map(escapeRe).join('(-?\\d+)'), 'g');
      out = out.replace(re, (_all, code: string) => target.split('{code}').join(code));
    }
  }
  return out;
}
