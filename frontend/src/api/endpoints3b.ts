// [3b] Assistant endpoints (docs/api.md "[3b] Phase 3b contract"). Paths and fields are exact.
import { api, ApiError, buildUrl, parseError, type Query } from './client';
import type { DeviceTemplate, Page, TemplateLibraryStatus } from './types';
import type {
  AssistantConversation,
  AssistantMessage,
  AssistantSettings,
  AssistantSettingsInput,
  AssistantStatus,
  AssistantTestResult,
  CertInfo,
  ConversationDetail,
  CsrRequest,
  CsrResult,
  DiffExplanation,
  DiffFlag,
  Digest,
  DigestSummary,
  Incident,
  IncidentDetail,
  IncidentState,
  KnowledgeInput,
  KnowledgeSettings,
  KnowledgeTestResult,
  ModelList,
  RuleDraft,
  RuleDraftKind,
  SnmpWalk,
  StreamEvent,
  TemplateDraft,
  UsageReport,
  WalkPreset,
} from './types3b';
import { SseParser } from '../lib/sse';
import { toStreamEvent } from '../lib/assistant';

export const qk3b = {
  settings: ['assistant-settings'] as const,
  status: ['assistant-status'] as const,
  knowledge: ['assistant-knowledge'] as const,
  conversations: (q: object) => ['assistant-conversations', q] as const,
  conversation: (id: number) => ['assistant-conversation', id] as const,
  usage: (month: string) => ['assistant-usage', month] as const,
  incidents: (q: object) => ['incidents', q] as const,
  incident: (id: number) => ['incident', id] as const,
  digests: (q: object) => ['assistant-digests', q] as const,
  digest: (day: string) => ['assistant-digest', day] as const,
  diffFlags: (device: number, from: number, to: number) =>
    ['assistant-diff-flags', device, from, to] as const,
  walk: (id: number) => ['assistant-walk', id] as const,
};

export type ConversationQuery = { q?: string; kind?: string; page?: number; page_size?: number };
export type IncidentQuery = {
  state?: IncidentState[];
  since?: string;
  page?: number;
  page_size?: number;
};

async function postForm<T>(path: string, fd: FormData): Promise<T> {
  let res: Response;
  try {
    res = await fetch(buildUrl(path), {
      method: 'POST',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      body: fd,
    });
  } catch (e) {
    throw new ApiError(0, 'network_error', e instanceof Error ? e.message : 'Network error');
  }
  if (!res.ok) throw await parseError(res);
  return (await res.json()) as T;
}

export type StreamHandlers = {
  onEvent: (ev: StreamEvent) => void;
  signal?: AbortSignal;
};

/**
 * Reads an SSE response (`POST /ask`, `POST /explain`, `GET /messages/{id}/stream`) with fetch +
 * ReadableStream. Pre-stream errors (JSON) throw an ApiError; a connection that ends without a
 * `done` event resolves `false` (the caller re-attaches with Last-Event-ID).
 */
export async function readStream(
  method: 'GET' | 'POST',
  path: string,
  body: unknown,
  h: StreamHandlers & { lastEventId?: number | null },
): Promise<boolean> {
  const headers: Record<string, string> = { Accept: 'text/event-stream' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (h.lastEventId != null) headers['Last-Event-ID'] = String(h.lastEventId);
  let res: Response;
  try {
    res = await fetch(buildUrl(path), {
      method,
      credentials: 'same-origin',
      cache: 'no-store',
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: h.signal,
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    throw new ApiError(0, 'network_error', e instanceof Error ? e.message : 'Network error');
  }
  if (!res.ok) throw await parseError(res);
  const ctype = res.headers.get('content-type') ?? '';
  if (!ctype.includes('text/event-stream')) {
    // defensive: a JSON body where a stream was expected
    const text = await res.text();
    throw new ApiError(res.status, 'internal_error', text.slice(0, 200) || 'Unexpected response');
  }
  if (!res.body) return false;
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  const parser = new SseParser();
  let done = false;
  try {
    for (;;) {
      const r = await reader.read();
      if (r.done) break;
      for (const raw of parser.push(dec.decode(r.value, { stream: true }))) {
        const ev = toStreamEvent(raw);
        if (!ev) continue;
        h.onEvent(ev);
        if (ev.event === 'done') done = true;
      }
    }
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    return done; // connection cut: re-attach
  } finally {
    try {
      reader.releaseLock();
    } catch {
      /* ignore */
    }
  }
  return done;
}

export const endpoints3b = {
  // settings, test, certificate
  settings: () => api.get<AssistantSettings>('/assistant/settings'),
  updateSettings: (body: AssistantSettingsInput) =>
    api.put<AssistantSettings>('/assistant/settings', body),
  test: (settings?: Record<string, unknown>) =>
    api.post<AssistantTestResult>('/assistant/test', settings ? { settings } : {}),
  uploadCert: (file: File, key: File | null, password: string) => {
    const fd = new FormData();
    fd.append('file', file);
    if (key) fd.append('key', key);
    if (password) fd.append('password', password);
    return postForm<CertInfo>('/assistant/cert', fd);
  },
  deleteCert: () => api.del('/assistant/cert'),
  /** [3b owner decision] see CsrRequest */
  createCsr: (body: CsrRequest) => api.post<CsrResult>('/assistant/cert/csr', body),
  csrDownloadUrl: () => buildUrl('/assistant/cert/csr'),
  deleteCsr: () => api.del('/assistant/cert/csr'),
  models: (settings?: Record<string, unknown>) =>
    api.post<ModelList>('/assistant/models', settings ? { settings } : {}),
  status: () => api.get<AssistantStatus>('/assistant/status'),

  // ask, stream, conversations
  ask: (conversationId: number | null, question: string, h: StreamHandlers) =>
    readStream('POST', '/assistant/ask', { conversation_id: conversationId, question }, h),
  attach: (messageId: number, lastEventId: number | null, h: StreamHandlers) =>
    readStream('GET', `/assistant/messages/${messageId}/stream`, undefined, { ...h, lastEventId }),
  cancel: (messageId: number) =>
    api.post<{ status: string }>(`/assistant/messages/${messageId}/cancel`),
  feedback: (messageId: number, rating: 1 | -1 | 0, comment: string | null) =>
    api.post<AssistantMessage>(`/assistant/messages/${messageId}/feedback`, { rating, comment }),
  conversations: (q: ConversationQuery = {}) =>
    api.get<Page<AssistantConversation>>('/assistant/conversations', q as Query),
  conversation: (id: number) => api.get<ConversationDetail>(`/assistant/conversations/${id}`),
  renameConversation: (id: number, title: string) =>
    api.patch<AssistantConversation>(`/assistant/conversations/${id}`, { title }),
  deleteConversation: (id: number) => api.del(`/assistant/conversations/${id}`),
  deleteAllConversations: () => api.del<{ deleted: number }>('/assistant/conversations'),

  // AI features
  explain: (
    body: { sensor_id: number; event_id: number | null; refresh: boolean },
    h: StreamHandlers,
  ) => readStream('POST', '/assistant/explain', body, h),
  incidents: (q: IncidentQuery = {}) => api.get<Page<Incident>>('/incidents', q as Query),
  incident: (id: number) => api.get<IncidentDetail>(`/incidents/${id}`),
  summarizeIncident: (id: number) => api.post<Incident>(`/incidents/${id}/summarize`),
  explainDiff: (body: { device_id: number; from: number; to: number; refresh: boolean }) =>
    api.post<DiffExplanation>('/assistant/config-diff/explain', body),
  diffFlags: (device_id: number, from: number, to: number) =>
    api.get<{ flags: DiffFlag[] }>('/assistant/config-diff/flags', { device_id, from, to }),
  digests: (q: { page?: number; page_size?: number } = {}) =>
    api.get<Page<DigestSummary>>('/assistant/digests', q),
  digest: (day: string) => api.get<Digest>(`/assistant/digests/${day}`),
  runDigest: (day: string, send: boolean) =>
    api.post<Digest>(`/assistant/digests/${day}/run`, { send }),
  ruleDraft: (kind: RuleDraftKind, description: string) =>
    api.post<RuleDraft>('/assistant/rule-drafts', { kind, description }),
  startWalk: (body: { device_id: number; presets: WalkPreset[]; oids: string[] }) =>
    api.post<SnmpWalk>('/assistant/template-drafts/walks', body),
  walk: (id: number) => api.get<SnmpWalk>(`/assistant/template-drafts/walks/${id}`),
  cancelWalk: (id: number) => api.post<SnmpWalk>(`/assistant/template-drafts/walks/${id}/cancel`),
  templateDraft: (body: { walk_id: number; template_id: string | null; notes: string | null }) =>
    api.post<TemplateDraft>('/assistant/template-drafts', body),
  saveUserTemplate: (yaml: string, overwrite: boolean) =>
    api.post<{ template: DeviceTemplate; file: string; library: TemplateLibraryStatus }>(
      '/device-templates/user',
      { yaml, overwrite },
    ),
  knowledge: () => api.get<KnowledgeSettings>('/assistant/knowledge'),
  updateKnowledge: (body: Partial<KnowledgeInput> & { enabled?: boolean; test_id?: string }) =>
    api.put<KnowledgeSettings>('/assistant/knowledge', body),
  testKnowledge: (settings?: Partial<KnowledgeInput>) =>
    api.post<KnowledgeTestResult>('/assistant/knowledge/test', settings ? { settings } : {}),
  usage: (month?: string) => api.get<UsageReport>('/assistant/usage', { month }),
};
