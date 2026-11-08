// Test fixtures for the ports view (used by Vitest only; never imported by the app).
import type { Port } from '../api/types';

/** A minimal physical port; only the fields the layout reads matter. */
export function mkPort(if_index: number, name: string, extra: Partial<Port> = {}): Port {
  return {
    if_index,
    name,
    descr: name,
    alias: '',
    kind: 'physical',
    if_type: 6,
    admin_status: 'up',
    oper_status: 'up',
    state: 'up',
    alert: false,
    alert_reasons: [],
    speed_bps: 1_000_000_000,
    duplex: 'full',
    mac: null,
    last_change_at: null,
    pvid: 1,
    mode: 'access',
    untagged_vlans: [1],
    tagged_vlans: [],
    poe: null,
    in_bps: null,
    out_bps: null,
    in_errors_delta: null,
    out_errors_delta: null,
    is_uplink: false,
    uplink_reason: null,
    connected_kind: 'client',
    trunk: false,
    neighbor: null,
    mac_count: 0,
    first_mac: null,
    macs: [],
    sensor_id: null,
    sensor_status: null,
    collected_at: '2026-11-05T10:00:00Z',
    ...extra,
  };
}
