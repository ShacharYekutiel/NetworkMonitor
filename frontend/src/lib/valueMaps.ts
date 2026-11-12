/**
 * [1.3.1] Named enum value maps (shared code tables, e.g. the HPE Comware hardware fault codes).
 * The table lives in the backend (netmon/poller/value_maps.py) and comes with
 * GET /api/sensor-types (`value_maps`); this module keeps the last copy so value texts can be
 * shown in the viewer's language anywhere.
 */
import type { NamedValueMap } from '../api/types';

let maps: Record<string, NamedValueMap> = {};

export function setNamedValueMaps(m: Record<string, NamedValueMap> | undefined): void {
  if (m && typeof m === 'object') maps = m;
}

export function namedValueMap(ref: unknown): NamedValueMap | undefined {
  return typeof ref === 'string' && ref ? maps[ref] : undefined;
}

export function pickLang(t: { en: string; he?: string } | undefined, lang: string): string {
  if (!t) return '';
  return (lang.startsWith('he') ? t.he : undefined) || t.en || '';
}
