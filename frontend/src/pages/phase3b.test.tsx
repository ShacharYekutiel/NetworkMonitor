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
import type { AssistantMessage, AssistantSettings, AwsConnection } from '../api/types';
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

function conn(id: number, name: string, auth: AwsConnection['auth_mode'], isDefault = false) {
  return {
    id,
    name,
    is_default: isDefault,
    auth_mode: auth,
    region: 'us-east-1',
    ra_profile_arn:
      auth === 'roles_anywhere' ? 'arn:aws:rolesanywhere:us-east-1:111111111111:profile/p-1' : null,
    certificate: null,
    updated_at: '2026-11-01T00:00:00Z',
  } as unknown as AwsConnection;
}

const hopFailure = {
  ok: false,
  test_id: null,
  expires_at: null,
  steps: [
    {
      step: 'config',
      hop: null,
      ok: true,
      detail: 'auth access_key',
      ms: 1,
      error_code: null,
      hints: [],
    },
    {
      step: 'credentials',
      hop: 0,
      ok: true,
      detail: 'source static',
      ms: 2,
      error_code: null,
      hints: [],
    },
    {
      step: 'identity',
      hop: 0,
      ok: true,
      detail: 'account 111111111111',
      ms: 3,
      error_code: null,
      hints: [],
    },
    {
      step: 'hop',
      hop: 1,
      ok: false,
      detail:
        'User: arn:aws:sts::111111111111:assumed-role/netmon-base/1a is not authorized to perform: sts:SetSourceIdentity',
      ms: 4,
      error_code: 'aws_assume_role_denied',
      hints: [
        {
          code: 'set_source_identity',
          text: 'server text',
          values: {
            source_identity: 'CN=netmonitor.example.net',
            from_arn: 'arn:aws:sts::111111111111:assumed-role/netmon-base/1a',
            role_arn: 'arn:aws:iam::222222222222:role/netmon-bedrock',
          },
        },
      ],
    },
  ],
  via: 'direct',
  endpoint_host: '',
  address_class: null,
  model_id: 'us.anthropic.claude-x-v1:0',
  time_to_first_event_ms: null,
  usage: null,
  warnings: [],
  error: { code: 'aws_assume_role_denied', message: 'denied' },
};

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
  region: 'us-east-1',
  model_id: 'us.anthropic.claude-x-v1:0',
  model_id_light: null,
  endpoint_url: null,
  control_endpoint_url: null,
  prompt_caching: true,
  // [4] an installation upgraded from 1.4.x: the migrated "Default" connection, direct
  connection_id: null,
  access: { mode: 'direct', role_arn: null, external_id: null },
  connection: {
    id: 1,
    name: 'Default',
    auth_mode: 'roles_anywhere',
    is_default: true,
    account: '111111111111',
    arn: 'arn:aws:sts::111111111111:assumed-role/netmon-bedrock/1234',
    state: 'ok',
  },
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

  it('[4] AWS access: an upgraded install shows "Default — direct"; a hop shows its steps and hints', async () => {
    let lastBody: Record<string, unknown> = {};
    mockFetch((url, init) => {
      if (url.startsWith('/api/aws/connections') && (!init?.method || init.method === 'GET'))
        return {
          status: 200,
          body: [conn(1, 'Default', 'roles_anywhere', true), conn(2, 'Keys', 'access_key')],
        };
      if (url.startsWith('/api/aws/external-id'))
        return { status: 200, body: { external_id: 'Gen0123456789Gen0123456789abcdef' } };
      if (url.startsWith('/api/assistant/test') && init?.method === 'POST') {
        lastBody = (JSON.parse(String(init.body ?? '{}')) as { settings: Record<string, unknown> })
          .settings;
        return { status: 200, body: hopFailure };
      }
      return baseRoutes(url);
    });
    render(wrap(<ConnectionSettings s={settings} auditOn />));
    // zero action after the upgrade: the default connection, used directly
    expect(screen.getByTestId('assistant-aws-current').textContent).toMatch(
      /Default — Use the connection directly/,
    );
    await waitFor(() =>
      expect(
        (screen.getByTestId('access-connection') as HTMLSelectElement).options[0].textContent,
      ).toBe('Default connection (Default)'),
    );
    expect((screen.getByTestId('access-direct') as HTMLInputElement).checked).toBe(true);
    // ra_role is offered for the Roles Anywhere default, not for the access-key connection
    expect(screen.queryByTestId('access-ra_role')).toBeTruthy();
    fireEvent.change(screen.getByTestId('access-connection'), { target: { value: '2' } });
    expect(screen.queryByTestId('access-ra_role')).toBeNull();
    // Assume a role: role ARN (doubled ARN refused), ExternalId with Generate
    fireEvent.click(screen.getByTestId('access-assume_role'));
    const role = screen.getByTestId('access-role-arn');
    fireEvent.change(role, {
      target: { value: 'arn:aws:iam::222222222222:role/arn:aws:iam::222222222222:role/b' },
    });
    expect(screen.getByText('The ARN appears twice — paste it once.')).toBeTruthy();
    expect((screen.getByTestId('test-connection') as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(role, { target: { value: 'arn:aws:iam::222222222222:role/netmon-bedrock' } });
    fireEvent.click(screen.getByTestId('external-id-generate'));
    await waitFor(() =>
      expect((screen.getByTestId('external-id') as HTMLInputElement).value).toBe(
        'Gen0123456789Gen0123456789abcdef',
      ),
    );
    fireEvent.click(screen.getByTestId('test-connection'));
    await waitFor(() => expect(screen.getByTestId('test-result')).toBeTruthy());
    expect(lastBody).toMatchObject({
      connection_id: 2,
      access: {
        mode: 'assume_role',
        role_arn: 'arn:aws:iam::222222222222:role/netmon-bedrock',
        external_id: 'Gen0123456789Gen0123456789abcdef',
      },
    });
    expect('auth_mode' in lastBody).toBe(false);
    const hopStep = document.querySelector('[data-step="hop"]')!;
    expect(hopStep.getAttribute('data-ok')).toBe('false');
    expect(hopStep.getAttribute('data-hop')).toBe('1');
    expect(document.querySelector('[data-step="identity"]')!.getAttribute('data-ok')).toBe('true');
    const hint = hopStep.querySelector('[data-hint="set_source_identity"]')!;
    expect(hint.textContent).toMatch(/sts:SetSourceIdentity in two places/);
    expect(hint.querySelector('[data-value="source_identity"]')!.textContent).toBe(
      'CN=netmonitor.example.net',
    );
    expect((screen.getByRole('switch') as HTMLButtonElement).disabled).toBe(true);
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
