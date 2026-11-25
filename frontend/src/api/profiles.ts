// [profiles] Connection profiles (api.md § Connection profiles). Every endpoint is admin-only.
import { api } from './client';
import type {
  ConnectionProfile,
  CredentialKind,
  Device,
  Group,
  Page,
  ProfileTestResult,
  ProfileUsage,
  SnmpCredentialsInput,
} from './types';
import type { SshCredentialsInput } from './types3';

export type ProfileCredentialsInput = SnmpCredentialsInput | SshCredentialsInput;

export type ConnectionProfileInput = {
  kind: CredentialKind;
  name: string;
  description?: string;
  credentials: ProfileCredentialsInput;
};

/** PATCH: a subset; `kind` only with the current value. */
export type ConnectionProfilePatch = {
  name?: string;
  description?: string;
  credentials?: ProfileCredentialsInput;
};

/** Exactly one of device_id / host. */
export type ProfileTestTarget = { device_id: number } | { host: string };

export type ProfileTestInput = ProfileTestTarget & {
  /** partial, merged over the stored profile; nothing is saved */
  credentials?: ProfileCredentialsInput;
  /** SSH only */
  driver?: string | null;
};

export type ProfileTestNewInput = ProfileTestTarget & {
  kind: CredentialKind;
  credentials: ProfileCredentialsInput;
  driver?: string | null;
};

export type CopyFromProfileInput = { profile_id?: number };

export const qkp = {
  /** prefix of every profile list query */
  all: ['connectionProfiles'] as const,
  list: (params?: { kind?: CredentialKind; q?: string }) =>
    ['connectionProfiles', params ?? {}] as const,
  one: (id: number) => ['connectionProfile', id] as const,
  usage: (id: number) => ['connectionProfileUsage', id] as const,
};

export const profilesApi = {
  list: (params?: { kind?: CredentialKind; q?: string; page?: number; page_size?: number }) =>
    api.get<Page<ConnectionProfile>>('/connection-profiles', { page_size: 1000, ...params }),
  get: (id: number) => api.get<ConnectionProfile>(`/connection-profiles/${id}`),
  create: (body: ConnectionProfileInput) =>
    api.post<ConnectionProfile>('/connection-profiles', body),
  update: (id: number, body: ConnectionProfilePatch) =>
    api.patch<ConnectionProfile>(`/connection-profiles/${id}`, body),
  remove: (id: number) => api.del(`/connection-profiles/${id}`),
  usage: (id: number) => api.get<ProfileUsage>(`/connection-profiles/${id}/usage`),
  test: (id: number, body: ProfileTestInput) =>
    api.post<ProfileTestResult>(`/connection-profiles/${id}/test`, body),
  testNew: (body: ProfileTestNewInput) =>
    api.post<ProfileTestResult>('/connection-profiles/test', body),
  copyToGroup: (groupId: number, kind: CredentialKind, body: CopyFromProfileInput = {}) =>
    api.post<Group>(`/groups/${groupId}/credentials/${kind}/copy-from-profile`, body),
  copyToDevice: (deviceId: number, kind: CredentialKind, body: CopyFromProfileInput = {}) =>
    api.post<Device>(`/devices/${deviceId}/credentials/${kind}/copy-from-profile`, body),
};
