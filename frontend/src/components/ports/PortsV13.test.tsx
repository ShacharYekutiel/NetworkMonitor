import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import i18n from '../../i18n';
import type { Device, DevicePorts, PortNeighbor } from '../../api/types';
import { mkPort } from '../../lib/portFixtures';
import {
  COLUMNS_KEY,
  COLUMNS_KNOWN_KEY,
  connectedHostView,
  DEFAULT_COLUMNS,
  loadColumns,
  saveColumns,
} from '../../lib/ports';
import { authRoutes, mockFetch, Wrap } from '../../test/v13';
import { PortList } from './PortList';
import { PortsTab } from './PortsTab';

beforeAll(async () => {
  await i18n.changeLanguage('en');
});
beforeEach(() => localStorage.clear());
afterEach(() => vi.unstubAllGlobals());

const lldp: PortNeighbor = {
  link_id: 1,
  source: 'lldp',
  kind: 'device',
  device_id: 42,
  neighbor_id: null,
  name: 'dist-sw',
  port: 'Gi1/0/48',
  device_class: 'switch',
  status: 'up',
  mgmt_address: '10.0.0.2',
};

const ports = [
  mkPort(1, 'GE1/0/1', {
    connected: {
      count: 3,
      first: {
        mac: 'aa:bb:cc:00:00:01',
        ip: '10.1.1.20',
        hostname: 'pc-20.lan',
        vendor: 'Dell',
        device_id: null,
        device_name: null,
      },
    },
    neighbor_name: null,
    neighbor_mgmt_address: null,
  }),
  mkPort(2, 'GE1/0/2', {
    connected: {
      count: 1,
      first: {
        mac: 'aa:bb:cc:00:00:02',
        ip: '10.1.1.5',
        hostname: null,
        vendor: null,
        device_id: 9,
        device_name: 'printer-1',
      },
    },
  }),
  mkPort(48, 'XGE1/0/48', {
    neighbor: lldp,
    is_uplink: true,
    connected: { count: 120, first: null },
    neighbor_name: 'dist-sw',
    neighbor_mgmt_address: '10.0.0.2',
  }),
  mkPort(3, 'GE1/0/3'),
];

describe('[1.3] C. ports: IP and Name/neighbor columns, CSV', () => {
  it('connectedHostView: neighbour, host (+N), uplink, old-server fallback', () => {
    expect(connectedHostView(ports[2])).toEqual({
      kind: 'neighbor',
      name: 'dist-sw',
      mgmt: '10.0.0.2',
      device_id: 42,
    });
    expect(connectedHostView(ports[0])).toMatchObject({ kind: 'host', ip: '10.1.1.20', more: 2 });
    expect(
      connectedHostView(mkPort(5, 'x', { is_uplink: true, connected: { count: 9, first: null } })),
    ).toEqual({ kind: 'uplink', count: 9 });
    expect(connectedHostView(ports[3])).toEqual({ kind: 'none' });
    // a 1.2 server: no `connected`, use first_mac
    const old = mkPort(6, 'y', {
      mac_count: 2,
      first_mac: {
        mac: 'aa:aa:aa:aa:aa:aa',
        vlan: 1,
        vendor: null,
        locally_administered: false,
        ip: '10.9.9.9',
        ips: ['10.9.9.9'],
        hostname: null,
        device_id: null,
        device_name: null,
        first_seen: '',
        last_seen: '',
      },
    });
    expect(connectedHostView(old)).toMatchObject({ kind: 'host', ip: '10.9.9.9', more: 1 });
  });

  it('list shows IP, name/device link, +N tooltip and LLDP neighbour with mgmt address', () => {
    render(
      <Wrap>
        <PortList ports={ports} selected={null} onSelect={() => undefined} />
      </Wrap>,
    );
    const headers = screen.getAllByRole('columnheader').map((h) => h.textContent);
    expect(headers).toContain('Connected IP');
    expect(headers).toContain('Name / neighbor');
    const row = (name: string) => screen.getByText(name).closest('tr') as HTMLElement;
    const r1 = row('GE1/0/1');
    expect(within(r1).getByTestId('port-conn-ip').textContent).toBe('10.1.1.20');
    expect(within(r1).getByTestId('port-conn-name').textContent).toContain('pc-20.lan');
    const more = within(r1).getByTestId('port-conn-more');
    expect(more.textContent).toBe('+2');
    expect(more.getAttribute('title')).toBe('2 more devices on this port');
    const r2 = row('GE1/0/2');
    expect(within(r2).getByRole('link', { name: 'printer-1' }).getAttribute('href')).toBe(
      '/devices/9',
    );
    expect(within(r2).queryByTestId('port-conn-more')).toBeNull();
    const r48 = row('XGE1/0/48');
    expect(within(r48).getByTestId('port-conn-ip').textContent).toBe('10.0.0.2');
    expect(within(r48).getByRole('link', { name: 'dist-sw' }).getAttribute('href')).toBe(
      '/devices/42',
    );
    // IP column hidden on narrow screens; the IP repeats in the Name cell there
    const ipTh = screen.getAllByRole('columnheader').find((h) => h.textContent === 'Connected IP')!;
    expect(ipTh.className).toContain('hidden sm:table-cell');
    expect(within(r48).getByTestId('port-conn-name').textContent).toContain('10.0.0.2');
  });

  it('new default columns are added once to a pre-1.3 stored choice', () => {
    localStorage.setItem(COLUMNS_KEY, JSON.stringify(['status', 'connected', 'vlan']));
    expect(loadColumns()).toEqual(['status', 'connected', 'connIp', 'connName', 'vlan']);
    saveColumns(['status', 'vlan']);
    expect(localStorage.getItem(COLUMNS_KNOWN_KEY)).toBeTruthy();
    expect(loadColumns()).toEqual(['status', 'vlan']);
    localStorage.clear();
    expect(loadColumns()).toEqual(DEFAULT_COLUMNS);
    expect(DEFAULT_COLUMNS).toContain('connIp');
  });

  it('Ports tab has an Export CSV link to ports.csv', async () => {
    const data: DevicePorts = {
      device_id: 7,
      ports_effective: true,
      arp_effective: true,
      port_inventory: 'on',
      arp_collect: 'on',
      effective_class: 'switch',
      status: {
        last_run_at: '2026-11-05T10:00:00Z',
        last_ok_at: '2026-11-05T10:00:00Z',
        duration_ms: 100,
        error: null,
        running: false,
        next_run_at: null,
        sources: {
          if_xtable: 'ok',
          duplex: 'ok',
          bridge: 'ok',
          vlans: 'ok',
          fdb: 'ok',
          poe: 'ok',
          arp: 'ok',
        },
        fdb_mib: 'q_bridge',
        vlan_mib: 'static',
        ports: 4,
        macs: 0,
        arp_entries: 0,
        arp_sources: 1,
      },
      settings: { interval_min: 15, uplink_mac_threshold: 5 },
      health: [],
      ports,
    } as unknown as DevicePorts;
    mockFetch((method, url) => {
      const a = authRoutes(method, url);
      if (a) return a;
      if (url.startsWith('/api/devices/7/ports')) return { status: 200, body: data };
      return undefined;
    });
    render(
      <Wrap auth>
        <PortsTab device={{ id: 7, name: 'sw', port_inventory: 'on' } as unknown as Device} />
      </Wrap>,
    );
    const a = await screen.findByTestId('ports-csv');
    expect(a.getAttribute('href')).toBe('/api/devices/7/ports.csv');
    expect(a.hasAttribute('download')).toBe(true);
    expect(a.textContent).toBe('Export CSV');
  });
});
