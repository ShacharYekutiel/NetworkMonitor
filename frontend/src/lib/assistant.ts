// [3b] Pure helpers of the assistant UI (architecture.md § 14.3, § 14.10).
import type {
  AssistantConnectionInput,
  AssistantMessage,
  AssistantSettings,
  AssistantSource,
  AssistantStep,
  AwsAccess,
  AwsAccessMode,
  AwsAuthMode,
  Features,
  StreamEvent,
} from '../api/types';
import { accessErrors, isHttpsUrl, normalizeAccess } from './aws';
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

/**
 * [4] The assistant's connection form: the Bedrock fields plus the AWS connection and the access
 * (the authentication itself lives in Settings → AWS; architecture § 15.1.10).
 */
export type ConnForm = {
  region: string;
  model_id: string;
  model_id_light: string;
  endpoint_url: string;
  control_endpoint_url: string;
  prompt_caching: boolean;
  /** '' = the default connection (null in the API) */
  connection_id: string;
  access_mode: AwsAccessMode;
  role_arn: string;
  external_id: string;
};

/** Owner decision: default setup is us-east-1 with a `us.` inference profile. */
export const DEFAULT_REGION = 'us-east-1';
export const SUGGESTED_REGIONS = ['us-east-1', 'us-west-2', 'il-central-1', 'eu-central-1'];

export function connFormFrom(s: AssistantSettings): ConnForm {
  const a = s.access ?? { mode: 'direct', role_arn: null, external_id: null };
  return {
    region: s.region || DEFAULT_REGION,
    model_id: s.model_id ?? '',
    model_id_light: s.model_id_light ?? '',
    endpoint_url: s.endpoint_url ?? '',
    control_endpoint_url: s.control_endpoint_url ?? '',
    prompt_caching: s.prompt_caching,
    connection_id:
      s.connection_id === null || s.connection_id === undefined ? '' : String(s.connection_id),
    access_mode: a.mode,
    role_arn: a.role_arn ?? '',
    external_id: a.external_id ?? '',
  };
}

const orNull = (v: string) => (v.trim() ? v.trim() : null);

export function formAccess(f: ConnForm): AwsAccess {
  return normalizeAccess({ mode: f.access_mode, role_arn: f.role_arn, external_id: f.external_id });
}

/** The writable connection fields of the form (request body of PUT / Test). */
export function connInput(f: ConnForm): AssistantConnectionInput {
  return {
    region: f.region.trim(),
    model_id: f.model_id.trim(),
    model_id_light: orNull(f.model_id_light),
    endpoint_url: orNull(f.endpoint_url),
    control_endpoint_url: orNull(f.control_endpoint_url),
    prompt_caching: f.prompt_caching,
    connection_id: f.connection_id ? Number(f.connection_id) : null,
    access: formAccess(f),
  };
}

/**
 * Stable key of the connection values a test covered: any edit of a connection field locks the
 * bottom switch again (architecture § 14.2.6). `connVersion` changes when the AWS connection the
 * assistant uses changes (certificate, keys).
 */
export function connKey(f: ConnForm, connVersion = ''): string {
  return JSON.stringify([connInput(f), connVersion]);
}

export type ConnErrors = Partial<Record<keyof ConnForm, string>>;
const REGION_RE = /^[a-z]{2}(-gov)?-[a-z]+-\d$/;
const MODEL_RE = /^[A-Za-z0-9.:/_-]{1,256}$/;

export { isHttpsUrl } from './aws';

/** Client-side checks mirroring data-model § [3b] Settings + [4] consumer access. */
export function connErrors(
  f: ConnForm,
  authMode: AwsAuthMode | null = null,
  profileArn?: string | null,
): ConnErrors {
  const e: ConnErrors = {};
  if (!REGION_RE.test(f.region.trim())) e.region = 'region';
  if (!MODEL_RE.test(f.model_id.trim())) e.model_id = 'model';
  if (f.model_id_light.trim() && !MODEL_RE.test(f.model_id_light.trim()))
    e.model_id_light = 'model';
  for (const k of ['endpoint_url', 'control_endpoint_url'] as const)
    if (f[k].trim() && !isHttpsUrl(f[k].trim())) e[k] = 'url';
  const ae = accessErrors(formAccess(f), authMode, profileArn);
  if (ae.mode) e.access_mode = ae.mode;
  if (ae.role_arn) e.role_arn = ae.role_arn;
  if (ae.external_id) e.external_id = ae.external_id;
  return e;
}

/** What the test needs besides valid fields (shown as a hint; Test still runs and reports). */
export function connMissing(f: ConnForm, connections: { id: number }[] | undefined): string[] {
  const miss: string[] = [];
  if (connections && connections.length === 0) miss.push('connection');
  void f;
  return miss;
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

// --- [3b owner] chat model choice ------------------------------------------------------

/**
 * A readable name for a Bedrock model / inference-profile id: `us.anthropic.claude-sonnet-4-5-
 * 20250929-v1:0` → "Claude Sonnet 4.5", `anthropic.claude-3-5-haiku-…` → "Claude 3.5 Haiku";
 * ARNs use their last segment; anything that is not a Claude id is returned as is.
 */
export function friendlyModelName(id: string): string {
  const last = id.slice(id.lastIndexOf('/') + 1);
  const at = last.indexOf('claude-');
  if (at < 0) return id;
  const rest = last
    .slice(at + 'claude-'.length)
    .replace(/-v\d+(:\d+)?$/, '')
    .replace(/:\d+$/, '');
  const parts = rest.split('-').filter((p) => p && !/^\d{8}$/.test(p));
  if (!parts.length) return id;
  const words: string[] = [];
  for (const p of parts) {
    const prev = words[words.length - 1];
    if (/^\d{1,2}$/.test(p) && prev !== undefined && /^\d+(\.\d+)*$/.test(prev))
      words[words.length - 1] = `${prev}.${p}`;
    else words.push(/^\d/.test(p) ? p : p.charAt(0).toUpperCase() + p.slice(1));
  }
  return `Claude ${words.join(' ')}`;
}

/** Options of the composer's model select: the label, else the friendly name; names shared
 *  by several ids get the profile prefix (`us`, `global`…) appended. */
export function chatModelOptions(
  models: { id: string; label: string | null }[],
): { id: string; name: string }[] {
  const base = models.map((m) => ({ id: m.id, name: m.label?.trim() || friendlyModelName(m.id) }));
  const count = new Map<string, number>();
  for (const o of base) count.set(o.name, (count.get(o.name) ?? 0) + 1);
  return base.map((o) => {
    if ((count.get(o.name) ?? 0) < 2) return o;
    const last = o.id.slice(o.id.lastIndexOf('/') + 1);
    const dot = last.indexOf('.');
    const prefix = dot > 0 && !last.startsWith('anthropic.') ? last.slice(0, dot) : '';
    return { id: o.id, name: prefix ? `${o.name} (${prefix})` : `${o.name} (${o.id})` };
  });
}

/** The model to preselect: the remembered one when still allowed, else the default (main). */
export function pickChatModel(
  allowed: { id: string }[],
  remembered: string | null,
  defaultId: string | null | undefined,
): string | null {
  if (!allowed.length) return null;
  if (remembered && allowed.some((m) => m.id === remembered)) return remembered;
  if (defaultId && allowed.some((m) => m.id === defaultId)) return defaultId;
  return allowed[0].id;
}

export const CHAT_MODEL_KEY = 'nm.assistant.chatModel';

/** localStorage key of the remembered chat model (per user). */
export function chatModelKey(userId: number | null | undefined): string {
  return `${CHAT_MODEL_KEY}.${userId ?? 0}`;
}

export function readChatModel(userId: number | null | undefined): string | null {
  try {
    return localStorage.getItem(chatModelKey(userId));
  } catch {
    return null;
  }
}

export function rememberChatModel(userId: number | null | undefined, id: string): void {
  try {
    localStorage.setItem(chatModelKey(userId), id);
  } catch {
    /* storage unavailable (private mode, blocked): not remembered */
  }
}
