import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api, buildUrl, parseError, setUnauthorizedHandler } from './client';

function mockFetch(status: number, body: unknown, statusText = '') {
  const fn = vi.fn().mockResolvedValue(
    new Response(
      body === undefined ? null : typeof body === 'string' ? body : JSON.stringify(body),
      {
        status,
        statusText,
        headers: { 'Content-Type': 'application/json' },
      },
    ),
  );
  vi.stubGlobal('fetch', fn);
  return fn;
}

afterEach(() => {
  vi.unstubAllGlobals();
  setUnauthorizedHandler(null);
});

describe('buildUrl', () => {
  it('prefixes /api and drops empty query values', () => {
    expect(buildUrl('/sensors', { device_id: 3, status: undefined, q: '', page: 1 })).toBe(
      '/api/sensors?device_id=3&page=1',
    );
    expect(buildUrl('/api/tree')).toBe('/api/tree');
  });
});

describe('request', () => {
  it('sends JSON with same-origin credentials', async () => {
    const f = mockFetch(201, { id: 1 });
    const r = await api.post<{ id: number }>('/groups', { name: 'x' });
    expect(r).toEqual({ id: 1 });
    const [url, init] = f.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/groups');
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('same-origin');
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json');
    expect(init.body).toBe('{"name":"x"}');
  });

  it('sends bodiless POST without a content type', async () => {
    const f = mockFetch(200, { ok: true });
    await api.post('/sensors/1/run');
    const init = f.mock.calls[0][1] as RequestInit;
    expect(init.body).toBeUndefined();
    expect((init.headers as Record<string, string>)['Content-Type']).toBeUndefined();
  });

  it('returns undefined for 204', async () => {
    mockFetch(204, undefined);
    await expect(api.del('/groups/1')).resolves.toBeUndefined();
  });

  it('throws ApiError parsed from the error envelope', async () => {
    mockFetch(409, {
      error: { code: 'conflict', message: 'Name exists', details: [{ field: 'name' }] },
    });
    const err = await api.post('/groups', { name: 'a' }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    const e = err as ApiError;
    expect(e.status).toBe(409);
    expect(e.code).toBe('conflict');
    expect(e.message).toBe('Name exists');
    expect(e.details).toEqual([{ field: 'name' }]);
  });

  it('falls back to a code derived from the status for non-envelope bodies', async () => {
    mockFetch(502, '<html>Bad gateway</html>', 'Bad Gateway');
    const e = (await api.get('/tree').catch((x: unknown) => x)) as ApiError;
    expect(e.code).toBe('internal_error');
    expect(e.status).toBe(502);
  });

  it('maps network failures to network_error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    const e = (await api.get('/tree').catch((x: unknown) => x)) as ApiError;
    expect(e.code).toBe('network_error');
    expect(e.status).toBe(0);
  });

  it('calls the unauthorized handler on 401 unauthenticated only', async () => {
    const h = vi.fn();
    setUnauthorizedHandler(h);
    mockFetch(401, { error: { code: 'unauthenticated', message: 'x', details: null } });
    await api.get('/tree').catch(() => undefined);
    expect(h).toHaveBeenCalledTimes(1);

    mockFetch(401, { error: { code: 'invalid_credentials', message: 'x', details: null } });
    await api.post('/auth/change-password', {}).catch(() => undefined);
    expect(h).toHaveBeenCalledTimes(1);
  });

  it('honours skipAuthRedirect', async () => {
    const h = vi.fn();
    setUnauthorizedHandler(h);
    mockFetch(401, { error: { code: 'unauthenticated', message: 'x', details: null } });
    await api.get('/auth/me', undefined, { skipAuthRedirect: true }).catch(() => undefined);
    expect(h).not.toHaveBeenCalled();
  });
});

describe('parseError', () => {
  it('handles validation details', async () => {
    const e = await parseError(
      new Response(
        JSON.stringify({
          error: {
            code: 'validation_error',
            message: 'Invalid',
            details: [{ loc: ['body', 'interval_s'], msg: 'must be >= 10' }],
          },
        }),
        { status: 422 },
      ),
    );
    expect(e.code).toBe('validation_error');
    expect(e.details?.[0].loc).toEqual(['body', 'interval_s']);
  });
});
