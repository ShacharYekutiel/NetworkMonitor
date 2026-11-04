import type {
  SnmpAuthProtocol,
  SnmpCredentials,
  SnmpCredentialsInput,
  SnmpPrivProtocol,
  SnmpSecurityLevel,
  SnmpVersion,
} from '../api/types';

export type SnmpFormState = {
  inherit: boolean;
  version: SnmpVersion;
  port: string;
  timeout_s: string;
  retries: string;
  community: string;
  v3_username: string;
  v3_security_level: SnmpSecurityLevel;
  v3_auth_protocol: SnmpAuthProtocol;
  v3_auth_key: string;
  v3_auth_key_clear: boolean;
  v3_priv_protocol: SnmpPrivProtocol;
  v3_priv_key: string;
  v3_priv_key_clear: boolean;
};

export function snmpFormFrom(c: SnmpCredentials | null): SnmpFormState {
  return {
    inherit: c === null,
    version: c?.version ?? 'v2c',
    port: String(c?.port ?? 161),
    timeout_s: String(c?.timeout_s ?? 2),
    retries: String(c?.retries ?? 1),
    community: c?.community ?? 'public',
    v3_username: c?.v3_username ?? '',
    v3_security_level: c?.v3_security_level ?? 'authPriv',
    v3_auth_protocol: c?.v3_auth_protocol ?? 'SHA',
    v3_auth_key: '',
    v3_auth_key_clear: false,
    v3_priv_protocol: c?.v3_priv_protocol ?? 'AES128',
    v3_priv_key: '',
    v3_priv_key_clear: false,
  };
}

/**
 * Builds the request value. Secrets follow api.md: omitted/null keeps the stored value,
 * "" clears it, any other string replaces it.
 */
export function snmpFormToInput(f: SnmpFormState): SnmpCredentialsInput | null {
  if (f.inherit) return null;
  const out: SnmpCredentialsInput = {
    version: f.version,
    port: Number(f.port),
    timeout_s: Number(f.timeout_s),
    retries: Number(f.retries),
    community: f.version === 'v3' ? null : f.community,
    v3_username: null,
    v3_security_level: null,
    v3_auth_protocol: null,
    v3_priv_protocol: null,
  };
  if (f.version === 'v3') {
    out.v3_username = f.v3_username || null;
    out.v3_security_level = f.v3_security_level;
    const auth = f.v3_security_level !== 'noAuthNoPriv';
    const priv = f.v3_security_level === 'authPriv';
    out.v3_auth_protocol = auth ? f.v3_auth_protocol : null;
    out.v3_priv_protocol = priv ? f.v3_priv_protocol : null;
    if (f.v3_auth_key_clear || !auth) out.v3_auth_key = '';
    else if (f.v3_auth_key) out.v3_auth_key = f.v3_auth_key;
    if (f.v3_priv_key_clear || !priv) out.v3_priv_key = '';
    else if (f.v3_priv_key) out.v3_priv_key = f.v3_priv_key;
  }
  return out;
}
