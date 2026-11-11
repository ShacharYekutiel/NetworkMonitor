// [3b] Pure helpers of the assistant UI (architecture.md § 14.3, § 14.10).
import type {
  AssistantMessage,
  AssistantSettings,
  AssistantSource,
  AssistantStep,
  AuthMode,
  Features,
  StreamEvent,
} from '../api/types';
import type { RawSseEvent } from './sse';

// --- nm: links ---------------------------------------------------------------

const INT = /^\d{1,12}$/;

/**
 * Maps an `nm:` ref (without the `nm:` prefix) to its SPA route (architecture § 14.3.7); null for
 * an unknown or malformed ref. `kb/<n>` refs are resolved through the message sources instead.
 */
export function nmRoute(ref: string): string | null {
  if (!ref || ref.length > 400) return null;
  const slash = ref.indexOf('/');
  if (slash < 1) return null;
  const kind = ref.slice(0, slash);
  const rest = ref.slice(slash + 1);
  if (kind === 'flows') {
    // the query part is built and url-encoded by the server
    if (rest.length > 300 || /[\s<>"'`\\]/.test(rest)) return null;
    return rest ? `/flows?${rest}` : '/flows';
  }
  const p = rest.split('/');
  if (!p.every((x) => INT.test(x))) return null;
  const one = p.length === 1;
  switch (kind) {
    case 'device':
      return one ? `/devices/${p[0]}` : null;
    case 'sensor':
      return one ? `/sensors/${p[0]}` : null;
    case 'group':
      return one ? `/groups/${p[0]}` : null;
    case 'port':
      return p.length === 2 ? `/devices/${p[0]}?tab=ports&port=${p[1]}` : null;
    case 'event':
      return one ? `/events?id=${p[0]}` : null;
    case 'syslog':
      return one ? `/logs?tab=syslog&id=${p[0]}` : null;
    case 'trap':
      return one ? `/logs?tab=traps&id=${p[0]}` : null;
    case 'maintenance':
      return one ? `/maintenance?id=${p[0]}` : null;
    case 'config':
      return p.length === 2 ? `/devices/${p[0]}?tab=configuration&version=${p[1]}` : null;
    case 'config-diff':
      return p.length === 3 ? `/devices/${p[0]}?tab=configuration&from=${p[1]}&to=${p[2]}` : null;
    case 'topology':
      return one ? `/topology?focus=${p[0]}` : null;
    case 'incident':
      return one ? `/incidents/${p[0]}` : null;
    default:
      return null;
  }
}

/** `kb/<n>` → the n-th Knowledge Base source of the message; only `https://` URLs are clickable. */
export function kbSourceUrl(ref: string, sources: AssistantSource[] | undefined): string | null {
  if (!/^kb\/\d{1,3}$/.test(ref)) return null;
  const s = sources?.find((x) => x.ref === ref);
  return s?.url && /^https:\/\//i.test(s.url) ? s.url : null;
}

export type LinkTarget = { kind: 'internal'; href: string } | { kind: 'external'; href: string };

/**
 * Where a link written by the model may point. `allowedRefs` (refs seen in tool results so far)
 * applies while streaming; the final stored message is already validated by the server.
 */
export function resolveNmLink(
  url: string,
  opts: { allowedRefs?: ReadonlySet<string> | null; sources?: AssistantSource[] } = {},
): LinkTarget | null {
  if (!url.toLowerCase().startsWith('nm:')) return null;
  const ref = url.slice(3);
  if (opts.allowedRefs && !opts.allowedRefs.has(ref)) return null;
  if (ref.startsWith('kb/')) {
    const u = kbSourceUrl(ref, opts.sources);
    return u ? { kind: 'external', href: u } : null;
  }
  const r = nmRoute(ref);
  return r ? { kind: 'internal', href: r } : null;
}

/** Markdown for *Copy*: `nm:` links become absolute app URLs (kb links their source URL or text). */
export function copyableMarkdown(
  content: string,
  origin: string,
  sources: AssistantSource[] = [],
): string {
  return content.replace(/\[([^\]\n]*)\]\(nm:([^)\s]+)\)/g, (_m, label: string, ref: string) => {
    const tgt = resolveNmLink(`nm:${ref}`, { sources });
    if (!tgt) return label;
    return `[${label}](${tgt.kind === 'internal' ? origin + tgt.href : tgt.href})`;
  });
}

// --- SSE events → live answer -------------------------------------------------

const EVENTS = new Set(['start', 'step', 'delta', 'round_end', 'notice', 'done', 'error']);

/** Typed stream event from a raw SSE event; null for unknown events or bad JSON. */
export function toStreamEvent(raw: RawSseEvent): StreamEvent | null {
  if (!EVENTS.has(raw.event)) return null;
  let data: unknown;
  try {
    data = JSON.parse(raw.data);
  } catch {
    return null;
  }
  if (!data || typeof data !== 'object') return null;
  const id = raw.id !== null && /^\d+$/.test(raw.id) ? Number(raw.id) : null;
  return { event: raw.event, id, data } as StreamEvent;
}

export type LiveAnswer = {
  conversationId: number | null;
  messageId: number | null;
  language: 'en' | 'he' | null;
  redaction: boolean;
  /** text of the current round */
  text: string;
  round: number;
  /** text of rounds that ended in tool use (dimmed) */
  notes: { round: number; text: string }[];
  steps: AssistantStep[];
  refs: string[];
  notices: { code: string; message: string }[];
  error: { code: string; message: string; details?: Record<string, unknown>[] } | null;
  final: AssistantMessage | null;
  lastEventId: number | null;
};

export const EMPTY_LIVE: LiveAnswer = {
  conversationId: null,
  messageId: null,
  language: null,
  redaction: false,
  text: '',
  round: 1,
  notes: [],
  steps: [],
  refs: [],
  notices: [],
  error: null,
  final: null,
  lastEventId: null,
};

/** Applies one stream event (pure reducer; replays with the same ids are idempotent). */
export function applyStreamEvent(s: LiveAnswer, ev: StreamEvent): LiveAnswer {
  if (ev.id !== null && s.lastEventId !== null && ev.id <= s.lastEventId) return s;
  const n: LiveAnswer = { ...s, lastEventId: ev.id ?? s.lastEventId };
  switch (ev.event) {
    case 'start':
      n.conversationId = ev.data.conversation_id;
      n.messageId = ev.data.message_id;
      n.language = ev.data.language;
      n.redaction = !!ev.data.redaction;
      break;
    case 'step': {
      const { refs, ...step } = ev.data;
      const i = n.steps.findIndex((x) => x.call_id === step.call_id);
      n.steps = i < 0 ? [...n.steps, step] : n.steps.map((x, j) => (j === i ? step : x));
      if (refs?.length) n.refs = [...new Set([...n.refs, ...refs])];
      break;
    }
    case 'delta':
      n.text = s.text + (ev.data.text ?? '');
      break;
    case 'round_end':
      if (s.text.trim()) n.notes = [...s.notes, { round: ev.data.round, text: s.text }];
      n.text = '';
      n.round = ev.data.round + 1;
      break;
    case 'notice':
      n.notices = [...s.notices, ev.data];
      break;
    case 'error':
      n.error = ev.data;
      break;
    case 'done':
      n.final = ev.data.message;
      n.conversationId = ev.data.message.conversation_id;
      n.messageId = ev.data.message.id;
      break;
  }
  return n;
}

/** The live state of an already stored message (re-attach starts from it). */
export function liveFromMessage(m: AssistantMessage): LiveAnswer {
  return {
    ...EMPTY_LIVE,
    conversationId: m.conversation_id,
    messageId: m.id,
    text: m.status === 'running' ? '' : m.content,
    notes: m.notes,
    steps: m.steps,
    final: m.status === 'running' ? null : m,
  };
}

// --- errors -----------------------------------------------------------------

/** Codes after which *Retry* makes sense (transient on the AWS side or our concurrency). */
export const RETRYABLE_CODES = new Set([
  'bedrock_throttled',
  'bedrock_unavailable',
  'aws_network',
  'assistant_timeout',
  'assistant_busy',
  'network_error',
  'stream_interrupted',
]);
export function isRetryable(code: string | null | undefined): boolean {
  return !!code && RETRYABLE_CODES.has(code);
}

// --- example questions ----------------------------------------------------------

export type Example = { key: string; needs: (keyof Features)[] };
/** Example chips (architecture § 14.10); hidden when a needed feature is OFF. */
export const EXAMPLES: Example[] = [
  { key: 'down', needs: [] },
  { key: 'yesterday', needs: [] },
  { key: 'talkers', needs: ['flows'] },
  { key: 'mac', needs: [] },
  { key: 'configChanges', needs: ['config_backup'] },
  { key: 'availability', needs: [] },
  { key: 'syslogErrors', needs: ['syslog'] },
];
export function visibleExamples(f: Features): Example[] {
  return EXAMPLES.filter((e) => e.needs.every((n) => f[n]));
}

// --- connection form -------------------------------------------------------------

export type ConnForm = {
  auth_mode: AuthMode;
  region: string;
  model_id: string;
  model_id_light: string;
  endpoint_url: string;
  control_endpoint_url: string;
  proxy_mode: AssistantSettings['proxy_mode'];
  proxy_url: string;
  prompt_caching: boolean;
  access_key_id: string;
  secret_access_key: string;
  clear_secret: boolean;
  /** write-only session token of temporary (ASIA…) keys; empty keeps the stored one */
  session_token: string;
  clear_session_token: boolean;
  access_key_created_at: string;
  ra_region: string;
  ra_endpoint_url: string;
  ra_trust_anchor_arn: string;
  ra_profile_arn: string;
  ra_role_arn: string;
  ra_session_seconds: string;
};

/** Owner decision: default setup is us-east-1 with a `us.` inference profile. */
export const DEFAULT_REGION = 'us-east-1';
export const SUGGESTED_REGIONS = ['us-east-1', 'us-west-2', 'il-central-1', 'eu-central-1'];

export function connFormFrom(s: AssistantSettings): ConnForm {
  return {
    auth_mode: s.auth_mode,
    region: s.region || DEFAULT_REGION,
    model_id: s.model_id ?? '',
    model_id_light: s.model_id_light ?? '',
    endpoint_url: s.endpoint_url ?? '',
    control_endpoint_url: s.control_endpoint_url ?? '',
    proxy_mode: s.proxy_mode,
    proxy_url: s.proxy_url ?? '',
    prompt_caching: s.prompt_caching,
    access_key_id: s.access_key_id ?? '',
    secret_access_key: '',
    clear_secret: false,
    session_token: '',
    clear_session_token: false,
    access_key_created_at: s.access_key_created_at ?? '',
    ra_region: s.ra_region ?? '',
    ra_endpoint_url: s.ra_endpoint_url ?? '',
    ra_trust_anchor_arn: s.ra_trust_anchor_arn ?? '',
    ra_profile_arn: s.ra_profile_arn ?? '',
    ra_role_arn: s.ra_role_arn ?? '',
    ra_session_seconds: String(s.ra_session_seconds ?? 3600),
  };
}

const orNull = (v: string) => (v.trim() ? v.trim() : null);

/** The writable connection fields of the form (request body of PUT / Test). */
export function connInput(f: ConnForm): Record<string, unknown> {
  const body: Record<string, unknown> = {
    auth_mode: f.auth_mode,
    region: f.region.trim(),
    model_id: f.model_id.trim(),
    model_id_light: orNull(f.model_id_light),
    endpoint_url: orNull(f.endpoint_url),
    control_endpoint_url: orNull(f.control_endpoint_url),
    proxy_mode: f.proxy_mode,
    proxy_url: f.proxy_mode === 'custom' ? orNull(f.proxy_url) : null,
    prompt_caching: f.prompt_caching,
    access_key_id: orNull(f.access_key_id),
    access_key_created_at: orNull(f.access_key_created_at),
    ra_region: orNull(f.ra_region),
    ra_endpoint_url: orNull(f.ra_endpoint_url),
    ra_trust_anchor_arn: orNull(f.ra_trust_anchor_arn),
    ra_profile_arn: orNull(f.ra_profile_arn),
    ra_role_arn: orNull(f.ra_role_arn),
    ra_session_seconds: Number(f.ra_session_seconds) || 3600,
  };
  if (f.clear_secret) body.secret_access_key = '';
  else if (f.secret_access_key) body.secret_access_key = f.secret_access_key;
  // a changed (or cleared) token changes connKey, which locks the switch until a new test
  const token = f.session_token.replace(/\s+/g, '');
  if (f.clear_session_token) body.session_token = '';
  else if (token) body.session_token = token;
  return body;
}

/**
 * Stable key of the connection values a test covered: any edit of a connection field locks the
 * bottom switch again (architecture § 14.2.6). `certVersion` changes on certificate upload/delete.
 */
export function connKey(f: ConnForm, certVersion = ''): string {
  return JSON.stringify([connInput(f), certVersion]);
}

export type ConnErrors = Partial<Record<keyof ConnForm, string>>;
const REGION_RE = /^[a-z]{2}(-gov)?-[a-z]+-\d$/;
const MODEL_RE = /^[A-Za-z0-9.:/_-]{1,256}$/;
const ARN_RA = (kind: string) =>
  new RegExp(`^arn:aws[a-z-]*:rolesanywhere:[a-z0-9-]+:\\d{12}:${kind}/[0-9a-f-]{36}$`);
const ARN_ROLE = /^arn:aws[a-z-]*:iam::\d{12}:role\/[\w+=,.@/-]+$/;
const ACCESS_KEY_RE = /^(AKIA|ASIA)[A-Z0-9]{16}$/;
const SESSION_TOKEN_RE = /^[A-Za-z0-9+/=_-]{1,4096}$/;

/** `ASIA…` key ids are temporary credentials (STS / IAM Identity Center): they need a session token. */
export function isTemporaryKey(accessKeyId: string): boolean {
  return accessKeyId.trim().startsWith('ASIA');
}

export function isHttpsUrl(v: string): boolean {
  try {
    const u = new URL(v);
    return u.protocol === 'https:' && (u.pathname === '/' || u.pathname === '') && !u.search;
  } catch {
    return false;
  }
}

/** Client-side checks mirroring data-model § [3b] Settings (the server stays authoritative). */
export function connErrors(f: ConnForm): ConnErrors {
  const e: ConnErrors = {};
  if (!REGION_RE.test(f.region.trim())) e.region = 'region';
  if (!MODEL_RE.test(f.model_id.trim())) e.model_id = 'model';
  if (f.model_id_light.trim() && !MODEL_RE.test(f.model_id_light.trim()))
    e.model_id_light = 'model';
  for (const k of ['endpoint_url', 'control_endpoint_url', 'ra_endpoint_url'] as const)
    if (f[k].trim() && !isHttpsUrl(f[k].trim())) e[k] = 'url';
  if (f.proxy_mode === 'custom' && !/^https?:\/\/[^/@\s]+:\d{1,5}\/?$/.test(f.proxy_url.trim()))
    e.proxy_url = 'proxy';
  if (f.auth_mode === 'roles_anywhere') {
    if (!ARN_RA('trust-anchor').test(f.ra_trust_anchor_arn.trim())) e.ra_trust_anchor_arn = 'arn';
    if (!ARN_RA('profile').test(f.ra_profile_arn.trim())) e.ra_profile_arn = 'arn';
    if (!ARN_ROLE.test(f.ra_role_arn.trim())) e.ra_role_arn = 'arn';
    if (f.ra_region.trim() && !REGION_RE.test(f.ra_region.trim())) e.ra_region = 'region';
    const n = Number(f.ra_session_seconds);
    if (!Number.isInteger(n) || n < 900 || n > 43200) e.ra_session_seconds = 'session';
  }
  if (f.auth_mode === 'access_key' && !ACCESS_KEY_RE.test(f.access_key_id.trim()))
    e.access_key_id = 'accessKey';
  const token = f.session_token.replace(/\s+/g, '');
  if (
    f.auth_mode === 'access_key' &&
    token &&
    !f.clear_session_token &&
    !SESSION_TOKEN_RE.test(token)
  )
    e.session_token = 'sessionToken';
  return e;
}

/** What the test needs besides valid fields (shown as a hint; Test still runs and reports). */
export function connMissing(
  f: ConnForm,
  s: Pick<AssistantSettings, 'certificate' | 'secret_access_key_set' | 'session_token_set'>,
): string[] {
  const miss: string[] = [];
  if (f.auth_mode === 'roles_anywhere' && !s.certificate) miss.push('certificate');
  if (
    f.auth_mode === 'access_key' &&
    (f.clear_secret || (!s.secret_access_key_set && !f.secret_access_key))
  )
    miss.push('secret');
  if (
    f.auth_mode === 'access_key' &&
    isTemporaryKey(f.access_key_id) &&
    (f.clear_session_token || (!s.session_token_set && !f.session_token.trim()))
  )
    miss.push('sessionToken');
  return miss;
}

/** All ARNs of a Roles Anywhere setup must be in the same AWS account (warning only). */
export function arnAccountsDiffer(f: ConnForm): boolean {
  const ids = [f.ra_trust_anchor_arn, f.ra_profile_arn, f.ra_role_arn]
    .map((a) => /:(\d{12}):/.exec(a)?.[1])
    .filter((x): x is string => !!x);
  return new Set(ids).size > 1;
}

// --- model / profile geography ---------------------------------------------------------

export type ProfileScope =
  'us' | 'eu' | 'apac' | 'jp' | 'au' | 'ca' | 'us-gov' | 'global' | 'regional' | 'arn' | 'none';

/**
 * Data-location class of a model id (owner decision: show a short explanation per profile prefix,
 * warn for `global.`). `regional` = a bare foundation-model id (processed in the region itself).
 */
export function profileScope(modelId: string): ProfileScope {
  const m = modelId.trim().toLowerCase();
  if (!m) return 'none';
  if (m.startsWith('arn:')) return 'arn';
  const prefix = m.split('.')[0];
  switch (prefix) {
    case 'us':
    case 'eu':
    case 'apac':
    case 'jp':
    case 'au':
    case 'ca':
    case 'us-gov':
    case 'global':
      return prefix;
    default:
      return 'regional';
  }
}

// --- certificates, usage, money --------------------------------------------------------

export function daysUntil(iso: string | null | undefined, now: number = Date.now()): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.floor((t - now) / 86_400_000);
}

export function certTone(days: number | null): 'ok' | 'warn' | 'error' {
  if (days === null) return 'ok';
  if (days < 0) return 'error';
  return days < 30 ? 'warn' : 'ok';
}

/** "≈ $0.042" style estimate with the configured currency label. */
export function formatCost(
  v: number | null | undefined,
  currency: string | null | undefined,
): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const digits = v === 0 ? 2 : v < 0.01 ? 4 : v < 1 ? 3 : 2;
  const n = v.toFixed(digits);
  const c = (currency || 'USD').trim();
  return c === 'USD' ? `$${n}` : `${n} ${c}`;
}

/** 1234567 → "1.2M", 12345 → "12.3k". */
export function compactTokens(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(n >= 100_000 ? 0 : 1)}k`;
  return String(n);
}

export function pctTone(pct: number, stopPct = 80): 'ok' | 'warn' | 'error' {
  if (pct >= 100) return 'error';
  return pct >= stopPct ? 'warn' : 'ok';
}

/** Total tokens of a message as the caps count them (cache reads at 10 %). */
export function messageTokens(u: AssistantMessage['usage'] | null | undefined): number {
  if (!u) return 0;
  return (
    u.input_tokens + u.output_tokens + u.cache_write_tokens + Math.round(u.cache_read_tokens * 0.1)
  );
}

/** "2026-11" for a Date (local calendar month). */
export function monthKey(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}
export function shiftMonth(month: string, delta: number): string {
  const [y, m] = month.split('-').map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  return monthKey(d);
}

/** Parses "10.0.0.0/8, 192.168.0.0/16" (comma/space/newline separated); returns list + bad items. */
export function parseCidrs(text: string): { items: string[]; bad: string[] } {
  const items: string[] = [];
  const bad: string[] = [];
  for (const raw of text.split(/[\s,]+/)) {
    const v = raw.trim();
    if (!v) continue;
    const v4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/(\d{1,2})$/.exec(v);
    const ok4 = v4 && v4.slice(1, 5).every((o) => Number(o) <= 255) && Number(v4[5]) <= 32;
    const v6 = /^[0-9a-f:]+\/(\d{1,3})$/i.exec(v);
    const ok6 = v6 && v.includes(':') && Number(v6[1]) <= 128;
    if (ok4 || ok6) items.push(v);
    else bad.push(v);
  }
  return { items, bad };
}

/** Plain-language label of a tool step's arguments ("since: …, device_id: 12"). */
export function argsSummary(args: Record<string, unknown>, max = 120): string {
  const s = Object.entries(args)
    .map(([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join(', ');
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

/** Splits the description of a question's per-day remaining questions. */
export function remainingQuestions(
  me: { questions_today: number; daily_limit: number } | undefined,
): number | null {
  if (!me) return null;
  return Math.max(0, me.daily_limit - me.questions_today);
}
