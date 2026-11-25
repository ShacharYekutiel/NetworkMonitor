// [profiles] The request table of api.md § Groups and devices, row by row.
import { describe, expect, it } from 'vitest';
import type { TFunction } from 'i18next';
import type { CredentialOrigin } from '../api/types';
import {
  buildCredentialPatch,
  choiceValid,
  copyNeedsConfirm,
  hasSavedManual,
  initialChoice,
  needsDiscardConfirm,
  nodeMode,
  originText,
  type PatchArgs,
} from './credentials';

const MANUAL = { version: 'v2c' as const, community: 'x' };

function args(p: Partial<PatchArgs>): PatchArgs {
  return {
    kind: 'snmp',
    isNew: false,
    initialMode: 'inherit',
    initialProfileId: null,
    mode: 'inherit',
    profileId: null,
    manual: MANUAL,
    manualTouched: false,
    savedManual: false,
    restore: false,
    ...p,
  };
}

describe('buildCredentialPatch', () => {
  it('sends nothing for an unchanged kind (every mode)', () => {
    expect(buildCredentialPatch(args({ initialMode: 'inherit', mode: 'inherit' }))).toEqual({});
    expect(
      buildCredentialPatch(
        args({ initialMode: 'profile', initialProfileId: 4, mode: 'profile', profileId: 4 }),
      ),
    ).toEqual({});
    expect(buildCredentialPatch(args({ initialMode: 'manual', mode: 'manual' }))).toEqual({});
  });

  it('POST: inherit is the absence of both fields', () => {
    expect(buildCredentialPatch(args({ isNew: true, mode: 'inherit' }))).toEqual({});
  });

  it('POST: a profile or manual data is sent', () => {
    expect(buildCredentialPatch(args({ isNew: true, mode: 'profile', profileId: 2 }))).toEqual({
      snmp_profile_id: 2,
    });
    expect(buildCredentialPatch(args({ isNew: true, mode: 'manual' }))).toEqual({
      snmp_credentials: MANUAL,
    });
  });

  it('to profile: K_profile_id only (manual data kept dormant by the server)', () => {
    for (const initialMode of ['inherit', 'manual'] as const)
      expect(buildCredentialPatch(args({ initialMode, mode: 'profile', profileId: 7 }))).toEqual({
        snmp_profile_id: 7,
      });
  });

  it('another profile: K_profile_id', () => {
    expect(
      buildCredentialPatch(
        args({ initialMode: 'profile', initialProfileId: 4, mode: 'profile', profileId: 5 }),
      ),
    ).toEqual({ snmp_profile_id: 5 });
  });

  it('to inherit: K_credentials null (reference and dormant data removed)', () => {
    for (const initialMode of ['manual', 'profile'] as const)
      expect(buildCredentialPatch(args({ initialMode, mode: 'inherit' }))).toEqual({
        snmp_credentials: null,
      });
  });

  it('manual: an edit is sent; from inherit / profile always a new object', () => {
    expect(
      buildCredentialPatch(args({ initialMode: 'manual', mode: 'manual', manualTouched: true })),
    ).toEqual({ snmp_credentials: MANUAL });
    expect(buildCredentialPatch(args({ initialMode: 'inherit', mode: 'manual' }))).toEqual({
      snmp_credentials: MANUAL,
    });
    expect(buildCredentialPatch(args({ initialMode: 'profile', mode: 'manual' }))).toEqual({
      snmp_credentials: MANUAL,
    });
  });

  it('profile with dormant data → manual: restore = K_profile_id null, else new data', () => {
    const base = { initialMode: 'profile' as const, initialProfileId: 3, mode: 'manual' as const };
    expect(buildCredentialPatch(args({ ...base, savedManual: true, restore: true }))).toEqual({
      snmp_profile_id: null,
    });
    expect(buildCredentialPatch(args({ ...base, savedManual: true, restore: false }))).toEqual({
      snmp_credentials: MANUAL,
    });
    // restore without dormant data is meaningless: new data
    expect(buildCredentialPatch(args({ ...base, savedManual: false, restore: true }))).toEqual({
      snmp_credentials: MANUAL,
    });
  });

  it('uses the keys of the kind', () => {
    expect(
      buildCredentialPatch(
        args({ kind: 'ssh', initialMode: 'manual', mode: 'profile', profileId: 1 }),
      ),
    ).toEqual({ ssh_profile_id: 1 });
    expect(
      buildCredentialPatch(args({ kind: 'ssh', initialMode: 'manual', mode: 'inherit' })),
    ).toEqual({ ssh_credentials: null });
  });

  it('never sends both fields of a kind', () => {
    const modes = ['inherit', 'profile', 'manual'] as const;
    for (const initialMode of modes)
      for (const mode of modes)
        for (const restore of [true, false]) {
          const p = buildCredentialPatch(
            args({
              initialMode,
              mode,
              profileId: 1,
              savedManual: true,
              restore,
              manualTouched: true,
            }),
          );
          const both = 'snmp_credentials' in p && 'snmp_profile_id' in p;
          expect(both, `${initialMode}→${mode}`).toBe(false);
        }
  });

  it('throws for profile mode without a profile', () => {
    expect(() => buildCredentialPatch(args({ mode: 'profile', profileId: null }))).toThrow();
  });
});

describe('mode helpers', () => {
  it('derives the mode from an older payload without *_mode', () => {
    expect(nodeMode({ snmp_credentials: {} }, 'snmp')).toBe('manual');
    expect(nodeMode({ snmp_credentials: null }, 'snmp')).toBe('inherit');
    expect(nodeMode({ ssh_mode: 'profile', ssh_credentials: null }, 'ssh')).toBe('profile');
    expect(nodeMode(undefined, 'ssh')).toBe('inherit');
  });

  it('initial choice: restore preselected when dormant data exists', () => {
    expect(
      initialChoice(
        { snmp_mode: 'profile', snmp_profile_id: 9, snmp_has_saved_manual: true },
        'snmp',
      ),
    ).toEqual({ mode: 'profile', profileId: 9, manualTouched: false, restore: true });
    expect(hasSavedManual({ ssh_has_saved_manual: false }, 'ssh')).toBe(false);
  });

  it('confirms switching to inherit only when manual data would be deleted', () => {
    expect(needsDiscardConfirm('manual', false, false, 'inherit')).toBe(true);
    expect(needsDiscardConfirm('profile', true, false, 'inherit')).toBe(true);
    expect(needsDiscardConfirm('profile', false, false, 'inherit')).toBe(false);
    expect(needsDiscardConfirm('manual', false, false, 'profile')).toBe(false);
    expect(needsDiscardConfirm('manual', false, true, 'inherit')).toBe(false);
  });

  it('confirms a copy that overwrites manual data', () => {
    expect(copyNeedsConfirm('profile', true)).toBe(true);
    expect(copyNeedsConfirm('manual', false)).toBe(true);
    expect(copyNeedsConfirm('profile', false)).toBe(false);
  });

  it('profile mode needs a profile', () => {
    expect(
      choiceValid({ mode: 'profile', profileId: null, manualTouched: false, restore: false }),
    ).toBe(false);
    expect(
      choiceValid({ mode: 'inherit', profileId: null, manualTouched: false, restore: false }),
    ).toBe(true);
  });
});

describe('originText', () => {
  const t = ((k: string, o?: Record<string, string>) =>
    o ? `${k}(${Object.values(o).join(',')})` : k) as unknown as TFunction;
  const o = (p: Partial<CredentialOrigin>): CredentialOrigin => ({
    source: 'self',
    group_id: null,
    group_name: null,
    mode: null,
    profile_id: null,
    profile_name: null,
    ...p,
  });
  it('names the deciding node and profile', () => {
    expect(
      originText(
        o({
          source: 'group',
          group_name: 'TLV',
          mode: 'profile',
          profile_id: 1,
          profile_name: 'Core',
        }),
        t,
      ),
    ).toBe('credMode.origin.groupProfile(TLV,Core)');
    expect(originText(o({ source: 'group', group_name: 'TLV', mode: 'manual' }), t)).toBe(
      'credMode.origin.groupManual(TLV)',
    );
    expect(originText(o({ mode: 'profile', profile_name: 'Core' }), t)).toBe(
      'credMode.origin.selfProfile(Core)',
    );
    expect(originText(o({ mode: 'manual' }), t)).toBe('credMode.origin.selfManual');
    expect(originText(o({ source: 'default' }), t)).toBe('credMode.origin.default');
    expect(originText(o({ source: 'none' }), t)).toBe('credMode.origin.none');
    expect(originText(undefined, t)).toBe('credMode.origin.unknown');
  });
});
