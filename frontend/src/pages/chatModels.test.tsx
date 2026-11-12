// [3b owner] usage per model, model choice in the chat composer, chat-models settings list.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { ReactNode } from 'react';
import i18n from '../i18n';
import { AuthProvider } from '../hooks/auth';
import { ThemeProvider } from '../hooks/theme';
import { ToastProvider } from '../components/toast';
import { ChatModelsCard, chatModelsErrors } from '../components/assistant/ChatModelsCard';
import type { AssistantSettings, UsageReport } from '../api/types';
import { chatModelKey, chatModelOptions, friendlyModelName, pickChatModel } from '../lib/assistant';
import { AssistantPage } from './AssistantPage';
import { AssistantUsagePage } from './AssistantUsagePage';

const MAIN = 'us.anthropic.claude-sonnet-5-5-20260901-v1:0';
const LIGHT = 'us.anthropic.claude-haiku-5-5-20260901-v1:0';

type Route = (url: string, init?: RequestInit) => { status: number; body?: unknown } | undefined;

function mockFetch(route: Route) {
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

const baseRoutes: Route = (url) => {
  if (url.startsWith('/api/setup/status')) return { status: 200, body: { setup_required: false } };
  if (url.startsWith('/api/auth/me'))
    return {
      status: 200,
      body: { user: { id: 7, username: 'admin', role: 'admin', language: 'en', is_active: true } },
    };
  if (url.startsWith('/api/settings/public'))
    return {
      status: 200,
      body: { default_language: 'en', features: { audit: true, assistant: true } },
    };
  return undefined;
};

const month = {
  month: '2026-11',
  tokens: 1_200_000,
  token_cap: 5_000_000,
  cost_estimate: 4.2,
  cost_cap: null,
  currency: 'USD',
  pct_of_cap: 24,
  projection_pct: 60,
  background_stopped: false,
};

beforeEach(async () => {
  await i18n.changeLanguage('en');
  localStorage.clear();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe('[3b owner] model helpers', () => {
  it('derives friendly names from model / profile ids', () => {
    expect(friendlyModelName(MAIN)).toBe('Claude Sonnet 5.5');
    expect(friendlyModelName(LIGHT)).toBe('Claude Haiku 5.5');
    expect(friendlyModelName('global.anthropic.claude-opus-4-1-20250805-v1:0')).toBe(
      'Claude Opus 4.1',
    );
    expect(friendlyModelName('anthropic.claude-3-5-sonnet-20240620-v1:0')).toBe(
      'Claude 3.5 Sonnet',
    );
    expect(
      friendlyModelName(
        'arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-sonnet-4-20250514-v1:0',
      ),
    ).toBe('Claude Sonnet 4');
    expect(friendlyModelName('amazon.nova-pro-v1:0')).toBe('amazon.nova-pro-v1:0');
  });

  it('labels win, equal names get the profile prefix; preselection falls back to the default', () => {
    const opts = chatModelOptions([
      { id: MAIN, label: null },
      { id: 'global.anthropic.claude-sonnet-5-5-20260901-v1:0', label: null },
      { id: LIGHT, label: 'Fast' },
    ]);
    expect(opts.map((o) => o.name)).toEqual([
      'Claude Sonnet 5.5 (us)',
      'Claude Sonnet 5.5 (global)',
      'Fast',
    ]);
    expect(pickChatModel(opts, LIGHT, MAIN)).toBe(LIGHT);
    expect(pickChatModel(opts, 'gone', MAIN)).toBe(MAIN);
    expect(pickChatModel(opts, null, null)).toBe(MAIN);
    expect(pickChatModel([], LIGHT, MAIN)).toBeNull();
  });

  it('validates the settings list', () => {
    expect(chatModelsErrors([{ id: MAIN, label: '' }])).toEqual({});
    expect(
      chatModelsErrors([
        { id: MAIN, label: '' },
        { id: MAIN, label: '' },
        { id: 'bad id', label: '' },
        { id: LIGHT, label: 'x'.repeat(61) },
      ]),
    ).toEqual({ 1: 'duplicate', 2: 'invalid', 3: 'label' });
  });
});

describe('[3b owner] usage per model', () => {
  it('renders the Per model card between Per feature and Top lookups', async () => {
    const report: UsageReport = {
      ...month,
      by_day: [], // no chart in jsdom (no canvas)
      by_user: [],
      by_purpose: [{ purpose: 'ask', calls: 5, tokens: 1_200_000, cost_estimate: 4.2 }],
      by_model: [
        {
          model_id: MAIN,
          calls: 4,
          input_tokens: 900_000,
          output_tokens: 100_000,
          cache_read_tokens: 50_000,
          cache_write_tokens: 0,
          tokens: 1_005_000,
          cost_estimate: 4.1,
        },
        {
          model_id: '',
          calls: 0,
          input_tokens: 300,
          output_tokens: 0,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
          tokens: 300,
          cost_estimate: null,
        },
      ],
      top_tools: [{ tool: 'list_problems', calls: 2, errors: 0, avg_ms: 40 }],
      errors: [],
      feedback: { up: 0, down: 0 },
      last_12_months: [],
    };
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe() {}
        disconnect() {}
      },
    );
    mockFetch((url) => {
      if (url.startsWith('/api/assistant/usage')) return { status: 200, body: report };
      return baseRoutes(url);
    });
    render(wrap(<AssistantUsagePage />));
    const card = await screen.findByTestId('usage-by-model');
    const rows = within(card).getAllByRole('row');
    expect(rows).toHaveLength(3);
    expect(rows[1].textContent).toContain('Claude Sonnet 5.5');
    expect(rows[1].textContent).toContain(MAIN);
    expect(rows[1].textContent).toContain('$4.10');
    expect(rows[1].textContent).toContain('cached');
    expect(rows[2].textContent).toContain('Unknown model');
    expect(rows[2].textContent).toContain('—');
    const titles = screen
      .getAllByText(/^(Per feature|Per model|Top lookups)$/)
      .map((e) => e.textContent);
    expect(titles).toEqual(['Per feature', 'Per model', 'Top lookups']);
  });
});

describe('[3b owner] model choice in the chat', () => {
  const status = {
    state: 'ready',
    state_error: null,
    tools: [],
    features: [],
    me: { questions_today: 0, daily_limit: 50, running_message_id: null },
    month,
    chat_models: [
      { id: MAIN, label: null },
      { id: LIGHT, label: null },
    ],
    default_model_id: MAIN,
  };

  function chatRoutes(asked: Record<string, unknown>[]): Route {
    return (url, init) => {
      if (url.startsWith('/api/assistant/status')) return { status: 200, body: status };
      if (url.startsWith('/api/assistant/conversations'))
        return { status: 200, body: { items: [], total: 0, page: 1, page_size: 100 } };
      if (url.startsWith('/api/assistant/ask') && init?.method === 'POST') {
        asked.push(JSON.parse(String(init.body)) as Record<string, unknown>);
        return {
          status: 429,
          body: { error: { code: 'assistant_busy', message: 'busy', details: null } },
        };
      }
      return baseRoutes(url);
    };
  }

  it('defaults to the main model, sends model_id and remembers the choice per user', async () => {
    const asked: Record<string, unknown>[] = [];
    mockFetch(chatRoutes(asked));
    const view = render(wrap(<AssistantPage />));
    const select = (await screen.findByTestId('chat-model')) as HTMLSelectElement;
    expect(select.value).toBe(MAIN);
    expect([...select.options].map((o) => o.text)).toEqual([
      'Claude Sonnet 5.5',
      'Claude Haiku 5.5',
    ]);
    fireEvent.change(select, { target: { value: LIGHT } });
    await waitFor(() => expect(localStorage.getItem(chatModelKey(7))).toBe(LIGHT));
    fireEvent.change(screen.getByTestId('question'), {
      target: { value: 'Which devices are down?' },
    });
    fireEvent.click(screen.getByText('Send'));
    await waitFor(() => expect(asked).toHaveLength(1));
    expect(asked[0]).toMatchObject({ question: 'Which devices are down?', model_id: LIGHT });
    view.unmount();

    // a new page load preselects the remembered model
    mockFetch(chatRoutes(asked));
    render(wrap(<AssistantPage />));
    await waitFor(() =>
      expect((screen.getByTestId('chat-model') as HTMLSelectElement).value).toBe(LIGHT),
    );
  });

  it('a remembered model that is no longer allowed falls back to the main model', async () => {
    localStorage.setItem(chatModelKey(7), 'us.anthropic.claude-removed-v1:0');
    mockFetch(chatRoutes([]));
    render(wrap(<AssistantPage />));
    await waitFor(() =>
      expect((screen.getByTestId('chat-model') as HTMLSelectElement).value).toBe(MAIN),
    );
  });

  it('works without localStorage (throwing storage)', async () => {
    const asked: Record<string, unknown>[] = [];
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    const spy2 = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    try {
      mockFetch(chatRoutes(asked));
      render(wrap(<AssistantPage />));
      const select = (await screen.findByTestId('chat-model')) as HTMLSelectElement;
      expect(select.value).toBe(MAIN);
      fireEvent.change(select, { target: { value: LIGHT } });
      expect(select.value).toBe(LIGHT);
    } finally {
      spy.mockRestore();
      spy2.mockRestore();
    }
  });
});

describe('[3b owner] chat models settings', () => {
  const s = {
    model_id: MAIN,
    model_id_light: LIGHT,
    chat_models: [],
    chat_models_effective: [
      { id: MAIN, label: null },
      { id: LIGHT, label: null },
    ],
  } as unknown as AssistantSettings;

  it('edits the list and saves only chat_models (no test needed)', async () => {
    let body: Record<string, unknown> | null = null;
    mockFetch((url, init) => {
      if (url.startsWith('/api/assistant/settings') && init?.method === 'PUT') {
        body = JSON.parse(String(init.body)) as Record<string, unknown>;
        return { status: 200, body: { ...s, chat_models: body.chat_models } };
      }
      if (url.startsWith('/api/assistant/models') && init?.method === 'POST')
        return {
          status: 200,
          body: {
            items: [
              {
                id: 'us.anthropic.claude-opus-5-20260901-v1:0',
                name: 'Claude Opus 5',
                kind: 'inference_profile',
                streaming: true,
                status: 'ACTIVE',
              },
            ],
            error: null,
          },
        };
      return baseRoutes(url);
    });
    render(wrap(<ChatModelsCard s={s} />));
    expect(screen.getByTestId('chat-models-default')).toBeTruthy();
    expect(screen.getByTestId('chat-models-effective').textContent).toContain(
      'Claude Sonnet 5.5 (default)',
    );
    fireEvent.click(screen.getByText('List models'));
    fireEvent.click(await screen.findByText('us.anthropic.claude-opus-5-20260901-v1:0'));
    fireEvent.change(screen.getByLabelText('Display name'), { target: { value: 'Opus' } });
    fireEvent.click(screen.getByText('Add model'));
    const ids = screen.getAllByLabelText('Model or inference profile ID');
    fireEvent.change(ids[1], { target: { value: 'bad id' } });
    expect(screen.getByText('Invalid model or inference profile ID')).toBeTruthy();
    fireEvent.change(ids[1], { target: { value: LIGHT } });
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(body).not.toBeNull());
    expect(body).toEqual({
      chat_models: [
        { id: 'us.anthropic.claude-opus-5-20260901-v1:0', label: 'Opus' },
        { id: LIGHT, label: null },
      ],
    });
  });
});
