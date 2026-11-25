// [profiles] Credential modes of groups and devices (api.md § Groups and devices). Pure.
import type { TFunction } from 'i18next';
import type {
  CredentialKind,
  CredentialMode,
  CredentialOrigin,
  NodeCredentialRefs,
  SnmpCredentialsInput,
} from '../api/types';
import type { SshCredentialsInput } from '../api/types3';

/** The per-kind choice of a group / device form. */
export type KindChoice = {
  mode: CredentialMode;
  /** selected profile (mode = profile) */
  profileId: number | null;
  /** the user edited the manual fields of this kind */
  manualTouched: boolean;
  /**
   * mode = manual from a saved profile node that keeps hidden manual data: restore that data
   * (true) or enter new credentials (false).
   */
  restore: boolean;
};

type NodeLike = Partial<NodeCredentialRefs> & {
  snmp_credentials?: unknown;
  ssh_credentials?: unknown;
};

/** The node keeps hidden manual data of this kind (mode profile; owner decision Q2). */
export function hasSavedManual(node: NodeLike | null | undefined, kind: CredentialKind): boolean {
  if (!node) return false;
  return !!(kind === 'snmp' ? node.snmp_has_saved_manual : node.ssh_has_saved_manual);
}

/** The saved mode of a node; derived from `*_credentials` when an older server omits `*_mode`. */
export function nodeMode(node: NodeLike | null | undefined, kind: CredentialKind): CredentialMode {
  if (!node) return 'inherit';
  const mode = kind === 'snmp' ? node.snmp_mode : node.ssh_mode;
  if (mode) return mode;
  const creds = kind === 'snmp' ? node.snmp_credentials : node.ssh_credentials;
  return creds ? 'manual' : 'inherit';
}

export function nodeProfileId(
  node: NodeLike | null | undefined,
  kind: CredentialKind,
): number | null {
  if (!node) return null;
  return (kind === 'snmp' ? node.snmp_profile_id : node.ssh_profile_id) ?? null;
}

/** Initial form choice of a node (new node: inherit). */
export function initialChoice(node: NodeLike | null | undefined, kind: CredentialKind): KindChoice {
  return {
    mode: nodeMode(node, kind),
    profileId: nodeProfileId(node, kind),
    manualTouched: false,
    restore: hasSavedManual(node, kind),
  };
}

export type CredentialPatch = {
  snmp_credentials?: SnmpCredentialsInput | null;
  snmp_profile_id?: number | null;
  ssh_credentials?: SshCredentialsInput | null;
  ssh_profile_id?: number | null;
};

export type PatchArgs = {
  kind: CredentialKind;
  /** POST of a new node (an absent kind means inherit) */
  isNew: boolean;
  initialMode: CredentialMode;
  initialProfileId: number | null;
  mode: CredentialMode;
  profileId: number | null;
  /** the manual form as request value (only read in mode manual) */
  manual: SnmpCredentialsInput | SshCredentialsInput | null;
  manualTouched: boolean;
  /** the node keeps hidden manual data (mode profile) */
  savedManual: boolean;
  /** restore the hidden manual data instead of sending `manual` */
  restore: boolean;
};

/** Request of "switch back to the saved manual settings" (profile with dormant data → manual). */
export function restorePatch(kind: CredentialKind): CredentialPatch {
  return { [`${kind}_profile_id`]: null } as CredentialPatch;
}

/**
 * The request keys of one kind, implementing the mode table of api.md: a kind the user did not
 * change sends nothing (so a profile reference is never dropped by an unrelated edit).
 *
 * - inherit: `K_credentials: null` when leaving manual / profile (clears the reference and any
 *   saved manual data); nothing otherwise.
 * - profile: `K_profile_id: <id>` when the reference is new or different (manual data is kept
 *   hidden by the server).
 * - manual: `K_credentials: {…}` when coming from another mode or after an edit; from a profile
 *   with saved manual data and `restore`, the restore request instead.
 *
 * Throws when mode = profile without a profile (the form must block saving first).
 */
export function buildCredentialPatch(a: PatchArgs): CredentialPatch {
  const credKey = `${a.kind}_credentials` as const;
  const refKey = `${a.kind}_profile_id` as const;
  const from = a.isNew ? 'inherit' : a.initialMode;
  switch (a.mode) {
    case 'inherit':
      return from === 'inherit' ? {} : { [credKey]: null };
    case 'profile':
      if (a.profileId === null) throw new Error('profile mode without a profile');
      if (from === 'profile' && a.profileId === a.initialProfileId) return {};
      return { [refKey]: a.profileId };
    case 'manual':
      if (from === 'manual' && !a.manualTouched) return {};
      if (from === 'profile' && a.savedManual && a.restore) return restorePatch(a.kind);
      if (a.manual === null) throw new Error('manual mode without credentials');
      return { [credKey]: a.manual };
  }
}

/**
 * Switching to inherit deletes the node's manual data — the visible one (mode manual) or the
 * hidden one kept under a profile: ask first. Choosing a profile keeps it (no question).
 */
export function needsDiscardConfirm(
  initialMode: CredentialMode,
  savedManual: boolean,
  isNew: boolean,
  next: CredentialMode,
): boolean {
  if (isNew || next !== 'inherit') return false;
  return initialMode === 'manual' || (initialMode === 'profile' && savedManual);
}

/** "Copy into manual" overwrites the node's manual data (visible or hidden): ask first. */
export function copyNeedsConfirm(initialMode: CredentialMode, savedManual: boolean): boolean {
  return initialMode === 'manual' || savedManual;
}

/** Whether the choice can be saved (a profile must be chosen in mode profile). */
export function choiceValid(c: KindChoice): boolean {
  return c.mode !== 'profile' || c.profileId !== null;
}

/**
 * The origin line: "Inherited from group X · profile Y", "Profile Y", "Manual",
 * "Built-in default (v2c, public)", "No SSH credentials".
 */
export function originText(origin: CredentialOrigin | null | undefined, t: TFunction): string {
  if (!origin) return t('credMode.origin.unknown');
  switch (origin.source) {
    case 'default':
      return t('credMode.origin.default');
    case 'none':
      return t('credMode.origin.none');
    case 'self':
      return origin.mode === 'profile'
        ? t('credMode.origin.selfProfile', { profile: origin.profile_name ?? '' })
        : t('credMode.origin.selfManual');
    case 'group':
      return origin.mode === 'profile'
        ? t('credMode.origin.groupProfile', {
            group: origin.group_name ?? '',
            profile: origin.profile_name ?? '',
          })
        : t('credMode.origin.groupManual', { group: origin.group_name ?? '' });
  }
}
