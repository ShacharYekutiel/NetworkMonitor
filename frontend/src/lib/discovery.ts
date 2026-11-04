import type {
  DeviceClass,
  DiscoveryFinished,
  DiscoveryProgress,
  DiscoveryResult,
  DiscoveryScan,
  ExtraV3Input,
  Page,
  ScanOptions,
  ScanOptionsInput,
  SnmpAuthProtocol,
  SnmpPrivProtocol,
  SnmpSecurityLevel,
} from '../api/types';
import { parseIpv4, parseIpv6 } from './targets';

// ---------------------------------------------------------------------------
// Live reducers (WS discovery.* messages -> cached scan / results)

/** Applies a `discovery.progress` / `discovery.finished` message to a cached scan. */
export function applyScanProgress(
  scan: DiscoveryScan | undefined,
  d: DiscoveryProgress | DiscoveryFinished,
  ts?: string,
): DiscoveryScan | undefined {
  if (!scan || scan.id !== d.scan_id) return scan;
  // A late progress frame must not resurrect a scan we already know has finished.
  if (scan.status !== 'running' && d.status === 'running') return scan;
  const next: DiscoveryScan = {
    ...scan,
    status: d.status,
    total_hosts: d.total_hosts,
    scanned_hosts: Math.max(scan.scanned_hosts, d.scanned_hosts),
    found_hosts: Math.max(scan.found_hosts, d.found_hosts),
    new_hosts: d.new_hosts,
  };
  if ('error' in d) {
    next.error = d.error;
    if (d.status !== 'running' && !next.finished_at) next.finished_at = ts ?? null;
  }
  return next;
}

/** Patches the scan inside cached scan list pages. */
export function applyScanProgressToPage(
  page: Page<DiscoveryScan> | undefined,
  d: DiscoveryProgress | DiscoveryFinished,
  ts?: string,
): Page<DiscoveryScan> | undefined {
  if (!page || !page.items.some((s) => s.id === d.scan_id)) return page;
  return {
    ...page,
    items: page.items.map((s) => (s.id === d.scan_id ? applyScanProgress(s, d, ts)! : s)),
  };
}

/** Sort key per PostgreSQL inet order: IPv4 before IPv6, numerically. */
export function compareIp(a: string, b: string): number {
  const ka = ipSortKey(a);
  const kb = ipSortKey(b);
  for (let i = 0; i < Math.max(ka.length, kb.length); i++) {
    const d = (ka[i] ?? 0) - (kb[i] ?? 0);
    if (d) return d;
  }
  return a.localeCompare(b);
}

function ipSortKey(ip: string): number[] {
  const v4 = parseIpv4(ip);
  if (v4 !== null) return [4, v4];
  const v6 = parseIpv6(ip);
  if (v6) return [6, ...v6];
  return [9];
}

/** Inserts or replaces a result (by id, then by ip), keeping the list in ip order. */
export function mergeResult(list: DiscoveryResult[], r: DiscoveryResult): DiscoveryResult[] {
  const i = list.findIndex((x) => x.id === r.id || (x.scan_id === r.scan_id && x.ip === r.ip));
  if (i >= 0) {
    const copy = list.slice();
    copy[i] = r;
    return copy;
  }
  const out = [...list, r];
  out.sort((a, b) => compareIp(a.ip, b.ip));
  return out;
}

export function progressPct(scan: Pick<DiscoveryScan, 'total_hosts' | 'scanned_hosts'>): number {
  if (!scan.total_hosts) return 0;
  return Math.min(100, Math.round((scan.scanned_hosts / scan.total_hosts) * 100));
}

// ---------------------------------------------------------------------------
// Result filters (client-side so that live rows merge into the same list)

export type ResultFilters = {
  onlyNew: boolean;
  hideExisting: boolean;
  classes: DeviceClass[];
  q: string;
};

export const NO_FILTERS: ResultFilters = {
  onlyNew: false,
  hideExisting: false,
  classes: [],
  q: '',
};

export function isExisting(r: DiscoveryResult): boolean {
  return r.existing_device_id !== null || r.imported_device_id !== null;
}

/** Mirrors the server's filters: only_new, hide_existing, device_class, q (ip / hostname / sys_name). */
export function filterResults(list: DiscoveryResult[], f: ResultFilters): DiscoveryResult[] {
  const q = f.q.trim().toLowerCase();
  return list.filter((r) => {
    if (f.onlyNew && !r.is_new) return false;
    if (f.hideExisting && isExisting(r)) return false;
    if (f.classes.length && !f.classes.includes(r.device_class)) return false;
    if (q && ![r.ip, r.hostname, r.sys_name].some((x) => x?.toLowerCase().includes(q)))
      return false;
    return true;
  });
}

/** Display name proposed for a found host (same order as the importer). */
export function resultDisplayName(r: DiscoveryResult): string {
  return (r.sys_name || r.hostname || r.ip).trim().slice(0, 128);
}

// ---------------------------------------------------------------------------
// Scan options form

export const DEFAULT_TCP_PORTS = [22, 23, 53, 80, 443, 445, 631, 3389, 8080, 8443, 9100];

export type ExtraV3Form = {
  v3_username: string;
  v3_security_level: SnmpSecurityLevel;
  v3_auth_protocol: SnmpAuthProtocol;
  v3_auth_key: string;
  v3_priv_protocol: SnmpPrivProtocol;
  v3_priv_key: string;
};

export type ScanOptionsForm = {
  ping: boolean;
  ping_timeout_ms: string;
  snmp: boolean;
  snmp_port: string;
  snmp_timeout_s: string;
  /** One community per line. Write-only: empty + `replaceSecrets=false` keeps the stored list. */
  extra_communities: string;
  extra_v3: ExtraV3Form[];
  /** Counts of stored secrets (from the API), shown as hints when editing a profile. */
  stored_communities: number;
  stored_v3: number;
  /** Editing a profile: whether the secret lists are being replaced (otherwise they are kept). */
  replace_secrets: boolean;
  tcp: boolean;
  tcp_ports: string;
  tcp_timeout_ms: string;
  dns: boolean;
};

export function newExtraV3(): ExtraV3Form {
  return {
    v3_username: '',
    v3_security_level: 'authPriv',
    v3_auth_protocol: 'SHA',
    v3_auth_key: '',
    v3_priv_protocol: 'AES128',
    v3_priv_key: '',
  };
}

export function scanOptionsForm(o?: ScanOptions | null): ScanOptionsForm {
  return {
    ping: o?.ping ?? true,
    ping_timeout_ms: String(o?.ping_timeout_ms ?? 1000),
    snmp: o?.snmp ?? true,
    snmp_port: o?.snmp_port != null ? String(o.snmp_port) : '',
    snmp_timeout_s: String(o?.snmp_timeout_s ?? 1),
    extra_communities: '',
    extra_v3: [],
    stored_communities: o?.extra_communities_count ?? 0,
    stored_v3: o?.extra_v3_count ?? 0,
    // A new profile/scan has nothing stored, so the lists are always "replaced".
    replace_secrets: !o,
    tcp: o?.tcp ?? true,
    tcp_ports: (o?.tcp_ports ?? DEFAULT_TCP_PORTS).join(', '),
    tcp_timeout_ms: String(o?.tcp_timeout_ms ?? 1000),
    dns: o?.dns ?? true,
  };
}

export type OptionsErrors = Partial<
  Record<
    | 'probes'
    | 'ping_timeout_ms'
    | 'snmp_port'
    | 'snmp_timeout_s'
    | 'extra_communities'
    | 'extra_v3'
    | 'tcp_ports'
    | 'tcp_timeout_ms',
    string
  >
>;

function intIn(s: string, min: number, max: number): number | null {
  const v = s.trim();
  if (!/^\d+$/.test(v)) return null;
  const n = Number(v);
  return n >= min && n <= max ? n : null;
}

/** Parses a ports list ("22, 80 443") into distinct ports, or null when invalid. */
export function parsePorts(s: string): number[] | null {
  const parts = s.split(/[\s,;]+/).filter(Boolean);
  if (!parts.length) return null;
  const out: number[] = [];
  for (const p of parts) {
    if (!/^\d+$/.test(p)) return null;
    const n = Number(p);
    if (n < 1 || n > 65535) return null;
    if (!out.includes(n)) out.push(n);
  }
  return out;
}

/**
 * Builds the request options. Error values are i18n keys under `discovery.err.*`.
 * Secret lists are only sent when `replace_secrets` is set (API: omitted = keep stored list).
 */
export function scanOptionsToInput(f: ScanOptionsForm): {
  options: ScanOptionsInput;
  errors: OptionsErrors;
} {
  const errors: OptionsErrors = {};
  const options: ScanOptionsInput = { ping: f.ping, snmp: f.snmp, tcp: f.tcp, dns: f.dns };
  if (!f.ping && !f.snmp && !f.tcp) errors.probes = 'discovery.err.probes';

  const pt = intIn(f.ping_timeout_ms, 100, 5000);
  if (pt === null) errors.ping_timeout_ms = 'discovery.err.pingTimeout';
  else options.ping_timeout_ms = pt;

  if (f.snmp_port.trim()) {
    const p = intIn(f.snmp_port, 1, 65535);
    if (p === null) errors.snmp_port = 'discovery.err.port';
    else options.snmp_port = p;
  } else options.snmp_port = null;

  const st = intIn(f.snmp_timeout_s, 1, 10);
  if (st === null) errors.snmp_timeout_s = 'discovery.err.snmpTimeout';
  else options.snmp_timeout_s = st;

  const tt = intIn(f.tcp_timeout_ms, 100, 5000);
  if (tt === null) errors.tcp_timeout_ms = 'discovery.err.tcpTimeout';
  else options.tcp_timeout_ms = tt;

  const ports = parsePorts(f.tcp_ports);
  if (!ports || ports.length > 32) {
    if (f.tcp) errors.tcp_ports = 'discovery.err.ports';
  } else options.tcp_ports = ports;

  if (f.replace_secrets) {
    const communities = f.extra_communities
      .split('\n')
      .map((x) => x.trim())
      .filter(Boolean);
    if (communities.length > 5 || communities.some((c) => c.length > 64))
      errors.extra_communities = 'discovery.err.communities';
    else options.extra_communities = communities;

    if (f.extra_v3.length > 3 || f.extra_v3.some((v) => !v.v3_username.trim()))
      errors.extra_v3 = 'discovery.err.v3';
    else
      options.extra_v3 = f.extra_v3.map((v): ExtraV3Input => {
        const auth = v.v3_security_level !== 'noAuthNoPriv';
        const priv = v.v3_security_level === 'authPriv';
        return {
          v3_username: v.v3_username.trim(),
          v3_security_level: v.v3_security_level,
          v3_auth_protocol: auth ? v.v3_auth_protocol : null,
          v3_auth_key: auth ? v.v3_auth_key : null,
          v3_priv_protocol: priv ? v.v3_priv_protocol : null,
          v3_priv_key: priv ? v.v3_priv_key : null,
        };
      });
  }
  return { options, errors };
}

// ---------------------------------------------------------------------------
// Profile schedule (UI offers Off / Daily / Weekly / every N hours)

export type ScheduleChoice = 'off' | 'daily' | 'weekly' | 'hours';

export function scheduleChoice(enabled: boolean, hours: number): ScheduleChoice {
  if (!enabled) return 'off';
  if (hours === 24) return 'daily';
  if (hours === 168) return 'weekly';
  return 'hours';
}

export function scheduleFromChoice(
  choice: ScheduleChoice,
  hours: string,
): { schedule_enabled: boolean; schedule_every_hours: number } | null {
  switch (choice) {
    case 'off': {
      const n = intIn(hours, 1, 720);
      return { schedule_enabled: false, schedule_every_hours: n ?? 24 };
    }
    case 'daily':
      return { schedule_enabled: true, schedule_every_hours: 24 };
    case 'weekly':
      return { schedule_enabled: true, schedule_every_hours: 168 };
    case 'hours': {
      const n = intIn(hours, 1, 720);
      return n === null ? null : { schedule_enabled: true, schedule_every_hours: n };
    }
  }
}
