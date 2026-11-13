// Release 1.3 helpers (docs/api.md § "[1.3] Release 1.3 contract", docs/architecture.md § 14):
// sensor impact, important ports, apply-template port selection, connected hosts / CSV, trunk + LAG alerts.
//
// Lab: the 2b-2 port sims (127.0.0.81 ArubaOS "port-aos": 1-8 access, 9 + 10 trunks with tagged VLANs 10/20,
// 6 + 8 oper down, Trk1 = 11 (LAG, no members), 12 DEFAULT_VLAN) and the 2b-3 LAG sims (127.0.0.88 "lag-aos":
// Trk1 = 3 + 4 via LAG-MIB; 127.0.0.87 "lag-cw").
import { expect, type Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { Api, SCREENSHOT_DIR, uid } from './helpers';
import { ensureGroup, raw, snmpCreds } from './lab';
import { refreshPorts } from './lab2b2';

export const SHOT_DIR_13 = process.env.QA13_SHOT_DIR ?? join(SCREENSHOT_DIR, 'qa-13');
export const GROUP_13 = 'QA 1.3';

export async function shot13(page: Page, name: string): Promise<string> {
  mkdirSync(SHOT_DIR_13, { recursive: true });
  const p = join(SHOT_DIR_13, `${name}.png`);
  await page.screenshot({ path: p, fullPage: false });
  return p;
}

/**
 * A fresh switch device on a port sim (own device, so other specs' sensors / conditions on the shared lab
 * devices do not interfere): ping-less (snmp_system only), identified as a switch, port inventory collected.
 */
export async function freshSwitch(api: Api, host: string, prefix: string): Promise<any> {
  const gid = await ensureGroup(api, GROUP_13);
  let d = await api.post('/api/devices', { group_id: gid, name: uid(prefix), host, snmp_credentials: snmpCreds(), auto_sensors: ['snmp_system'] });
  const r = await raw(api, 'POST', `/api/devices/${d.id}/identify`);
  expect(r.status, r.text).toBe(200);
  d = await api.get(`/api/devices/${d.id}`);
  expect(d.effective_class ?? d.identity?.device_class, `${host} class`).toBe('switch');
  await refreshPorts(api, d.id);
  return d;
}

export async function ifSensors(api: Api, deviceId: number): Promise<any[]> {
  return (await api.get(`/api/sensors?device_id=${deviceId}&page_size=1000`)).items.filter((s: any) => s.type === 'snmp_interface');
}

export async function waitDeviceStatus(api: Api, id: number, status: string, timeout = 30_000): Promise<any> {
  let d: any;
  await expect
    .poll(async () => { d = await api.get(`/api/devices/${id}`); return d.status; }, { timeout, intervals: [300, 600, 1000], message: `device ${id} -> ${status}` })
    .toBe(status);
  return d;
}
