import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { ReactNode } from 'react';
import i18n from '../i18n';
import { AuthProvider } from '../hooks/auth';
import { ThemeProvider } from '../hooks/theme';
import { ToastProvider } from '../components/toast';
import { AnswerBlock } from '../components/assistant/AnswerView';
import { ConnectionSettings } from '../components/assistant/ConnectionSettings';
import { RuleDraftBox } from '../components/assistant/RuleDraft';
import type { AssistantMessage, AssistantSettings } from '../api/types';
import { EMPTY_LIVE } from '../lib/assistant';

function mockFetch(
  route: (url: string, init?: RequestInit) => { status: number; body?: unknown } | undefined,
) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const r = route(url, init) ?? {
      status: 404,
      body: { error: { code: 'not_found', message: 'nf', details: null } },
    };
    return new Response(r.body === undefined ? null : JSON.stringify(r.body), {
      status: r.status,
      headers: { 'Content-Type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

function wrap(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <ToastProvider>
          <MemoryRouter>
            <AuthProvider>{children}</AuthProvider>
          </MemoryRouter>
        </ToastProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}

const baseRoutes = (url: string) => {
  if (url.startsWith('/api/setup/status')) return { status: 200, body: { setup_required: false } };
  if (url.startsWith('/api/auth/me'))
    return {
      status: 200,
      body: { user: { id: 1, username: 'admin', role: 'admin', language: 'en', is_active: true } },
    };
  if (url.startsWith('/api/settings/public'))
    return { status: 200, body: { default_language: 'en', features: { audit: true } } };
  return undefined;
};

beforeEach(async () => {
  await i18n.changeLanguage('en');
});
afterEach(() => {
  vi.unstubAllGlobals();
});

const message: AssistantMessage = {
  id: 340,
  conversation_id: 12,
  seq: 2,
  role: 'assistant',
  purpose: 'ask',
  content:
    'Two sensors are down: [core-sw / Ping](nm:sensor/7) and ![x](https://evil.example/a.png) [x](https://evil.example)',
  notes: [{ round: 1, text: 'Checking problems…' }],
  steps: [
    {
      call_id: 't1',
      round: 1,
      tool: 'list_problems',
      label_key: 'assistant.tool.list_problems',
      args: {},
      state: 'done',
      ok: true,
      rows: 2,
      truncated: false,
      ms: 41,
      error: null,
    },
  ],
  sources: [{ ref: 'sensor/7', label: 'core-sw / Ping', url: null }],
  status: 'complete',
  error: null,
  notices: [],
  model_id: 'us.anthropic.claude-x',
  model_calls: 2,
  tool_calls: 1,
  usage: { input_tokens: 1000, output_tokens: 200, cache_read_tokens: 0, cache_write_tokens: 0 },
  cost_estimate: 0.012,
  currency: 'USD',
  duration_ms: 3200,
  feedback: null,
  feedback_comment: null,
  created_at: '2026-11-10T08:00:00Z',
  finished_at: '2026-11-10T08:00:03Z',
};

describe('[3b] answer rendering', () => {
  it('renders steps, the nm link, no image and no external link', async () => {
    mockFetch(baseRoutes);
    const { container } = render(wrap(<AnswerBlock message={message} />));
    expect(await screen.findByText('Listing problems…')).toBeTruthy();
    expect(screen.getByText(/2 rows · 41 ms/)).toBeTruthy();
    const links = [...container.querySelectorAll('a')].map((a) => a.getAttribute('href'));
    expect(links).toContain('/sensors/7');
    expect(links.some((h) => h?.startsWith('https://evil'))).toBe(false);
    expect(container.querySelector('img')).toBeNull();
  });

  it('shows Stop while streaming and the retryable error with Retry', () => {
    mockFetch(baseRoutes);
    const onStop = vi.fn();
    const onRetry = vi.fn();
    const { rerender } = render(
      wrap(
        <AnswerBlock
          live={{ ...EMPTY_LIVE, messageId: 1, text: 'Partial' }}
          running
          onStop={onStop}
        />,
      ),
    );
    fireEvent.click(screen.getByTestId('stop'));
    expect(onStop).toHaveBeenCalled();
    rerender(
      wrap(
        <AnswerBlock
          live={{ ...EMPTY_LIVE, messageId: 1, error: { code: 'bedrock_throttled', message: 'x' } }}
          onRetry={onRetry}
        />,
      ),
    );
    expect(screen.getByTestId('answer-error').getAttribute('data-code')).toBe('bedrock_throttled');
    fireEvent.click(screen.getByText('Retry'));
    expect(onRetry).toHaveBeenCalled();
  });
});

const settings: AssistantSettings = {
  enabled: false,
  state: 'disabled',
  state_error: null,
  auth_mode: 'access_key',
  region: 'us-east-1',
  model_id: 'us.anthropic.claude-x-v1:0',
  model_id_light: null,
  endpoint_url: null,
  control_endpoint_url: null,
  proxy_mode: 'env',
  proxy_url: null,
  prompt_caching: true,
  access_key_id: 'AKIAABCDEFGHIJKLMNOP',
  secret_access_key: null,
  secret_access_key_set: true,
  secret_access_key_unreadable: false,
  access_key_created_at: null,
  ra_region: null,
  ra_endpoint_url: null,
  ra_trust_anchor_arn: null,
  ra_profile_arn: null,
  ra_role_arn: null,
  ra_session_seconds: 3600,
  certificate: null,
  send_config_excerpts: false,
  redact_internal_ips: false,
  internal_cidrs: [],
  chat_redact_ips: true,
  prices: {
    currency: 'USD',
    input_per_mtok: null,
    output_per_mtok: null,
    cache_read_per_mtok: null,
    cache_write_per_mtok: null,
  },
  last_test: null,
  warnings: [],
};

describe('[3b] configure-first connection form', () => {
  it('locks the bottom switch until a test of the current values succeeded; an edit locks it again', async () => {
    mockFetch((url, init) => {
      if (url.startsWith('/api/assistant/test') && init?.method === 'POST')
        return {
          status: 200,
          body: {
            ok: true,
            test_id: 'abc',
            expires_at: new Date(Date.now() + 600_000).toISOString(),
            steps: [{ step: 'converse', ok: true, detail: 'ok', ms: 120, error_code: null }],
            via: 'direct',
            endpoint_host: 'bedrock-runtime.us-east-1.amazonaws.com',
            address_class: 'public',
            model_id: 'us.anthropic.claude-x-v1:0',
            time_to_first_event_ms: 300,
            usage: { input_tokens: 10, output_tokens: 5 },
            warnings: [],
            error: null,
          },
        };
      return baseRoutes(url);
    });
    render(wrap(<ConnectionSettings s={settings} auditOn />));
    const sw = () => screen.getByRole('switch');
    expect((sw() as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId('enable-hint')).toBeTruthy();
    expect(screen.getByTestId('geo-note').getAttribute('data-scope')).toBe('us');
    fireEvent.click(screen.getByTestId('test-connection'));
    await waitFor(() => expect((sw() as HTMLButtonElement).disabled).toBe(false));
    fireEvent.change(screen.getByTestId('region'), { target: { value: 'il-central-1' } });
    expect((sw() as HTMLButtonElement).disabled).toBe(true);
  });

  it('temporary credentials: session token field (write-only), warning, ASIA without token', async () => {
    let lastBody: Record<string, unknown> = {};
    let answer: 'ok' | 'missing' = 'ok';
    mockFetch((url, init) => {
      if (url.startsWith('/api/assistant/test') && init?.method === 'POST') {
        const parsed = JSON.parse(String(init.body ?? '{}')) as {
          settings?: Record<string, unknown>;
        };
        lastBody = parsed.settings ?? {};
        if (answer === 'missing')
          return {
            status: 200,
            body: {
              ok: false,
              test_id: null,
              expires_at: null,
              steps: [
                { step: 'config', ok: true, detail: 'auth access_key', ms: 1, error_code: null },
                {
                  step: 'credentials',
                  ok: false,
                  detail: 'The access key id is a temporary key (ASIA…)',
                  ms: 1,
                  error_code: 'aws_session_token_required',
                },
              ],
              via: 'direct',
              endpoint_host: 'bedrock-runtime.us-east-1.amazonaws.com',
              address_class: null,
              model_id: 'us.anthropic.claude-x-v1:0',
              time_to_first_event_ms: null,
              usage: null,
              warnings: [],
              error: { code: 'aws_session_token_required', message: 'temporary key' },
            },
          };
        return {
          status: 200,
          body: {
            ok: true,
            test_id: 'tmp1',
            expires_at: new Date(Date.now() + 600_000).toISOString(),
            steps: [{ step: 'converse', ok: true, detail: 'ok', ms: 120, error_code: null }],
            via: 'direct',
            endpoint_host: 'bedrock-runtime.us-east-1.amazonaws.com',
            address_class: 'public',
            model_id: 'us.anthropic.claude-x-v1:0',
            time_to_first_event_ms: 300,
            usage: { input_tokens: 10, output_tokens: 5 },
            warnings: ['temporary_credentials'],
            error: null,
          },
        };
      }
      return baseRoutes(url);
    });
    const s: AssistantSettings = {
      ...settings,
      access_key_id: 'ASIAQWERTYUIOPASDFGH',
      session_token: null,
      session_token_set: true,
      session_token_unreadable: false,
      warnings: ['temporary_credentials'],
    };
    const { unmount } = render(wrap(<ConnectionSettings s={s} auditOn />));
    const field = screen.getByTestId('session-token-field');
    expect(field.getAttribute('data-set')).toBe('true');
    const ta = screen.getByTestId('session-token') as HTMLTextAreaElement;
    expect(ta.value).toBe('');
    expect(ta.placeholder).toMatch(/stored/);
    expect(document.querySelector('[data-warning="temporary_credentials"]')?.textContent).toMatch(
      /expire/,
    );
    const sw = () => screen.getByRole('switch') as HTMLButtonElement;
    fireEvent.click(screen.getByTestId('test-connection'));
    await waitFor(() => expect(sw().disabled).toBe(false));
    expect('session_token' in lastBody).toBe(false); // the stored token is kept, never sent back
    expect(screen.getByTestId('test-result').textContent).toMatch(/IAM user key, Roles Anywhere/);
    // a new token locks the switch again and is sent (whitespace removed)
    fireEvent.change(ta, { target: { value: 'IQoJb3Jp\nZ2luX2Vj==' } });
    expect(sw().disabled).toBe(true);
    fireEvent.click(screen.getByTestId('test-connection'));
    await waitFor(() => expect(lastBody.session_token).toBe('IQoJb3JpZ2luX2Vj=='));
    await waitFor(() => expect(sw().disabled).toBe(false));
    // "Clear" disables the textarea, sends "" and locks again
    fireEvent.click(screen.getByLabelText(/Clear the stored session token/));
    expect(ta.disabled).toBe(true);
    expect(sw().disabled).toBe(true);
    unmount();
    // ASIA without a stored token: hint + the credentials-step error text
    answer = 'missing';
    render(
      wrap(
        <ConnectionSettings
          s={{
            ...s,
            session_token_set: false,
            warnings: [],
            access_key_id: 'ASIAZXCVBNMLKJHGFDSA',
          }}
          auditOn
        />,
      ),
    );
    expect(screen.getByTestId('session-token-field').getAttribute('data-set')).toBe('false');
    expect(screen.getByText(/needs the session token\./)).toBeTruthy();
    fireEvent.click(screen.getByTestId('test-connection'));
    await waitFor(() =>
      expect(screen.getByTestId('test-result').textContent).toMatch(
        /temporary key \(ASIA…\): it needs the session token/,
      ),
    );
  });

  it('warns for a global. profile', () => {
    mockFetch(baseRoutes);
    render(
      wrap(
        <ConnectionSettings s={{ ...settings, model_id: 'global.anthropic.claude-x' }} auditOn />,
      ),
    );
    expect(screen.getByTestId('geo-note').getAttribute('data-scope')).toBe('global');
    expect(screen.getByTestId('geo-note').textContent).toMatch(/Warning/);
  });
});

describe('[3b owner] rule draft: cannot draft', () => {
  const routes = (draftBody: unknown) => (url: string) => {
    if (url.startsWith('/api/settings/public'))
      return {
        status: 200,
        body: { default_language: 'en', features: { assistant: true, ai_rule_drafts: true } },
      };
    if (url.startsWith('/api/assistant/rule-drafts')) return { status: 200, body: draftBody };
    return baseRoutes(url);
  };
  const cannot = {
    kind: 'event_rule',
    draft: null,
    cannot_draft: { reason: 'הבקשה לא מתארת אירוע רשת להתאמה.', source: 'model' },
    language: 'he',
    explanation: '',
    assumptions: [],
    validation: { ok: false, errors: [] },
    conversation_id: 5,
    usage: { input_tokens: 1, output_tokens: 1 },
  };

  it('shows the reason and an example, never prefills the form', async () => {
    mockFetch(routes(cannot));
    const onDraft = vi.fn();
    render(wrap(<RuleDraftBox kind="event_rule" onDraft={onDraft} />));
    fireEvent.click(await screen.findByTestId('describe-rule'));
    fireEvent.change(screen.getByTestId('describe-text'), { target: { value: 'תכין לי קפה' } });
    fireEvent.click(screen.getByTestId('draft-button'));
    expect(await screen.findByTestId('cannot-draft')).toBeTruthy();
    expect(screen.getByTestId('cannot-draft-reason').textContent).toBe(
      'הבקשה לא מתארת אירוע רשת להתאמה.',
    );
    expect(screen.getByText(/SSH login failure/)).toBeTruthy();
    expect(onDraft).not.toHaveBeenCalled();
    expect(screen.getByTestId('describe-text')).toBeTruthy(); // the box stays open
  });

  it('a real draft still goes to the form', async () => {
    mockFetch(
      routes({
        ...cannot,
        draft: { name: 'x', kind: 'syslog', match: { text: 'a' } },
        cannot_draft: null,
        validation: { ok: true, errors: [] },
      }),
    );
    const onDraft = vi.fn();
    render(wrap(<RuleDraftBox kind="event_rule" onDraft={onDraft} />));
    fireEvent.click(await screen.findByTestId('describe-rule'));
    fireEvent.change(screen.getByTestId('describe-text'), { target: { value: 'syslog fail' } });
    fireEvent.click(screen.getByTestId('draft-button'));
    await waitFor(() => expect(onDraft).toHaveBeenCalled());
    expect(screen.queryByTestId('cannot-draft')).toBeNull();
  });
});
