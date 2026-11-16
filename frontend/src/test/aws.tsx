// [4] Test fixtures of the shared AWS connections UI (Vitest only; never imported by the app).
import { vi } from 'vitest';
import type { AwsCertInfo, AwsConnection, AwsTestResult } from '../api/types';

export const TA =
  'arn:aws:rolesanywhere:us-east-1:111111111111:trust-anchor/0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0';
export const PROFILE =
  'arn:aws:rolesanywhere:us-east-1:111111111111:profile/0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f1';
export const BASE_ROLE = 'arn:aws:iam::111111111111:role/netmon-base';
export const BEDROCK_ROLE = 'arn:aws:iam::222222222222:role/netmon-bedrock';

export function mkCert(extra: Partial<AwsCertInfo> = {}): AwsCertInfo {
  return {
    source: 'uploaded',
    subject: 'CN=netmonitor.example.net,O=Example',
    issuer: 'CN=Example Issuing CA,DC=example,DC=net',
    serial_hex: '1234',
    serial_decimal: '4660',
    not_before: '2026-01-01T00:00:00Z',
    not_after: '2027-12-31T00:00:00Z',
    key_type: 'rsa',
    key_bits: 2048,
    sha256: 'ab'.repeat(32),
    chain: [],
    uploaded_at: '2026-11-01T10:00:00Z',
    key_unreadable: false,
    subject_cn: 'netmonitor.example.net',
    issuer_cn: 'Example Issuing CA',
    source_identity: 'CN=netmonitor.example.net',
    warnings: [],
    ...extra,
  };
}

export function mkConn(
  id: number,
  name: string,
  extra: Partial<AwsConnection> = {},
): AwsConnection {
  return {
    id,
    name,
    is_default: false,
    auth_mode: 'roles_anywhere',
    region: 'us-east-1',
    proxy_mode: 'env',
    proxy_url: null,
    endpoint_overrides: [],
    access_key_id: null,
    secret_access_key: null,
    secret_access_key_set: false,
    secret_access_key_unreadable: false,
    session_token: null,
    session_token_set: false,
    session_token_unreadable: false,
    access_key_created_at: null,
    ra_region: null,
    ra_trust_anchor_arn: TA,
    ra_profile_arn: PROFILE,
    ra_role_arn: BASE_ROLE,
    ra_session_seconds: 3600,
    certificate: mkCert(),
    certificate_host_files: false,
    csr: null,
    used_by: [],
    chains: [],
    last_test: null,
    warnings: [],
    unresolved_hosts: [],
    created_at: '2026-11-01T10:00:00Z',
    updated_at: '2026-11-01T10:00:00Z',
    ...extra,
  };
}

export const okChain: AwsTestResult = {
  ok: true,
  test_id: 't-ok',
  expires_at: new Date(Date.now() + 600_000).toISOString(),
  steps: [
    {
      step: 'config',
      hop: null,
      ok: true,
      detail: 'auth roles_anywhere, region us-east-1',
      ms: 1,
      error_code: null,
      hints: [],
    },
    {
      step: 'credentials',
      hop: 0,
      ok: true,
      detail: 'source roles_anywhere',
      ms: 20,
      error_code: null,
      hints: [],
    },
    {
      step: 'identity',
      hop: 0,
      ok: true,
      detail: 'account 111111111111',
      ms: 9,
      error_code: null,
      hints: [],
    },
  ],
  warnings: [],
  error: null,
  identity: { account: '111111111111', arn: `${BASE_ROLE}/1234` },
  hop_identity: null,
};

export const raFailure: AwsTestResult = {
  ok: false,
  test_id: null,
  expires_at: null,
  steps: [
    {
      step: 'config',
      hop: null,
      ok: true,
      detail: 'auth roles_anywhere, region us-east-1',
      ms: 1,
      error_code: null,
      hints: [],
    },
    {
      step: 'credentials',
      hop: 0,
      ok: false,
      detail: 'AccessDeniedException: Unable to assume role for arn',
      ms: 30,
      error_code: 'rolesanywhere_failed',
      hints: [
        { code: 'ra_trust_policy', text: 'x', values: { trust_anchor_arn: TA } },
        { code: 'ra_subject_cn', text: 'x', values: { cert_cn: 'netmonitor.example.net' } },
        { code: 'ra_issuer_cn', text: 'x', values: { issuer_cn: 'Example Issuing CA' } },
        {
          code: 'ra_profile_role',
          text: 'x',
          values: { profile_arn: PROFILE, role_arn: BASE_ROLE },
        },
      ],
    },
  ],
  warnings: [],
  error: { code: 'rolesanywhere_failed', message: 'Unable to assume role' },
  identity: null,
  hop_identity: null,
};

export type Call = { method: string; url: string; body: unknown };
export type Route = (
  method: string,
  url: string,
  body: unknown,
) => { status: number; body?: unknown } | undefined;

/** Stubs fetch; JSON bodies parsed, FormData kept as is (certificate uploads). */
export function mockAwsFetch(route: Route): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = (init?.method ?? 'GET').toUpperCase();
      const raw = init?.body;
      const body = raw instanceof FormData ? raw : raw ? JSON.parse(String(raw)) : undefined;
      calls.push({ method, url, body });
      const r = route(method, url, body) ?? {
        status: 404,
        body: { error: { code: 'not_found', message: 'nf', details: null } },
      };
      return new Response(r.body === undefined ? null : JSON.stringify(r.body), {
        status: r.status,
        headers: { 'Content-Type': 'application/json' },
      });
    }),
  );
  return calls;
}
