// [4] Hand-written mirror of docs/api.md "[4] Shared AWS connections" (PR 4-0a). Field names
// are exact (checked against backend/netmon/api/aws_connections.py and schemas/aws.py).
import type { CertInfo } from './types3b';

export type AwsAuthMode = 'roles_anywhere' | 'access_key' | 'default_chain';
export const AWS_AUTH_MODES: AwsAuthMode[] = ['roles_anywhere', 'access_key', 'default_chain'];
export type AwsProxyMode = 'env' | 'direct' | 'custom';
export const AWS_PROXY_MODES: AwsProxyMode[] = ['env', 'direct', 'custom'];

export type AwsAccessMode = 'direct' | 'assume_role' | 'ra_role';
export const AWS_ACCESS_MODES: AwsAccessMode[] = ['direct', 'assume_role', 'ra_role'];
export type AwsAccess = {
  mode: AwsAccessMode;
  role_arn: string | null;
  external_id: string | null;
};

/** 4-0a services with an endpoint override (later PRs add more). */
export const AWS_OVERRIDE_SERVICES = ['sts', 'rolesanywhere'] as const;
export type AwsOverrideService = (typeof AWS_OVERRIDE_SERVICES)[number];
export type AwsEndpointOverride = { service: AwsOverrideService; region: string; url: string };

export type AwsCertInfo = CertInfo & {
  subject_cn: string | null;
  issuer_cn: string | null;
  source_identity: string;
  warnings?: string[];
};
export type AwsCsrInfo = {
  subject: string | null;
  created_at: string | null;
  pem: string | null;
  key_unreadable: boolean;
  warnings?: string[];
};

export type AwsChainHop = {
  hop: 0 | 1;
  kind: 'roles_anywhere' | 'access_key' | 'default_chain' | 'assume_role' | 'ra_role';
  arn: string | null;
  account: string | null;
  expires_at: string | null;
  last_refresh: string | null;
};

export const AWS_CONNECTION_WARNINGS = [
  'access_key_old',
  'temporary_credentials',
  'arn_accounts_differ',
  'cert_expiring',
  'key_file_mode',
  'cn_not_source_identity_safe',
  'vpce_via_proxy',
  'endpoint_unresolved',
] as const;

export type AwsUsedBy = {
  kind: 'assistant';
  id: number | null;
  name: string;
  access_mode: AwsAccessMode;
};

export type AwsLastTest = {
  ok: boolean;
  at: string;
  by: string;
  error_code: string | null;
  account: string | null;
  arn: string | null;
};

export type AwsConnection = {
  id: number;
  name: string;
  is_default: boolean;
  auth_mode: AwsAuthMode;
  region: string;
  proxy_mode: AwsProxyMode;
  proxy_url: string | null;
  endpoint_overrides: AwsEndpointOverride[];
  access_key_id: string | null;
  secret_access_key: null;
  secret_access_key_set: boolean;
  secret_access_key_unreadable: boolean;
  session_token: null;
  session_token_set: boolean;
  session_token_unreadable: boolean;
  access_key_created_at: string | null;
  ra_region: string | null;
  ra_trust_anchor_arn: string | null;
  ra_profile_arn: string | null;
  ra_role_arn: string | null;
  ra_session_seconds: number;
  certificate: AwsCertInfo | null;
  certificate_host_files: boolean;
  csr: AwsCsrInfo | null;
  used_by: AwsUsedBy[];
  chains: {
    consumer: string;
    hops: AwsChainHop[];
    state: 'ok' | 'error' | 'idle';
    error_code: string | null;
  }[];
  last_test: AwsLastTest | null;
  warnings: string[];
  /** hosts of the proxy / overrides that did not resolve at the last read (warning only) */
  unresolved_hosts?: string[];
  created_at: string | null;
  updated_at: string | null;
};

/** Writable fields (POST: name, auth_mode, region required; PATCH: partial + `test_id`). */
export type AwsConnectionInput = {
  name?: string;
  auth_mode?: AwsAuthMode;
  region?: string;
  proxy_mode?: AwsProxyMode;
  proxy_url?: string | null;
  endpoint_overrides?: AwsEndpointOverride[];
  access_key_id?: string | null;
  access_key_created_at?: string | null;
  ra_region?: string | null;
  ra_trust_anchor_arn?: string | null;
  ra_profile_arn?: string | null;
  ra_role_arn?: string | null;
  ra_session_seconds?: number;
  /** null/omitted keeps, "" clears, a string replaces */
  secret_access_key?: string | null;
  session_token?: string | null;
};

export type AwsHint = { code: string; text: string; values: Record<string, string> };
export const AWS_HINT_CODES = [
  'ra_trust_policy',
  'ra_subject_cn',
  'ra_issuer_cn',
  'ra_profile_role',
  'ra_untrusted_cert',
  'ra_disabled',
  'ra_revoked',
  'set_source_identity',
  'tag_session',
  'caller_policy',
  'trust_policy',
  'external_id',
  'scp',
  'sts_region',
  'role_arn_invalid',
  'max_session',
  'vpc_endpoint_or_proxy',
  'cn_not_source_identity_safe',
  'model_access',
] as const;

export const AWS_TEST_STEPS = ['config', 'credentials', 'identity', 'hop', 'hop_identity'] as const;
export type AwsTestStep = {
  step: string;
  hop: 0 | 1 | null;
  ok: boolean;
  detail: string;
  ms: number;
  error_code: string | null;
  hints: AwsHint[];
};
export type AwsIdentity = { account: string | null; arn: string | null; user_id?: string | null };
export type AwsTestResult = {
  ok: boolean;
  test_id: string | null;
  expires_at: string | null;
  steps: AwsTestStep[];
  warnings: string[];
  error: { code: string; message: string } | null;
  identity: AwsIdentity | null;
  hop_identity: AwsIdentity | null;
};
export type AwsTestRequest = {
  connection_id: number | null;
  connection: AwsConnectionInput | null;
  access: AwsAccess | null;
};

export type AwsPolicies = {
  base_trust_policy: Record<string, unknown> | null;
  hop_caller_policy: Record<string, unknown> | null;
  hop_trust_policy: Record<string, unknown> | null;
  notes: string[];
};

/** `GET /api/assistant/settings` → `connection` (read-only). */
export type AssistantConnectionRef = {
  id: number | null;
  name: string | null;
  auth_mode: AwsAuthMode | null;
  is_default: boolean;
  account: string | null;
  arn: string | null;
  state: 'ok' | 'error' | 'idle';
};

/** `GET /api/assistant/status` → `aws`. */
export type AssistantAwsStatus = {
  connection_id: number | null;
  connection_name: string | null;
  access_mode: AwsAccessMode;
  hops: AwsChainHop[];
  last_error: { code: string; message: string | null; at: string | null } | null;
};
