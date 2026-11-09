// [2b-3] Traps & syslog settings form helpers. Pure.
import type {
  SnmpAuthProtocol,
  SnmpPrivProtocol,
  SnmpSecurityLevel,
  TrapV3User,
  TrapV3UserInput,
} from '../api/types';
import { isIpOrCidr } from './logs';

export type V3Row = {
  username: string;
  security_level: SnmpSecurityLevel;
  auth_protocol: SnmpAuthProtocol;
  priv_protocol: SnmpPrivProtocol;
  authKey: string;
  privKey: string;
  /** stored key (server *_set) — kept when the field is left empty */
  authSet: boolean;
  privSet: boolean;
  /** the stored username this row was loaded from (keys are kept only for the same username) */
  storedName: string | null;
};

export function v3Rows(users: TrapV3User[] | undefined): V3Row[] {
  return (users ?? []).map((u) => ({
    username: u.username,
    security_level: u.security_level,
    auth_protocol: u.auth_protocol ?? 'SHA',
    priv_protocol: u.priv_protocol ?? 'AES128',
    authKey: '',
    privKey: '',
    authSet: u.auth_key_set,
    privSet: u.priv_key_set,
    storedName: u.username,
  }));
}

export function newV3Row(): V3Row {
  return {
    username: '',
    security_level: 'authPriv',
    auth_protocol: 'SHA',
    priv_protocol: 'AES128',
    authKey: '',
    privKey: '',
    authSet: false,
    privSet: false,
    storedName: null,
  };
}

const needsAuth = (l: SnmpSecurityLevel) => l !== 'noAuthNoPriv';
const needsPriv = (l: SnmpSecurityLevel) => l === 'authPriv';

/** Request list: an empty key keeps the stored one (same username), a typed key replaces it. */
export function v3Input(rows: V3Row[]): TrapV3UserInput[] {
  return rows.map((r) => {
    const keep = r.storedName !== null && r.storedName === r.username.trim();
    const out: TrapV3UserInput = {
      username: r.username.trim(),
      security_level: r.security_level,
      auth_protocol: needsAuth(r.security_level) ? r.auth_protocol : null,
      priv_protocol: needsPriv(r.security_level) ? r.priv_protocol : null,
    };
    if (!needsAuth(r.security_level)) out.auth_key = '';
    else if (r.authKey) out.auth_key = r.authKey;
    else if (!keep) out.auth_key = '';
    if (!needsPriv(r.security_level)) out.priv_key = '';
    else if (r.privKey) out.priv_key = r.privKey;
    else if (!keep) out.priv_key = '';
    return out;
  });
}

/** Per-row error i18n key, or null. */
export function v3RowError(r: V3Row, all: V3Row[]): string | null {
  const name = r.username.trim();
  if (!name || name.length > 64) return 'ingestSettings.v3.errName';
  if (all.filter((x) => x.username.trim() === name).length > 1)
    return 'ingestSettings.v3.errDuplicate';
  const keep = r.storedName === name;
  if (needsAuth(r.security_level)) {
    if (!r.authKey && !(keep && r.authSet)) return 'ingestSettings.v3.errAuthKey';
    if (r.authKey && r.authKey.length < 8) return 'ingestSettings.v3.errKeyLength';
  }
  if (needsPriv(r.security_level)) {
    if (!r.privKey && !(keep && r.privSet)) return 'ingestSettings.v3.errPrivKey';
    if (r.privKey && r.privKey.length < 8) return 'ingestSettings.v3.errKeyLength';
  }
  return null;
}

/** Ignored sources textarea → list + invalid entries. */
export function parseSources(
  text: string,
  max = 256,
): { list: string[]; invalid: string[]; tooMany: boolean } {
  const list = [
    ...new Set(
      text
        .split(/[\s,;]+/)
        .map((x) => x.trim())
        .filter(Boolean),
    ),
  ];
  return { list, invalid: list.filter((x) => !isIpOrCidr(x)), tooMany: list.length > max };
}

export function intInRange(s: string, min: number, max: number): boolean {
  if (!/^\d+$/.test(s.trim())) return false;
  const n = Number(s);
  return n >= min && n <= max;
}
