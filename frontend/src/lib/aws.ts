// [4] Pure helpers of the shared AWS connections UI (architecture § 15.1.10).
import type {
  AwsAccess,
  AwsAccessMode,
  AwsAuthMode,
  AwsConnection,
  AwsConnectionInput,
  AwsEndpointOverride,
  AwsOverrideService,
  AwsProxyMode,
} from '../api/types';

export const DEFAULT_AWS_REGION = 'us-east-1';

const REGION_RE = /^[a-z]{2}(-gov|-iso[a-z]*)?-[a-z]+-\d$/;
const ARN_RA = (kind: string) =>
  new RegExp(`^arn:aws:rolesanywhere:[a-z0-9-]+:\\d{12}:${kind}/[0-9a-zA-Z-]+$`);
const ARN_ROLE = /^arn:aws:iam::\d{12}:role\/[\w+=,.@/-]{1,512}$/;
const ACCESS_KEY_RE = /^(AKIA|ASIA)[A-Z0-9]{16}$/;
const SESSION_TOKEN_RE = /^[A-Za-z0-9+/=_-]{1,4096}$/;
/** Characters a source identity may carry (IAM: letters, digits and `_ . , + = @ -`). */
const SOURCE_IDENTITY_RE = /^[A-Za-z0-9_.,+=@-]+$/;
const EXTERNAL_ID_RE = /^[\w+=,.@:/-]{2,1224}$/;

export function isRegion(v: string): boolean {
  return REGION_RE.test(v.trim());
}

/**
 * The owner's first mistake: an ARN pasted into a template that already held `…:trust-anchor/`
 * (or pasted twice) — `arn:` appears more than once and the value never matches.
 */
export function arnDoubled(v: string): boolean {
  return (v.match(/arn:/gi) ?? []).length > 1;
}

export type ArnKind = 'trust_anchor' | 'profile' | 'role';
/** `'twice'` (doubled), `'invalid'` (pattern) or null; an empty value is not checked here. */
export function arnError(v: string, kind: ArnKind): 'twice' | 'invalid' | null {
  const s = v.trim();
  if (!s) return null;
  if (arnDoubled(s)) return 'twice';
  const ok =
    kind === 'role'
      ? ARN_ROLE.test(s)
      : ARN_RA(kind === 'trust_anchor' ? 'trust-anchor' : 'profile').test(s);
  return ok ? null : 'invalid';
}

export function accountOfArn(arn: string | null | undefined): string | null {
  return /^arn:aws:[a-z0-9-]*:[a-z0-9-]*:(\d{12}):/.exec((arn ?? '').trim())?.[1] ?? null;
}

/** A CN a source identity can carry unchanged (`cn_not_source_identity_safe` otherwise). */
export function cnSourceIdentitySafe(cn: string): boolean {
  return !cn.trim() || SOURCE_IDENTITY_RE.test(cn.trim());
}

/** The source identity Roles Anywhere sets for a CN (fact 3; `ID=<serial>` without a CN). */
export function sourceIdentityFor(cn: string | null): string | null {
  if (!cn) return null;
  return cn.length <= 61 ? `CN=${cn}` : cn;
}

/**
 * The AD CS command for the pending CSR. The *template name* (no spaces) is what the CA knows;
 * the display name gives 0x80094800 (CERTSRV_E_UNSUPPORTED_CERT_TYPE).
 */
export function certreqCommand(templateName: string, csrFile = 'netmonitor.csr'): string {
  const name = templateName.trim() || '<TemplateName>';
  const cer = csrFile.replace(/\.csr$/i, '') + '.cer';
  return `certreq -submit -attrib "CertificateTemplate:${name}" ${csrFile} ${cer}`;
}

/** A template name with a space is almost certainly the *display* name. */
export function looksLikeDisplayName(templateName: string): boolean {
  return /\s/.test(templateName.trim());
}

/** The owner's fourth mistake: a private key chosen where only the issued certificate belongs. */
export function looksLikePrivateKey(fileName: string, head = ''): boolean {
  if (/\.(key|pfx|p12)$/i.test(fileName)) return true;
  return /-----BEGIN (RSA |EC |ENCRYPTED )?PRIVATE KEY-----/.test(head);
}

// --- the connection form -----------------------------------------------------------------

export type OverrideRow = { service: AwsOverrideService; region: string; url: string };

export type AwsForm = {
  name: string;
  auth_mode: AwsAuthMode;
  region: string;
  proxy_mode: AwsProxyMode;
  proxy_url: string;
  overrides: OverrideRow[];
  access_key_id: string;
  secret_access_key: string;
  clear_secret: boolean;
  session_token: string;
  clear_session_token: boolean;
  access_key_created_at: string;
  ra_region: string;
  ra_trust_anchor_arn: string;
  ra_profile_arn: string;
  ra_role_arn: string;
  ra_session_seconds: string;
};

export function awsFormFrom(c: AwsConnection | null): AwsForm {
  return {
    name: c?.name ?? '',
    auth_mode: c?.auth_mode ?? 'roles_anywhere',
    region: c?.region ?? DEFAULT_AWS_REGION,
    proxy_mode: c?.proxy_mode ?? 'env',
    proxy_url: c?.proxy_url ?? '',
    overrides: (c?.endpoint_overrides ?? []).map((o) => ({ ...o })),
    access_key_id: c?.access_key_id ?? '',
    secret_access_key: '',
    clear_secret: false,
    session_token: '',
    clear_session_token: false,
    access_key_created_at: c?.access_key_created_at ?? '',
    ra_region: c?.ra_region ?? '',
    ra_trust_anchor_arn: c?.ra_trust_anchor_arn ?? '',
    ra_profile_arn: c?.ra_profile_arn ?? '',
    ra_role_arn: c?.ra_role_arn ?? '',
    ra_session_seconds: String(c?.ra_session_seconds ?? 3600),
  };
}

const orNull = (v: string) => (v.trim() ? v.trim() : null);

/** The request body of POST / PATCH / Test (secrets: omitted keeps, "" clears). */
export function awsInput(f: AwsForm): AwsConnectionInput {
  const body: AwsConnectionInput = {
    name: f.name.trim(),
    auth_mode: f.auth_mode,
    region: f.region.trim(),
    proxy_mode: f.proxy_mode,
    proxy_url: f.proxy_mode === 'custom' ? orNull(f.proxy_url) : null,
    endpoint_overrides: f.overrides
      .filter((o) => o.url.trim() || o.region.trim())
      .map((o): AwsEndpointOverride => ({
        service: o.service,
        region: o.region.trim(),
        url: o.url.trim().replace(/\/+$/, ''),
      })),
    access_key_id: orNull(f.access_key_id),
    access_key_created_at: orNull(f.access_key_created_at),
    ra_region: orNull(f.ra_region),
    ra_trust_anchor_arn: orNull(f.ra_trust_anchor_arn),
    ra_profile_arn: orNull(f.ra_profile_arn),
    ra_role_arn: orNull(f.ra_role_arn),
    ra_session_seconds: Number(f.ra_session_seconds) || 3600,
  };
  if (f.clear_secret) body.secret_access_key = '';
  else if (f.secret_access_key) body.secret_access_key = f.secret_access_key;
  const token = f.session_token.replace(/\s+/g, '');
  if (f.clear_session_token) body.session_token = '';
  else if (token) body.session_token = token;
  return body;
}

/** Stable key of the values a Test covered (a later edit invalidates the session test). */
export function awsKey(id: number | null, f: AwsForm, certVersion = ''): string {
  const { name: _name, ...rest } = awsInput(f);
  void _name;
  return JSON.stringify([id, rest, certVersion]);
}

export type AwsErrKey =
  | 'name'
  | 'region'
  | 'arnTwice'
  | 'arn'
  | 'session'
  | 'accessKey'
  | 'sessionToken'
  | 'proxy'
  | 'url'
  | 'overrideDup'
  | 'overrideRegion';
export type AwsFormErrors = Partial<Record<keyof AwsForm | `override.${number}`, AwsErrKey>>;

export function isHttpsUrl(v: string): boolean {
  try {
    const u = new URL(v);
    return u.protocol === 'https:' && (u.pathname === '/' || u.pathname === '') && !u.search;
  } catch {
    return false;
  }
}

/** Client-side checks mirroring the server's `field_errors` (the server stays authoritative). */
export function awsErrors(f: AwsForm): AwsFormErrors {
  const e: AwsFormErrors = {};
  if (!f.name.trim() || f.name.trim().length > 64) e.name = 'name';
  if (!isRegion(f.region)) e.region = 'region';
  if (f.proxy_mode === 'custom' && !/^https?:\/\/[^/@\s]+:\d{1,5}\/?$/.test(f.proxy_url.trim()))
    e.proxy_url = 'proxy';
  const seen = new Set<string>();
  f.overrides.forEach((o, i) => {
    if (!o.url.trim() && !o.region.trim()) return;
    const k = `override.${i}` as const;
    if (!isRegion(o.region)) e[k] = 'overrideRegion';
    else if (!isHttpsUrl(o.url.trim())) e[k] = 'url';
    const key = `${o.service}|${o.region.trim()}`;
    if (seen.has(key)) e[k] = 'overrideDup';
    seen.add(key);
  });
  if (f.auth_mode === 'roles_anywhere') {
    const arns: [keyof AwsForm, ArnKind][] = [
      ['ra_trust_anchor_arn', 'trust_anchor'],
      ['ra_profile_arn', 'profile'],
      ['ra_role_arn', 'role'],
    ];
    for (const [k, kind] of arns) {
      const v = String(f[k]);
      const err = v.trim() ? arnError(v, kind) : 'invalid';
      if (err) e[k] = err === 'twice' ? 'arnTwice' : 'arn';
    }
    if (f.ra_region.trim() && !isRegion(f.ra_region)) e.ra_region = 'region';
    const n = Number(f.ra_session_seconds);
    if (!Number.isInteger(n) || n < 900 || n > 43200) e.ra_session_seconds = 'session';
  }
  if (f.auth_mode === 'access_key') {
    if (!ACCESS_KEY_RE.test(f.access_key_id.trim())) e.access_key_id = 'accessKey';
    const token = f.session_token.replace(/\s+/g, '');
    if (token && !f.clear_session_token && !SESSION_TOKEN_RE.test(token))
      e.session_token = 'sessionToken';
  }
  return e;
}

/** What a Test needs besides valid fields (shown as a hint; Test still runs and reports). */
export function awsMissing(f: AwsForm, c: AwsConnection | null): string[] {
  const miss: string[] = [];
  if (f.auth_mode === 'roles_anywhere' && !c?.certificate) miss.push('certificate');
  if (
    f.auth_mode === 'access_key' &&
    (f.clear_secret || (!c?.secret_access_key_set && !f.secret_access_key))
  )
    miss.push('secret');
  if (
    f.auth_mode === 'access_key' &&
    f.access_key_id.trim().startsWith('ASIA') &&
    (f.clear_session_token || (!c?.session_token_set && !f.session_token.trim()))
  )
    miss.push('sessionToken');
  return miss;
}

/** Trust anchor, profile and role of one Roles Anywhere setup in different accounts (warning). */
export function arnAccountsDiffer(
  f: Pick<AwsForm, 'ra_trust_anchor_arn' | 'ra_profile_arn' | 'ra_role_arn'>,
): boolean {
  const ids = [f.ra_trust_anchor_arn, f.ra_profile_arn, f.ra_role_arn]
    .map((a) => accountOfArn(a))
    .filter((x): x is string => !!x);
  return new Set(ids).size > 1;
}

// --- consumer access ----------------------------------------------------------------------

export function normalizeAccess(a: Partial<AwsAccess> | null | undefined): AwsAccess {
  const mode: AwsAccessMode = a?.mode ?? 'direct';
  return {
    mode,
    role_arn: mode === 'direct' ? null : (a?.role_arn ?? '').trim() || null,
    external_id: mode === 'assume_role' ? (a?.external_id ?? '').trim() || null : null,
  };
}

export type AccessErrors = {
  mode?: 'raOnly';
  role_arn?: 'arnTwice' | 'arn' | 'required' | 'profileAccount';
  external_id?: 'externalId';
};

/**
 * Consumer access rules (data-model "Consumer access"): the role ARN is required for a hop;
 * `ra_role` needs a Roles Anywhere connection and a role in the profile's account.
 */
export function accessErrors(
  a: AwsAccess,
  authMode: AwsAuthMode | null,
  profileArn?: string | null,
): AccessErrors {
  const e: AccessErrors = {};
  if (a.mode === 'direct') return e;
  const arn = a.role_arn ?? '';
  if (!arn.trim()) e.role_arn = 'required';
  else {
    const err = arnError(arn, 'role');
    if (err) e.role_arn = err === 'twice' ? 'arnTwice' : 'arn';
  }
  if (a.mode === 'assume_role' && a.external_id && !EXTERNAL_ID_RE.test(a.external_id))
    e.external_id = 'externalId';
  if (a.mode === 'ra_role') {
    if (authMode !== null && authMode !== 'roles_anywhere') e.mode = 'raOnly';
    const prof = accountOfArn(profileArn);
    if (!e.role_arn && prof && accountOfArn(arn) !== prof) e.role_arn = 'profileAccount';
  }
  return e;
}

export function accessModesFor(authMode: AwsAuthMode | null): AwsAccessMode[] {
  return authMode === 'roles_anywhere'
    ? ['direct', 'assume_role', 'ra_role']
    : ['direct', 'assume_role'];
}

/**
 * Hint values are AWS names (ARNs, CNs, hosts): wrapped in Unicode isolates so they stay
 * left-to-right inside a Hebrew sentence.
 */
export function isolateValues(values: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(values)) out[k] = `⁨${v}⁩`;
  return out;
}

/** Pretty JSON of a rendered policy (2 spaces, as the AWS console shows it). */
export function policyJson(p: Record<string, unknown>): string {
  return JSON.stringify(p, null, 2);
}

/** The server's fixed `notes` (policies.py) → i18n keys; unknown notes are shown as sent. */
const NOTES: [RegExp, string][] = [
  [/trust anchor ARN is not set/, 'taMissing'],
  [/no certificate uploaded/, 'noCert'],
  [/only for a Roles Anywhere connection/, 'notRa'],
  [/base identity's ARN is unknown/, 'baseUnknown'],
  [/source identity is unknown/, 'noSourceIdentity'],
  [/no ExternalId/, 'noExternalId'],
];
export function noteKey(note: string): string | null {
  return NOTES.find(([re]) => re.test(note))?.[1] ?? null;
}

// --- successful Tests of this page session (module scope: survives switching settings tabs) ---

const sessionTests = new Map<string, { id: string; expires: number }>();

export function rememberAwsTest(key: string, id: string, expiresAt: string | null): void {
  sessionTests.set(key, {
    id,
    expires: expiresAt ? Date.parse(expiresAt) : Date.now() + 30 * 60_000,
  });
}

/** The `test_id` of a successful Test of exactly these values in this page session, if any. */
export function awsTestFor(key: string): string | null {
  const s = sessionTests.get(key);
  if (!s || Date.now() > s.expires) return null;
  return s.id;
}

export function certVersion(
  c: { sha256: string; uploaded_at: string | null; source: string } | null,
): string {
  return c ? `${c.sha256}|${c.uploaded_at ?? ''}|${c.source}` : '';
}
