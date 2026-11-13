// [4] Shared AWS connections (docs/api.md "[4] Shared AWS connections"). Paths and fields are exact.
import { api, buildUrl } from './client';
import { postForm } from './endpoints3b';
import type { CsrRequest } from './types3b';
import type {
  AwsCertInfo,
  AwsConnection,
  AwsConnectionInput,
  AwsCsrInfo,
  AwsPolicies,
  AwsTestRequest,
  AwsTestResult,
} from './types4';

export const qk4 = {
  connections: ['aws-connections'] as const,
  policies: (id: number, roleArn: string, externalId: string) =>
    ['aws-policies', id, roleArn, externalId] as const,
};

const base = (id: number) => `/aws/connections/${id}`;

export const endpoints4 = {
  connections: () => api.get<AwsConnection[]>('/aws/connections'),
  connection: (id: number) => api.get<AwsConnection>(base(id)),
  createConnection: (body: AwsConnectionInput) => api.post<AwsConnection>('/aws/connections', body),
  updateConnection: (id: number, body: AwsConnectionInput & { test_id?: string }) =>
    api.patch<AwsConnection>(base(id), body),
  deleteConnection: (id: number) => api.del(base(id)),
  /** `test_id` is needed while an ON consumer uses the default implicitly (422 test_required) */
  setDefault: (id: number, testId?: string | null) =>
    api.post<AwsConnection[]>(`${base(id)}/default`, testId ? { test_id: testId } : undefined),
  test: (body: AwsTestRequest) => api.post<AwsTestResult>('/aws/connections/test', body),
  policies: (id: number, roleArn?: string | null, externalId?: string | null) =>
    api.get<AwsPolicies>(`${base(id)}/policies`, {
      role_arn: roleArn || undefined,
      external_id: externalId || undefined,
    }),
  uploadCert: (id: number, file: File, key: File | null, password: string) => {
    const fd = new FormData();
    fd.append('file', file);
    if (key) fd.append('key', key);
    if (password) fd.append('password', password);
    return postForm<AwsCertInfo>(`${base(id)}/cert`, fd);
  },
  deleteCert: (id: number) => api.del(`${base(id)}/cert`),
  createCsr: (id: number, body: CsrRequest) => api.post<AwsCsrInfo>(`${base(id)}/cert/csr`, body),
  csrDownloadUrl: (id: number) => buildUrl(`${base(id)}/cert/csr`),
  deleteCsr: (id: number) => api.del(`${base(id)}/cert/csr`),
  copyCert: (id: number, fromId: number) =>
    api.post<AwsConnection>(`${base(id)}/cert/copy`, { from_connection_id: fromId }),
  externalId: () => api.post<{ external_id: string }>('/aws/external-id'),
};
