// [if-noise] Per-sensor / device / group overrides of the event-log noise protections
// (api.md "[if-noise] Overrides"): form state <-> API input, and which levels decide.
import type {
  EffectiveEventNoise,
  EventNoiseOverride,
  NoiseMode,
  NoiseResolved,
} from '../api/types';

export const NOISE_KEYS = ['debounce', 'flap', 'debounce_polls', 'debounce_min_s'] as const;
export type NoiseKey = (typeof NOISE_KEYS)[number];
export type NoiseLevel = 'sensor' | 'device' | 'group';

export const INHERIT_ALL: EventNoiseOverride = {
  debounce: 'inherit',
  flap: 'inherit',
  debounce_polls: null,
  debounce_min_s: null,
};

/** Form state of the editor: values as typed ('' = inherit). */
export type NoiseForm = { debounce: NoiseMode; flap: NoiseMode; polls: string; minS: string };

export function noiseFormFrom(ov?: EventNoiseOverride | null): NoiseForm {
  const o = ov ?? INHERIT_ALL;
  return {
    debounce: o.debounce,
    flap: o.flap,
    polls: o.debounce_polls === null ? '' : String(o.debounce_polls),
    minS: o.debounce_min_s === null ? '' : String(o.debounce_min_s),
  };
}

function parseInt0(v: string, min: number, max: number): number | null | 'invalid' {
  const s = v.trim();
  if (s === '') return null;
  if (!/^\d+$/.test(s)) return 'invalid';
  const n = Number(s);
  return n >= min && n <= max ? n : 'invalid';
}

export function noiseFormErrors(f: NoiseForm): { polls: boolean; minS: boolean } {
  return {
    polls: parseInt0(f.polls, 1, 10) === 'invalid',
    minS: parseInt0(f.minS, 0, 3600) === 'invalid',
  };
}

/** The complete override of a valid form (null if a value is invalid). */
export function noiseFormValue(f: NoiseForm): EventNoiseOverride | null {
  const polls = parseInt0(f.polls, 1, 10);
  const minS = parseInt0(f.minS, 0, 3600);
  if (polls === 'invalid' || minS === 'invalid') return null;
  return { debounce: f.debounce, flap: f.flap, debounce_polls: polls, debounce_min_s: minS };
}

/**
 * Only the keys that differ from what is stored (`undefined`: nothing to send). An invalid form
 * gives `undefined` too: check `noiseFormErrors` first.
 */
export function noiseFormToInput(
  f: NoiseForm,
  stored?: EventNoiseOverride | null,
): Partial<EventNoiseOverride> | undefined {
  const v = noiseFormValue(f);
  if (!v) return undefined;
  const base = stored ?? INHERIT_ALL;
  const out: Partial<EventNoiseOverride> = {};
  for (const k of NOISE_KEYS) {
    if (v[k] !== base[k]) (out as Record<string, unknown>)[k] = v[k];
  }
  return Object.keys(out).length ? out : undefined;
}

export function isOverridden(ov?: EventNoiseOverride | null): boolean {
  if (!ov) return false;
  return NOISE_KEYS.some((k) => ov[k] !== INHERIT_ALL[k]);
}

/** The keys some level (not the global settings) decides, in display order. */
export function decidedByOverride(
  eff?: EffectiveEventNoise | null,
): Array<{ key: NoiseKey; resolved: NoiseResolved<boolean | number> }> {
  if (!eff) return [];
  return NOISE_KEYS.filter((k) => eff[k].source !== 'global').map((k) => ({
    key: k,
    resolved: eff[k] as NoiseResolved<boolean | number>,
  }));
}

// ------------------------------------------------------------------ bulk
export type KeepMode = NoiseMode | 'keep';
/** keep = leave each sensor's value; inherit = clear it; set = `value` */
export type ValueChoice = { mode: 'keep' | 'inherit' | 'set'; value: string };
export type BulkNoiseForm = {
  debounce: KeepMode;
  flap: KeepMode;
  polls: ValueChoice;
  minS: ValueChoice;
};
export const BULK_KEEP: BulkNoiseForm = {
  debounce: 'keep',
  flap: 'keep',
  polls: { mode: 'keep', value: '' },
  minS: { mode: 'keep', value: '' },
};

export function bulkNoiseErrors(f: BulkNoiseForm): { polls: boolean; minS: boolean } {
  const bad = (c: ValueChoice, min: number, max: number) =>
    c.mode === 'set' && (c.value.trim() === '' || parseInt0(c.value, min, max) === 'invalid');
  return { polls: bad(f.polls, 1, 10), minS: bad(f.minS, 0, 3600) };
}

/** The bulk request body's `event_noise` (null: nothing chosen or a value invalid). */
export function bulkNoiseInput(f: BulkNoiseForm): Partial<EventNoiseOverride> | null {
  const err = bulkNoiseErrors(f);
  if (err.polls || err.minS) return null;
  const out: Partial<EventNoiseOverride> = {};
  if (f.debounce !== 'keep') out.debounce = f.debounce;
  if (f.flap !== 'keep') out.flap = f.flap;
  const val = (c: ValueChoice) => (c.mode === 'inherit' ? null : Number(c.value.trim()));
  if (f.polls.mode !== 'keep') out.debounce_polls = val(f.polls);
  if (f.minS.mode !== 'keep') out.debounce_min_s = val(f.minS);
  return Object.keys(out).length ? out : null;
}
