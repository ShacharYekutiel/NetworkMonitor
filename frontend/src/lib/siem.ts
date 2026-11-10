// [2b-3] SIEM forwarding targets (data-model § SIEM forwarding target). Pure.
import type { SiemProtocol, SiemTarget } from '../api/types';
import { isIpOrCidr } from './logs';

export const MAX_TARGETS = 4;
export const DEFAULT_PORT: Record<SiemProtocol, number> = { udp: 514, tcp: 514, tls: 6514 };

export function newTarget(): SiemTarget {
  return {
    name: '',
    enabled: true,
    host: '',
    port: 6514,
    protocol: 'tls',
    tls_verify: true,
    kinds: ['syslog', 'trap'],
    min_severity: 7,
    sources: [],
    include_unknown: false,
  };
}

export type SiemErrors = Partial<Record<'name' | 'host' | 'port' | 'kinds' | 'sources', string>>;

export function validateTarget(t: SiemTarget, sourcesText: string, portText: string): SiemErrors {
  const e: SiemErrors = {};
  if (!t.name.trim()) e.name = 'validation.required';
  if (!t.host.trim() || /\s/.test(t.host.trim())) e.host = 'validation.required';
  const p = Number(portText);
  if (!/^\d+$/.test(portText.trim()) || p < 1 || p > 65535) e.port = 'siem.err.port';
  if (!t.kinds.length) e.kinds = 'siem.err.kinds';
  const src = sourcesText.split(/[\s,;]+/).filter(Boolean);
  if (src.length > 64 || src.some((s) => !isIpOrCidr(s))) e.sources = 'siem.err.sources';
  return e;
}
