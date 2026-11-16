// [4] Pure helpers of the shared AWS connections UI + the locale coverage of its codes.
import { describe, expect, it } from 'vitest';
import en from '../i18n/locales/en.json';
import he from '../i18n/locales/he.json';
import {
  AWS_ACCESS_MODES,
  AWS_AUTH_MODES,
  AWS_CONNECTION_WARNINGS,
  AWS_HINT_CODES,
  AWS_PROXY_MODES,
  AWS_TEST_STEPS,
} from '../api/types';
import {
  accessErrors,
  accessModesFor,
  arnAccountsDiffer,
  arnDoubled,
  arnError,
  awsErrors,
  awsFormFrom,
  awsInput,
  awsKey,
  awsMissing,
  certreqCommand,
  cnSourceIdentitySafe,
  isolateValues,
  looksLikeDisplayName,
  looksLikePrivateKey,
  normalizeAccess,
  noteKey,
  sourceIdentityFor,
  type AwsForm,
} from './aws';
import { BASE_ROLE, BEDROCK_ROLE, mkConn, PROFILE, TA } from '../test/aws';

type Tree = { [k: string]: string | Tree };
function flatten(obj: Tree, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (typeof v === 'string') out[key] = v;
    else Object.assign(out, flatten(v, key));
  }
  return out;
}

describe('[4] ARNs: the doubled trust anchor ARN (owner mistake 1)', () => {
  it('refuses an ARN that contains arn: twice', () => {
    const doubled = `arn:aws:rolesanywhere:us-east-1:111111111111:trust-anchor/${TA}`;
    expect(arnDoubled(doubled)).toBe(true);
    expect(arnError(doubled, 'trust_anchor')).toBe('twice');
    expect(arnError(TA, 'trust_anchor')).toBeNull();
    expect(arnError(PROFILE, 'profile')).toBeNull();
    expect(arnError(TA, 'profile')).toBe('invalid');
    expect(arnError(BASE_ROLE, 'role')).toBeNull();
    expect(arnError(`${BASE_ROLE}${BASE_ROLE}`, 'role')).toBe('twice');
    expect(arnError('', 'role')).toBeNull();
  });
  it('accounts of one Roles Anywhere setup', () => {
    const f = { ra_trust_anchor_arn: TA, ra_profile_arn: PROFILE, ra_role_arn: BASE_ROLE };
    expect(arnAccountsDiffer(f)).toBe(false);
    expect(arnAccountsDiffer({ ...f, ra_role_arn: BEDROCK_ROLE })).toBe(true);
  });
});

describe('[4] certificate helpers (owner mistakes 2-4)', () => {
  it('source identity of a CN (fact 3) and the CN check', () => {
    expect(sourceIdentityFor('netmonitor.example.net')).toBe('CN=netmonitor.example.net');
    expect(sourceIdentityFor('a'.repeat(62))).toBe('a'.repeat(62));
    expect(sourceIdentityFor(null)).toBeNull();
    expect(cnSourceIdentitySafe('netmonitor.example.net')).toBe(true);
    expect(cnSourceIdentitySafe('NetMonitor Bedrock')).toBe(false);
  });
  it('certreq with the template name; a display name (spaces) is recognised', () => {
    expect(certreqCommand('NetMonClientAuth')).toBe(
      'certreq -submit -attrib "CertificateTemplate:NetMonClientAuth" netmonitor.csr netmonitor.cer',
    );
    expect(certreqCommand('')).toContain('CertificateTemplate:<TemplateName>');
    expect(looksLikeDisplayName('NetMon Client Auth')).toBe(true);
    expect(looksLikeDisplayName('NetMonClientAuth')).toBe(false);
  });
  it('a private key is not an issued certificate', () => {
    expect(looksLikePrivateKey('netmonitor.key')).toBe(true);
    expect(looksLikePrivateKey('bundle.pfx')).toBe(true);
    expect(looksLikePrivateKey('netmonitor.cer')).toBe(false);
    expect(looksLikePrivateKey('x.pem', '-----BEGIN PRIVATE KEY-----\nMII')).toBe(true);
    expect(looksLikePrivateKey('x.pem', '-----BEGIN RSA PRIVATE KEY-----')).toBe(true);
    expect(looksLikePrivateKey('x.pem', '-----BEGIN CERTIFICATE-----')).toBe(false);
  });
});

describe('[4] connection form', () => {
  const base: AwsForm = {
    ...awsFormFrom(null),
    name: 'Network account',
    ra_trust_anchor_arn: TA,
    ra_profile_arn: PROFILE,
    ra_role_arn: BASE_ROLE,
  };
  it('defaults: Roles Anywhere, us-east-1, proxy env, 3600 s', () => {
    const f = awsFormFrom(null);
    expect(f).toMatchObject({
      auth_mode: 'roles_anywhere',
      region: 'us-east-1',
      proxy_mode: 'env',
      ra_session_seconds: '3600',
    });
  });
  it('validates per mode', () => {
    expect(awsErrors(base)).toEqual({});
    expect(awsErrors({ ...base, name: ' ' }).name).toBe('name');
    expect(awsErrors({ ...base, ra_trust_anchor_arn: `x${TA}${TA}` }).ra_trust_anchor_arn).toBe(
      'arnTwice',
    );
    expect(awsErrors({ ...base, ra_role_arn: 'role' }).ra_role_arn).toBe('arn');
    expect(awsErrors({ ...base, ra_session_seconds: '100' }).ra_session_seconds).toBe('session');
    const ak: AwsForm = { ...base, auth_mode: 'access_key', access_key_id: 'AKIAABCDEFGHIJKLMNOP' };
    expect(awsErrors(ak)).toEqual({});
    expect(awsErrors({ ...ak, access_key_id: 'nope' }).access_key_id).toBe('accessKey');
    expect(awsErrors({ ...ak, session_token: 'bad<token>' }).session_token).toBe('sessionToken');
    expect(awsErrors({ ...base, auth_mode: 'default_chain', ra_role_arn: '' })).toEqual({});
    expect(awsErrors({ ...base, proxy_mode: 'custom', proxy_url: 'proxy' }).proxy_url).toBe(
      'proxy',
    );
  });
  it('endpoint override rows: https only, one per (service, region)', () => {
    const row = {
      service: 'sts' as const,
      region: 'us-east-1',
      url: 'https://vpce-1.sts.us-east-1.vpce.amazonaws.com',
    };
    expect(awsErrors({ ...base, overrides: [row] })).toEqual({});
    expect(awsErrors({ ...base, overrides: [{ ...row, url: 'http://x' }] })['override.0']).toBe(
      'url',
    );
    expect(awsErrors({ ...base, overrides: [{ ...row, region: 'eu' }] })['override.0']).toBe(
      'overrideRegion',
    );
    expect(awsErrors({ ...base, overrides: [row, row] })['override.1']).toBe('overrideDup');
    // an empty row is ignored (not sent)
    expect(
      awsInput({ ...base, overrides: [{ service: 'sts', region: '', url: '' }] })
        .endpoint_overrides,
    ).toEqual([]);
    expect(
      awsInput({ ...base, overrides: [{ ...row, url: `${row.url}/` }] }).endpoint_overrides,
    ).toEqual([row]);
  });
  it('secrets are write-only: omitted keeps, "" clears; the token loses its whitespace', () => {
    expect('secret_access_key' in awsInput(base)).toBe(false);
    expect(awsInput({ ...base, secret_access_key: 's' }).secret_access_key).toBe('s');
    expect(
      awsInput({ ...base, secret_access_key: 's', clear_secret: true }).secret_access_key,
    ).toBe('');
    expect(awsInput({ ...base, session_token: ' IQoJ\nb3Jp== ' }).session_token).toBe('IQoJb3Jp==');
    expect(
      awsInput({ ...base, proxy_mode: 'direct', proxy_url: 'http://p:1' }).proxy_url,
    ).toBeNull();
  });
  it('the test key covers every credential field but not the name', () => {
    expect(awsKey(1, base)).toBe(awsKey(1, { ...base, name: 'Renamed' }));
    expect(awsKey(1, base)).not.toBe(awsKey(1, { ...base, region: 'eu-central-1' }));
    expect(awsKey(1, base)).not.toBe(awsKey(2, base));
    expect(awsKey(1, base, 'a')).not.toBe(awsKey(1, base, 'b'));
  });
  it('missing values', () => {
    expect(awsMissing(base, null)).toEqual(['certificate']);
    expect(awsMissing(base, mkConn(1, 'x'))).toEqual([]);
    const ak: AwsForm = { ...base, auth_mode: 'access_key', access_key_id: 'ASIAABCDEFGHIJKLMNOP' };
    expect(awsMissing(ak, null)).toEqual(['secret', 'sessionToken']);
  });
});

describe('[4] consumer access', () => {
  it('rules per mode', () => {
    expect(accessErrors(normalizeAccess({ mode: 'direct', role_arn: 'junk' }), null)).toEqual({});
    const hop = normalizeAccess({
      mode: 'assume_role',
      role_arn: BEDROCK_ROLE,
      external_id: 'AbC123',
    });
    expect(accessErrors(hop, 'access_key')).toEqual({});
    expect(accessErrors({ ...hop, role_arn: null }, null).role_arn).toBe('required');
    expect(accessErrors({ ...hop, external_id: 'x' }, null).external_id).toBe('externalId');
    const ra = normalizeAccess({ mode: 'ra_role', role_arn: BEDROCK_ROLE, external_id: 'ignored' });
    expect(ra.external_id).toBeNull();
    expect(accessErrors(ra, 'access_key').mode).toBe('raOnly');
    expect(accessErrors(ra, 'roles_anywhere', PROFILE).role_arn).toBe('profileAccount');
    expect(accessModesFor('roles_anywhere')).toEqual(['direct', 'assume_role', 'ra_role']);
    expect(accessModesFor('access_key')).toEqual(['direct', 'assume_role']);
  });
  it('hint values are isolated (LTR) inside Hebrew text', () => {
    const [fsi, pdi] = [String.fromCharCode(0x2068), String.fromCharCode(0x2069)];
    const v = 'arn:aws:iam::111111111111:role/netmon';
    expect(isolateValues({ a: v })).toEqual({ a: fsi + v + pdi });
  });
  it('policy notes map to translated texts', () => {
    expect(noteKey('base_trust_policy: the trust anchor ARN is not set')).toBe('taMissing');
    expect(
      noteKey(
        "hop_trust_policy: the base identity's ARN is unknown; run the connection Test first",
      ),
    ).toBe('baseUnknown');
    expect(noteKey('something new')).toBeNull();
  });
});

describe('[4] locales: every code of the AWS UI in en and he', () => {
  const enF = flatten(en as Tree);
  const heF = flatten(he as Tree);
  it('hint codes, warnings, steps, modes', () => {
    const keys = [
      ...AWS_HINT_CODES.map((c) => `aws.hint.${c}`),
      ...AWS_CONNECTION_WARNINGS.map((w) => `aws.warning.${w}`),
      ...AWS_TEST_STEPS.map((s) => `aws.test.step.${s}`),
      ...AWS_AUTH_MODES.map((m) => `aws.auth.${m}`),
      ...AWS_PROXY_MODES.map((m) => `aws.form.proxy.${m}`),
      ...AWS_ACCESS_MODES.map((m) => `aws.access.mode.${m}`),
      ...[
        'aws_assume_role_denied',
        'aws_assume_role_invalid',
        'aws_connection_unconfigured',
        'test_required',
      ].map((c) => `errors.${c}`),
      ...['identity', 'hop', 'hop_identity'].map((s) => `assistant.test.step.${s}`),
      'settings.tab.aws',
    ];
    for (const k of keys) {
      expect(enF[k], `en ${k}`).toBeTypeOf('string');
      expect(heF[k], `he ${k}`).toBeTypeOf('string');
    }
  });
  it('the aws namespace has the same keys in both languages (he plural forms aside)', () => {
    const strip = (k: string) => k.replace(/_(one|two|other)$/, '');
    const enK = new Set(
      Object.keys(enF)
        .filter((k) => k.startsWith('aws.'))
        .map(strip),
    );
    const heK = new Set(
      Object.keys(heF)
        .filter((k) => k.startsWith('aws.'))
        .map(strip),
    );
    expect([...enK].filter((k) => !heK.has(k))).toEqual([]);
    expect([...heK].filter((k) => !enK.has(k))).toEqual([]);
  });
  it('hint placeholders are the same in en and he', () => {
    const ph = (s: string) => [...s.matchAll(/\{\{(\w+)\}\}/g)].map((m) => m[1]).sort();
    for (const c of AWS_HINT_CODES)
      expect(ph(heF[`aws.hint.${c}`]), c).toEqual(ph(enF[`aws.hint.${c}`]));
  });
  it('the Hebrew texts are Hebrew (AWS names aside)', () => {
    for (const [k, v] of Object.entries(heF)) {
      if (!k.startsWith('aws.') || k.startsWith('aws.test.step') || v.length < 25) continue;
      expect(/[֐-׿]/.test(v), `${k}: ${v}`).toBe(true);
    }
  });
});
