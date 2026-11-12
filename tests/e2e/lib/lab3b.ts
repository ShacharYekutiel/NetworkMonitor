// Phase 3b helpers: Bedrock mock control, assistant settings/test/enable, SSE parsing, AI feature toggles,
// injection checks (docs/architecture.md §14, §14.11; docs/api.md "[3b] Phase 3b contract").
// The mock (tests/sim/bedrock/mock_bedrock.py) is started by tests/e2e/run.sh (E2E_PHASE3B=1): $BEDROCK_URL.
import { expect, request as pwRequest, type Page } from '@playwright/test';
import { mkdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { ADMIN, Api, BASE_URL, apiAs, uid } from './helpers';
import { raw } from './lab';
import { problemDevice, setFeatures } from './lab3';

export const BEDROCK_URL = process.env.BEDROCK_URL ?? 'http://127.0.0.1:18700';
export const BEDROCK_HTTPS_URL = process.env.BEDROCK_HTTPS_URL ?? '';
export const BEDROCK_PKI = process.env.BEDROCK_PKI ?? '';
export const PHASE3B = !!process.env.BEDROCK_URL;
/** What the backend is configured with: the TLS listener (test PKI trusted via NETMON_EXTRA_CA_FILE; the SPA form
 * accepts https:// only), falling back to plain http (accepted by the server under NETMON_TEST_MODE=1). */
export const MOCK_ENDPOINT = BEDROCK_HTTPS_URL || BEDROCK_URL;
export const SHOT_DIR_3B = process.env.QA3B_SHOT_DIR ?? join(__dirname, '..', 'test-results', 'screenshots', 'qa-3b');

// owner decision: default us-east-1 + `us.` inference profile; il-central-1 + `global.` supported
export const MODEL = 'us.anthropic.claude-sonnet-4-5-20250929-v1:0';
export const LIGHT_MODEL = 'us.anthropic.claude-haiku-4-5-20251001-v1:0';
export const IL_MODEL = 'global.anthropic.claude-sonnet-4-5-20250929-v1:0';
export const AK = 'AKIAMOCKSTATIC000001';
export const SK = 'mockStaticSecretKey/000000000000000000000';
export const RA = {
  trust_anchor: 'arn:aws:rolesanywhere:us-east-1:111122223333:trust-anchor/7a1b2c3d-0000-4000-8000-000000000001',
  profile: 'arn:aws:rolesanywhere:us-east-1:111122223333:profile/7a1b2c3d-0000-4000-8000-000000000002',
  role: 'arn:aws:iam::111122223333:role/netmon-bedrock',
};
export const PFX_PASSWORD = 'Pfx-pass-1';
export const KB_ID = 'QAKBTEST01';
export const ADMIN2 = { username: 'qa3b-admin2', password: 'Admin2-pass-1' };

export const AI_FEATURES = [
  'ai_explain_alert', 'ai_correlation', 'ai_alert_notes', 'ai_config_diff', 'ai_syslog_digest', 'ai_rule_drafts',
  'ai_template_drafts', 'ai_knowledge', 'ai_chat_ask', 'ai_report_summary',
] as const;
export type AiFeature = (typeof AI_FEATURES)[number];

// ------------------------------------------------------------------------------------------------ mock control
async function j(url: string, method = 'GET', body?: unknown): Promise<any> {
  const r = await fetch(url, { method, body: body === undefined ? undefined : JSON.stringify(body), headers: body === undefined ? undefined : { 'Content-Type': 'application/json' } });
  const t = await r.text();
  if (!r.ok) throw new Error(`${method} ${url} -> ${r.status} ${t}`);
  return t ? JSON.parse(t) : null;
}
export const mock = {
  reset: () => j(`${BEDROCK_URL}/_reset`, 'POST'),
  mode: (mode: string, target?: string, times?: number) => j(`${BEDROCK_URL}/_mode`, 'POST', { mode, target, times }),
  clearModes: () => j(`${BEDROCK_URL}/_mode`, 'DELETE'),
  requests: (q = ''): Promise<any[]> => j(`${BEDROCK_URL}/_requests${q ? `?${q}` : ''}`),
  clearRequests: () => j(`${BEDROCK_URL}/_requests`, 'DELETE'),
  addScenario: (scenario: Record<string, unknown>) => j(`${BEDROCK_URL}/_scenarios`, 'POST', { scenario }),
  clearScenarios: () => j(`${BEDROCK_URL}/_scenarios`, 'DELETE'),
  force: (name: string | null) => j(`${BEDROCK_URL}/_force_scenario`, 'POST', { name }),
  kb: (body?: Record<string, unknown>) => j(`${BEDROCK_URL}/_kb`, body ? 'POST' : 'GET', body),
  config: (body?: Record<string, unknown>) => j(`${BEDROCK_URL}/_config`, body ? 'POST' : 'GET', body),
  usage: () => j(`${BEDROCK_URL}/_usage`),
  credentials: () => j(`${BEDROCK_URL}/_credentials`),
  expire: (body: Record<string, unknown>) => j(`${BEDROCK_URL}/_credentials/expire`, 'POST', body),
};
/** Model calls (converse + converse_stream) since `seq`. The neutral audit-title call (light model, purpose `title`,
 *  made after a completed answer — owner rule: no question text in the audit) is excluded unless `withTitle`. */
export async function modelCalls(since = 0, extra = '', withTitle = false): Promise<any[]> {
  const all = await mock.requests(`op=converse,converse_stream&since=${since}${extra ? `&${extra}` : ''}`);
  return withTitle ? all : all.filter((c: any) => c.purpose !== 'title');
}
export async function lastSeq(): Promise<number> {
  const all = await mock.requests();
  return all.length ? all[all.length - 1].seq : 0;
}

// ------------------------------------------------------------------------------------------------ settings
/** Connection settings for auth mode access_key against the mock (proxy direct: the mock is local). */
export function akSettings(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    auth_mode: 'access_key', region: 'us-east-1', model_id: MODEL, model_id_light: null, endpoint_url: MOCK_ENDPOINT,
    control_endpoint_url: MOCK_ENDPOINT, proxy_mode: 'direct', proxy_url: null, prompt_caching: true,
    access_key_id: AK, secret_access_key: SK, ...over,
  };
}
export function raSettings(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    auth_mode: 'roles_anywhere', region: 'us-east-1', model_id: MODEL, endpoint_url: MOCK_ENDPOINT,
    control_endpoint_url: MOCK_ENDPOINT, proxy_mode: 'direct', prompt_caching: true, ra_region: 'us-east-1',
    ra_endpoint_url: MOCK_ENDPOINT, ra_trust_anchor_arn: RA.trust_anchor, ra_profile_arn: RA.profile,
    ra_role_arn: RA.role, ra_session_seconds: 3600, ...over,
  };
}
export const pkiFile = (name: string) => join(BEDROCK_PKI, name);

export async function assistantSettings(api: Api): Promise<any> {
  return api.get('/api/assistant/settings');
}
export async function testConnection(api: Api, settings?: Record<string, unknown>): Promise<{ status: number; body: any }> {
  const r = await raw(api, 'POST', '/api/assistant/test', settings ? { settings } : {});
  return { status: r.status, body: r.body };
}
/** Uploads a certificate of the test PKI: PEM cert + key (default) or the .pfx with its password. */
export async function uploadCert(api: Api, leaf = 'leaf-rsa', opts: { pfx?: boolean; key?: string; password?: string; bundle?: boolean } = {}) {
  const multipart: Record<string, any> = {};
  if (opts.pfx) {
    multipart.file = { name: `${leaf}.pfx`, mimeType: 'application/x-pkcs12', buffer: readFileSync(pkiFile(`${leaf}.pfx`)) };
    multipart.password = opts.password ?? PFX_PASSWORD;
  } else {
    const cert = opts.bundle ? `${leaf}-bundle.pem` : `${leaf}.pem`;
    multipart.file = { name: cert, mimeType: 'application/x-pem-file', buffer: readFileSync(pkiFile(cert)) };
    multipart.key = { name: 'key.pem', mimeType: 'application/x-pem-file', buffer: readFileSync(pkiFile(opts.key ?? `${leaf}.key`)) };
    if (opts.password) multipart.password = opts.password;
  }
  const r = await api.ctx.fetch('/api/assistant/cert', { method: 'POST', multipart });
  const text = await r.text();
  let body: any = null;
  try { body = JSON.parse(text); } catch { body = null; }
  return { status: r.status(), body, text };
}

/** Test + enable with the given connection settings (audit must be ON; § 14.7). */
export async function enableAssistant(api: Api, settings: Record<string, unknown> = akSettings()): Promise<any> {
  await setFeatures(api, { audit: true });
  const t = await testConnection(api, settings);
  expect(t.status, `assistant test: ${JSON.stringify(t.body)}`).toBe(200);
  expect(t.body.ok, `assistant test steps: ${JSON.stringify(t.body.steps)}`).toBe(true);
  const r = await raw(api, 'PUT', '/api/assistant/settings', { ...settings, enabled: true, test_id: t.body.test_id });
  expect(r.status, `enable assistant: ${r.text}`).toBe(200);
  return r.body;
}
/** Idempotent: leaves an already-enabled assistant alone (Test is rate limited: 10 per admin per 10 min). */
export async function ensureAssistant(api: Api): Promise<void> {
  const s = await assistantSettings(api);
  if (!s.enabled) await enableAssistant(api);
  // the whole 3b suite asks far more than the default 50 questions per admin per day (cap tested in spec 66)
  const cur = (await api.get('/api/settings')).assistant_daily_questions_per_user;
  if (cur !== E2E_DAILY_CAP) await setScalars(api, { assistant_daily_questions_per_user: E2E_DAILY_CAP });
}
export const E2E_DAILY_CAP = 1000;
export async function aiFeatures(api: Api, map: Partial<Record<AiFeature | 'syslog' | 'traps' | 'config_backup' | 'chat_ack' | 'reports' | 'audit' | 'flows' | 'timeline', boolean>>) {
  await setFeatures(api, map as Record<string, boolean>);
}
export async function setScalars(api: Api, body: Record<string, unknown>): Promise<void> {
  const r = await raw(api, 'PUT', '/api/settings', body);
  expect(r.status, `PUT /api/settings ${JSON.stringify(body)}: ${r.text}`).toBe(200);
}

/** A second admin (separate Test rate-limit bucket and conversation list). */
export async function ensureAdmin2(admin: Api): Promise<Api> {
  return ensureAdminUser(admin, ADMIN2);
}
/** Any extra admin (each has its own Test rate-limit bucket: 10 per admin per 10 min). */
export async function ensureAdminUser(admin: Api, user: { username: string; password: string }): Promise<Api> {
  const users = await admin.get('/api/users?page_size=200').catch(() => ({ items: [] }));
  const list: any[] = users.items ?? users;
  if (!list.some((u: any) => u.username === user.username)) {
    await admin.post('/api/users', { username: user.username, password: user.password, role: 'admin', language: 'en', email: null });
  }
  return apiAs(user);
}
export const KB_ADMIN = { username: 'qa3b-kbadmin', password: 'KbAdmin-pass-1' };

// ------------------------------------------------------------------------------------------------ SSE
export type SseEvent = { id: string | null; event: string; data: any };
export function parseSse(text: string): SseEvent[] {
  const out: SseEvent[] = [];
  for (const block of text.split(/\r?\n\r?\n/)) {
    let id: string | null = null;
    let ev = 'message';
    const data: string[] = [];
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith(':')) continue;
      const i = line.indexOf(':');
      const k = i < 0 ? line : line.slice(0, i);
      const v = i < 0 ? '' : line.slice(i + 1).replace(/^ /, '');
      if (k === 'id') id = v;
      else if (k === 'event') ev = v;
      else if (k === 'data') data.push(v);
    }
    if (!data.length) continue;
    let parsed: any = data.join('\n');
    try { parsed = JSON.parse(parsed); } catch { /* keep text */ }
    out.push({ id, event: ev, data: parsed });
  }
  return out;
}
export type AskResult = { status: number; headers: Record<string, string>; events: SseEvent[]; message: any; error: any; body: any; start: any };
/** POST /api/assistant/ask and read the whole SSE stream (resolves when the server closes it). */
export async function ask(api: Api, question: string, conversation_id: number | null = null, path = '/api/assistant/ask', body?: Record<string, unknown>): Promise<AskResult> {
  const r = await api.ctx.fetch(path, { method: 'POST', data: body ?? { conversation_id, question }, headers: { 'Content-Type': 'application/json' }, timeout: 180_000 });
  const text = await r.text();
  const headers = r.headers();
  if (!(headers['content-type'] ?? '').startsWith('text/event-stream')) {
    let b: any = null;
    try { b = JSON.parse(text); } catch { b = text; }
    return { status: r.status(), headers, events: [], message: null, error: b?.error ?? b, body: b, start: null };
  }
  const events = parseSse(text);
  const done = events.filter((e) => e.event === 'done').pop();
  const err = events.find((e) => e.event === 'error');
  return { status: r.status(), headers, events, message: done?.data?.message ?? null, error: err?.data ?? null, body: null, start: events.find((e) => e.event === 'start')?.data ?? null };
}
/** Live SSE reader (Node fetch with the API client's session cookie): for Stop / re-attach / busy tests. */
export type LiveStream = {
  status: number; events: SseEvent[]; finished: Promise<void>; abort: () => void;
  waitFor: (pred: (e: SseEvent) => boolean, timeout?: number) => Promise<SseEvent>;
};
export async function cookieHeader(api: Api): Promise<string> {
  const st = await api.ctx.storageState();
  return st.cookies.map((c) => `${c.name}=${c.value}`).join('; ');
}
export async function openStream(api: Api, path: string, body: Record<string, unknown> | null, headers: Record<string, string> = {}): Promise<LiveStream> {
  const ctl = new AbortController();
  const r = await fetch(BASE_URL + path, {
    method: body ? 'POST' : 'GET', signal: ctl.signal,
    headers: { cookie: await cookieHeader(api), ...(body ? { 'Content-Type': 'application/json' } : {}), ...headers },
    body: body ? JSON.stringify(body) : undefined,
  });
  const events: SseEvent[] = [];
  let buf = '';
  const finished = (async () => {
    if (!r.body) return;
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split(/\r?\n\r?\n/);
        buf = parts.pop() ?? '';
        for (const p of parts) events.push(...parseSse(p + '\n\n'));
      }
    } catch { /* aborted */ }
  })();
  const waitFor = async (pred: (e: SseEvent) => boolean, timeout = 60_000) => {
    const end = Date.now() + timeout;
    for (;;) {
      const e = events.find(pred);
      if (e) return e;
      if (Date.now() > end) throw new Error(`SSE event not seen in ${timeout} ms; got ${events.map((x) => x.event).join(',')}`);
      await new Promise((res) => setTimeout(res, 100));
    }
  };
  return { status: r.status, events, finished, abort: () => ctl.abort(), waitFor };
}

/** A question that must succeed; returns the final AssistantMessage. */
export async function askOk(api: Api, question: string, conversation_id: number | null = null): Promise<{ res: AskResult; msg: any }> {
  const res = await ask(api, question, conversation_id);
  expect(res.status, `ask: ${JSON.stringify(res.body ?? res.error)}`).toBe(200);
  expect(res.message, `done event: ${JSON.stringify(res.events.slice(-3))}`).toBeTruthy();
  return { res, msg: res.message };
}

// ------------------------------------------------------------------------------------------------ audit
export async function audit(api: Api, q: string): Promise<any[]> {
  return (await api.get(`/api/audit?page_size=200&${q}`)).items;
}
/** Audit `changes` are `{field: [old, new]}` pairs (2b-3); this returns `{field: new}`. */
export function changes(row: any): Record<string, any> {
  const out: Record<string, any> = {};
  for (const [k, v] of Object.entries(row?.changes ?? {})) out[k] = Array.isArray(v) && v.length === 2 ? v[1] : v;
  return out;
}
/** Owner decision: audit stores titles only — no question text anywhere in assistant audit rows. */
export function expectNoQuestionText(rows: any[], question: string): void {
  const needle = question.slice(0, 40);
  for (const r of rows) {
    // soft: the revised contract audits the conversation title, which make_title() derives verbatim from the
    // question -> conflicts with "no question text in audit rows" (reported to the lead)
    expect.soft(JSON.stringify(r), `audit ${r.action} #${r.id} must not contain the question text`).not.toContain(needle);
  }
}

// ------------------------------------------------------------------------------------------------ injection
/** Checks the rendered answer DOM: no images/scripts, no external or javascript: links (§14.5 defence 2). */
export async function expectSafeRendering(scope: ReturnType<Page['locator']>, appOrigin = BASE_URL): Promise<void> {
  const report = await scope.evaluate((root, origin) => {
    const bad: string[] = [];
    root.querySelectorAll('img,script,iframe,object,embed,svg image,video,audio,form,style,link').forEach((el) => bad.push(`<${el.tagName.toLowerCase()}>`));
    root.querySelectorAll('*').forEach((el) => {
      for (const a of Array.from(el.attributes)) {
        if (a.name.startsWith('on')) bad.push(`${a.name} on <${el.tagName.toLowerCase()}>`);
        if (['href', 'src', 'srcset', 'action', 'formaction'].includes(a.name)) {
          const v = a.value.trim().toLowerCase();
          if (v.startsWith('javascript:') || v.startsWith('data:')) bad.push(`${a.name}=${a.value}`);
          else if (/^(https?:)?\/\//.test(v)) {
            const u = new URL(a.value, origin);
            if (u.origin !== new URL(origin).origin) bad.push(`external ${a.name}=${a.value}`);
          }
        }
      }
    });
    return bad;
  }, appOrigin);
  expect(report, 'unsafe elements in the rendered answer').toEqual([]);
}
const LINK_RE = /(!?)\[([^\]]*)\]\(([^)\s]+)\)/g;
/** Stored Markdown: no images; links only nm:<ref> that the server issued. */
export function answerProblems(markdown: string, allowedRefs?: Set<string>): string[] {
  const out: string[] = [];
  for (const m of (markdown ?? '').matchAll(LINK_RE)) {
    if (m[1]) { out.push(`image: ${m[0].slice(0, 100)}`); continue; }
    const t = m[3];
    if (!t.startsWith('nm:')) out.push(`non-nm link: ${m[0].slice(0, 100)}`);
    else if (allowedRefs && !allowedRefs.has(t.slice(3))) out.push(`forged ref kept: ${t}`);
  }
  return out;
}
export function corpus(): any {
  // YAML is read by the python helper to avoid a YAML dependency in the E2E package
  const { spawnSync } = require('node:child_process');
  const PY = process.env.PY ?? join(__dirname, '..', '..', '..', 'backend', '.venv', 'bin', 'python');
  const r = spawnSync(PY, ['-c', 'import json,sys,yaml;print(json.dumps(yaml.safe_load(open(sys.argv[1]))))', join(__dirname, '..', '..', 'sim', 'bedrock', 'injection', 'corpus.yaml')], { encoding: 'utf8' });
  if (r.status !== 0) throw new Error(r.stderr);
  return JSON.parse(r.stdout);
}

// ------------------------------------------------------------------------------------------------ UI
export async function shot3b(page: Page, name: string): Promise<string> {
  mkdirSync(SHOT_DIR_3B, { recursive: true });
  const p = join(SHOT_DIR_3B, `${name}.png`);
  await page.screenshot({ path: p, fullPage: false });
  return p;
}
/** The question box of the Ask page (textarea in main). */
export function questionBox(page: Page) {
  return page.locator('main textarea').first();
}
export async function askInUi(page: Page, question: string): Promise<void> {
  const box = questionBox(page);
  await box.fill(question);
  await box.press('Enter');
}
/** A browser-less API client logged in with an API token (Bearer). */
export async function tokenApi(admin: Api, role: 'admin' | 'viewer' = 'admin'): Promise<Api> {
  const { token } = await admin.post('/api/tokens', { name: uid('qa3b-token'), role, expires_days: 1 });
  const ctx = await pwRequest.newContext({ baseURL: BASE_URL, extraHTTPHeaders: { Authorization: `Bearer ${token}` } });
  return new Api(ctx);
}
/** problemDevice + re-enable: after a failed test the worker restarts, afterAll paused the device, and the next
 * beforeAll finds it again (problemDevice is idempotent by host but keeps it disabled). */
export async function problem(api: Api, ip: string, name: string) {
  const p = await problemDevice(api, ip, name);
  await api.patch(`/api/devices/${p.device.id}`, { enabled: true });
  return p;
}
export { raw, uid };
