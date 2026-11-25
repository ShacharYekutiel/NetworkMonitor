// [profiles] Connection profiles (release 1.7.0) E2E helpers: private simulators on their own loopback
// addresses (127.0.0.241-.244, not used by the shared lab), so specs 96-98 neither depend on nor disturb the
// discovery lab / SSH lab state of other specs.
//
//   - snmpsim agents (tests/sim/snmp_agent.py, community "public") on 127.0.0.241-.244 : PROF_SNMP_PORT
//   - an SSH device simulator (tests/sim/ssh/fake_device.py) with comware devices on 127.0.0.241-.244 : 8022
//     (user netmon-backup / Backup-pass-1); its control API records every connect / auth / command.
import { expect } from '@playwright/test';
import { spawn, type ChildProcess } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { type Api } from './helpers';
import { PY, SIM_DIR } from './lab';

export const PROF_SNMP_PORT = 16161;
export const PROF_SSH_PORT = 8022;
export const PROF_HOSTS = ['127.0.0.241', '127.0.0.242', '127.0.0.243', '127.0.0.244'] as const;
export const SSH_USER = 'netmon-backup';
export const SSH_PASS = 'Backup-pass-1';

export type ProfSims = {
  snmp: ChildProcess[];
  ssh: ChildProcess | null;
  sshUrl: string;
  stateDir: string;
};

function waitLine(child: ChildProcess, re: RegExp, what: string, timeout = 30_000): Promise<RegExpMatchArray> {
  return new Promise((resolve, reject) => {
    let buf = '';
    const timer = setTimeout(() => reject(new Error(`${what} did not start: ${buf.slice(-500)}`)), timeout);
    const on = (d: Buffer) => {
      buf += d.toString();
      const m = buf.match(re);
      if (m) {
        clearTimeout(timer);
        resolve(m);
      }
    };
    child.stdout?.on('data', on);
    child.stderr?.on('data', on);
    child.on('exit', (c: number | null) => {
      clearTimeout(timer);
      reject(new Error(`${what} exited (${c}): ${buf.slice(-500)}`));
    });
  });
}

/** Starts the private simulators. `snmpHosts` get an SNMP agent; every PROF_HOSTS address gets an SSH device. */
export async function startProfSims(snmpHosts: readonly string[] = PROF_HOSTS): Promise<ProfSims> {
  const py = existsSync(PY) ? PY : 'python3';
  const snmp: ChildProcess[] = [];
  for (const h of snmpHosts) {
    const c = spawn(py, [join(SIM_DIR, 'snmp_agent.py'), '--host', h, '--port', String(PROF_SNMP_PORT)], {
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    snmp.push(c);
    await waitLine(c, /READY/, `snmp agent ${h}`);
  }
  const stateDir = mkdtempSync(join(tmpdir(), 'netmon-prof-ssh-'));
  const devs = PROF_HOSTS.flatMap((h, i) => ['--device', `prof${i + 1}=comware@${h}:${PROF_SSH_PORT}`]);
  const ssh = spawn(py, [join(SIM_DIR, 'ssh', 'fake_device.py'), '--state-dir', stateDir, '--control-port', '0', ...devs], {
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const m = await waitLine(ssh, /READY control=(\d+)/, 'ssh simulator');
  return { snmp, ssh, sshUrl: `http://127.0.0.1:${m[1]}`, stateDir };
}

export function stopProfSims(s: ProfSims | undefined): void {
  if (!s) return;
  for (const c of [...s.snmp, s.ssh]) if (c && c.exitCode === null) c.kill('SIGTERM');
  try {
    rmSync(s.stateDir, { recursive: true, force: true });
  } catch {
    /* best effort */
  }
}

async function j(url: string, method = 'GET', body?: unknown): Promise<any> {
  const r = await fetch(url, {
    method,
    body: body === undefined ? undefined : JSON.stringify(body),
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
  });
  const t = await r.text();
  if (!r.ok) throw new Error(`${method} ${url} -> ${r.status} ${t}`);
  return t ? JSON.parse(t) : null;
}
export const simLog = (s: ProfSims, device?: string): Promise<any[]> =>
  j(`${s.sshUrl}/log${device ? `?device=${device}` : ''}`);
export const clearSimLog = (s: ProfSims) => j(`${s.sshUrl}/log`, 'DELETE');
export const simState = (s: ProfSims, device: string, patch: Record<string, unknown>) =>
  j(`${s.sshUrl}/devices/${device}/state`, 'POST', patch);
export const simAllowed = (s: ProfSims): Promise<string[]> => j(`${s.sshUrl}/allowed-commands`);

/** The commands / exec requests a device received (the read-only guarantee is checked against these). */
export async function simCommands(s: ProfSims, device?: string): Promise<string[]> {
  return (await simLog(s, device)).filter((e) => e.event === 'command' || e.event === 'exec').map((e) => e.command);
}

export function snmpProfileCreds(community = 'public', extra: Record<string, unknown> = {}) {
  return { version: 'v2c', community, port: PROF_SNMP_PORT, timeout_s: 1, retries: 0, ...extra };
}
export function sshProfileCreds(extra: Record<string, unknown> = {}) {
  return { username: SSH_USER, password: SSH_PASS, port: PROF_SSH_PORT, legacy_algorithms: false, ...extra };
}

/** Status of the device's first sensor of `type`, polled until `want` (the real poller, no fixed sleeps). */
export async function waitSensorStatus(api: Api, sensorId: number, want: string, timeout = 60_000): Promise<any> {
  let last: any = null;
  await expect
    .poll(
      async () => {
        last = await api.get(`/api/sensors/${sensorId}`);
        return last.status;
      },
      { timeout, intervals: [500, 1000, 2000], message: `sensor ${sensorId} -> ${want}` },
    )
    .toBe(want);
  return last;
}

/** Runs the sensor check now (POST /api/sensors/{id}/run): the next status reflects a credential change without waiting a full interval. */
export async function checkNow(api: Api, sensorId: number): Promise<void> {
  await api.post(`/api/sensors/${sensorId}/run`);
}
