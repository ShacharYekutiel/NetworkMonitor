// [3] Single sign-on helpers (architecture.md § 13.5, api.md § [3] Single sign-on). Pure.
import { SSO_ERRORS } from '../api/types3';

/** Set on logout so the login page does not sign in again automatically (api.md). */
export const SSO_SKIP_KEY = 'netmon.sso.skip';

export type SsoError = (typeof SSO_ERRORS)[number];

/** `?sso_error=<code>` of the OIDC callback redirect; unknown codes map to sso_failed. */
export function ssoErrorFromSearch(search: string): SsoError | null {
  const v = new URLSearchParams(search).get('sso_error');
  if (!v) return null;
  return (SSO_ERRORS as readonly string[]).includes(v) ? (v as SsoError) : 'sso_failed';
}

/** Whether the login page tries Kerberos on its own (feature ON + auto_login, not after logout/error). */
export function shouldAutoKerberos(o: {
  kerberos: { auto_login: boolean } | null | undefined;
  skip: boolean;
  ssoError: string | null;
}): boolean {
  return !!o.kerberos?.auto_login && !o.skip && !o.ssoError;
}

/** Reads/writes the skip flag; storage may be unavailable (private mode). */
export function readSkip(storage: Pick<Storage, 'getItem'> | null): boolean {
  try {
    return storage?.getItem(SSO_SKIP_KEY) === '1';
  } catch {
    return false;
  }
}
export function writeSkip(storage: Pick<Storage, 'setItem' | 'removeItem'> | null, on: boolean) {
  try {
    if (on) storage?.setItem(SSO_SKIP_KEY, '1');
    else storage?.removeItem(SSO_SKIP_KEY);
  } catch {
    /* ignore */
  }
}

const GUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function isGuid(s: string): boolean {
  return GUID_RE.test(s.trim());
}

/** Realms: upper-cased, de-duplicated, 1–8 (data-model § Kerberos settings). */
export function parseRealms(text: string): string[] {
  return [
    ...new Set(
      text
        .split(/[\s,;]+/)
        .map((x) => x.trim().toUpperCase())
        .filter(Boolean),
    ),
  ];
}
export function realmsError(list: string[]): string | null {
  if (!list.length) return 'kerberos.realmsRequired';
  if (list.length > 8) return 'kerberos.realmsTooMany';
  if (list.some((r) => !/^[A-Z0-9][A-Z0-9.-]*$/.test(r))) return 'kerberos.realmsInvalid';
  return null;
}

/** Role values (app role values or group object ids): one per line, ≤ 16. */
export function parseValues(text: string): string[] {
  return [
    ...new Set(
      text
        .split(/[\n,;]+/)
        .map((x) => x.trim())
        .filter(Boolean),
    ),
  ];
}

export type OidcFormCheck = {
  tenant_id: string;
  client_id: string;
  secretSet: boolean;
  admin_values: string[];
  viewer_values: string[];
  role_source: 'app_roles' | 'groups';
};

/** i18n keys of what turning `sso_oidc` ON would reject (data-model § OIDC settings). */
export function oidcMissing(f: OidcFormCheck): string[] {
  const out: string[] = [];
  if (!isGuid(f.tenant_id)) out.push('oidc.err.tenant');
  if (!isGuid(f.client_id)) out.push('oidc.err.client');
  if (!f.secretSet) out.push('oidc.err.secret');
  if (!f.admin_values.length && !f.viewer_values.length) out.push('oidc.err.values');
  if (f.admin_values.length > 16 || f.viewer_values.length > 16) out.push('oidc.err.tooMany');
  if (f.role_source === 'groups' && [...f.admin_values, ...f.viewer_values].some((v) => !isGuid(v)))
    out.push('oidc.err.groupIds');
  return out;
}

/** Relative `next` for the OIDC start URL (never an absolute URL). */
export function safeNext(path: string | null | undefined): string {
  if (!path || !path.startsWith('/') || path.startsWith('//') || path.startsWith('/login'))
    return '/';
  return path;
}
