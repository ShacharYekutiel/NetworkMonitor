// [2b-3] Active Directory helpers. Pure.
import type { LdapSettings, LdapSettingsInput } from '../api/types';

export type LoginName =
  | { kind: 'sam'; sam: string }
  | { kind: 'upn'; upn: string; sam: string | null }
  | { kind: 'foreign'; domain: string };

/**
 * How the server reads a login name (mirror of `ldap_pure.parse_login`): "user",
 * "DOMAIN\\user" (DOMAIN = NetBIOS name or the domain's first label, case-insensitive), or
 * "user@suffix" (a sAMAccountName fallback when the suffix is the domain).
 */
export function parseLogin(raw: string, netbios: string, domain: string): LoginName {
  const name = raw.trim();
  const bs = name.indexOf('\\');
  if (bs >= 0) {
    const dom = name.slice(0, bs);
    const user = name.slice(bs + 1);
    const ok = [netbios, domain.split('.')[0]].some(
      (d) => !!d && d.toLowerCase() === dom.toLowerCase(),
    );
    return ok ? { kind: 'sam', sam: user } : { kind: 'foreign', domain: dom };
  }
  const at = name.lastIndexOf('@');
  if (at > 0) {
    const suffix = name.slice(at + 1).toLowerCase();
    return {
      kind: 'upn',
      upn: name,
      sam: domain && suffix === domain.toLowerCase() ? name.slice(0, at) : null,
    };
  }
  return { kind: 'sam', sam: name };
}

/** host | host:port | [v6]:port */
export function isLdapServer(s: string): boolean {
  const v = s.trim();
  if (!v) return false;
  const m6 = v.match(/^\[([0-9a-fA-F:.]+)\](?::(\d{1,5}))?$/);
  if (m6) return !m6[2] || (Number(m6[2]) >= 1 && Number(m6[2]) <= 65535);
  const m = v.match(/^([A-Za-z0-9.-]+)(?::(\d{1,5}))?$/);
  if (!m) return false;
  return !m[2] || (Number(m[2]) >= 1 && Number(m[2]) <= 65535);
}

/** A loose DN check ("CN=x,OU=y,DC=z"): every RDN has attr=value. */
export function looksLikeDn(s: string): boolean {
  const v = s.trim();
  if (!v) return false;
  return v
    .split(/(?<!\\),/)
    .every(
      (rdn) => /^\s*[A-Za-z][A-Za-z0-9-]*\s*=\s*\S/.test(rdn) || /^\s*\d+(\.\d+)*\s*=/.test(rdn),
    );
}

/** "DC=mnidom,DC=net" from "mnidom.net". */
export function baseDnFromDomain(domain: string): string {
  return domain
    .trim()
    .split('.')
    .filter(Boolean)
    .map((p) => `DC=${p}`)
    .join(',');
}

export type LdapForm = {
  servers: string;
  security: LdapSettings['security'];
  tls_verify: boolean;
  domain: string;
  netbios_domain: string;
  base_dn: string;
  bind_dn: string;
  password: string;
  clearPassword: boolean;
  admin_group_dn: string;
  viewer_group_dn: string;
  timeout_s: string;
};

export function ldapForm(l: LdapSettings | undefined): LdapForm {
  return {
    servers: (l?.servers ?? []).join('\n'),
    security: l?.security ?? 'ldaps',
    tls_verify: l?.tls_verify ?? true,
    domain: l?.domain ?? '',
    netbios_domain: l?.netbios_domain ?? '',
    base_dn: l?.base_dn ?? '',
    bind_dn: l?.bind_dn ?? '',
    password: '',
    clearPassword: false,
    admin_group_dn: l?.admin_group_dn ?? '',
    viewer_group_dn: l?.viewer_group_dn ?? '',
    timeout_s: String(l?.timeout_s ?? 5),
  };
}

/** Request object; the password follows § Secret fields (omitted keep, "" clear, string replace). */
export function ldapInput(f: LdapForm): LdapSettingsInput {
  const out: LdapSettingsInput = {
    servers: f.servers
      .split(/[\s,;]+/)
      .map((x) => x.trim())
      .filter(Boolean),
    security: f.security,
    tls_verify: f.tls_verify,
    domain: f.domain.trim(),
    netbios_domain: f.netbios_domain.trim(),
    base_dn: f.base_dn.trim(),
    bind_dn: f.bind_dn.trim(),
    admin_group_dn: f.admin_group_dn.trim(),
    viewer_group_dn: f.viewer_group_dn.trim(),
    timeout_s: Number(f.timeout_s),
  };
  if (f.clearPassword) out.bind_password = '';
  else if (f.password) out.bind_password = f.password;
  return out;
}

export function ldapFormErrors(f: LdapForm): Partial<Record<keyof LdapForm, string>> {
  const e: Partial<Record<keyof LdapForm, string>> = {};
  const servers = ldapInput(f).servers ?? [];
  if (servers.length > 8) e.servers = 'ldap.err.tooManyServers';
  else if (servers.some((s) => !isLdapServer(s))) e.servers = 'ldap.err.server';
  for (const k of ['base_dn', 'admin_group_dn', 'viewer_group_dn'] as const)
    if (f[k].trim() && !looksLikeDn(f[k])) e[k] = 'ldap.err.dn';
  const t = Number(f.timeout_s);
  if (!/^\d+$/.test(f.timeout_s.trim()) || t < 1 || t > 30) e.timeout_s = 'ldap.err.timeout';
  return e;
}
