// [premium] Home network map and port device types (backend branch premium-api). Every call
// degrades gracefully against an older server: a 404 (endpoint absent) resolves to null.
import { api, isApiError } from './client';

export type MapStatus = 'up' | 'warning' | 'down' | 'unreachable' | 'paused' | 'unknown';
export type MapLinkStatus = 'up' | 'down' | 'warning' | 'unknown';

export type OverviewMapNode = {
  id: string;
  kind: 'internet' | 'device' | 'group';
  role: 'internet' | 'firewall' | 'core' | 'group' | null;
  name: string;
  subtitle: string;
  status: MapStatus;
  problem: string | null;
  counts: { devices: number; sensors: number; down: number; warning: number; unreachable: number };
  ref: { type: 'device' | 'group'; id: number } | null;
  parent_id: string | null;
};

export type OverviewMapLink = { from: string; to: string; status: MapLinkStatus };

export type OverviewMap = {
  scope: { group_id: number | null; name: string };
  nodes: OverviewMapNode[];
  links: OverviewMapLink[];
  configured: boolean;
};

export type HomeMapMode = 'auto' | 'backbone';
export type HomeMapSettings = {
  mode: HomeMapMode;
  show_internet: boolean;
  backbone_device_ids: number[];
  max_groups: number;
};

export type { ConnectedKind as DeviceKind, ConnectedKindSource } from './types';
export { CONNECTED_KINDS as DEVICE_KINDS } from './types';
import type { ConnectedKind as DeviceKind } from './types';

async function orNull<T>(p: Promise<T>): Promise<T | null> {
  try {
    return await p;
  } catch (e) {
    if (isApiError(e) && (e.status === 404 || e.status === 405)) return null;
    throw e;
  }
}

export const qkPremium = {
  overviewMap: (groupId: number | null) => ['overview-map', groupId] as const,
  homeMap: ['settings', 'home-map'] as const,
};

export const premium = {
  /** null when the server has no map endpoint yet (the home then shows counters and lists only) */
  overviewMap: (groupId: number | null) =>
    orNull(
      api.get<OverviewMap>('/overview/map', groupId === null ? undefined : { group_id: groupId }),
    ),
  homeMap: () => orNull(api.get<HomeMapSettings>('/settings/home-map')),
  setHomeMap: (body: HomeMapSettings) => api.put<HomeMapSettings>('/settings/home-map', body),
  /** Admin override of the device type behind a port; null = automatic */
  setPortKind: (deviceId: number, portKey: string | number, kind: DeviceKind | null) =>
    api.put<unknown>(
      `/devices/${deviceId}/ports/${encodeURIComponent(String(portKey))}/connected-kind`,
      { kind },
    ),
};
