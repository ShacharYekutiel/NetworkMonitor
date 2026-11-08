import type { ApiErrorDetail } from './types';

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: ApiErrorDetail[] | null;

  constructor(
    status: number,
    code: string,
    message: string,
    details: ApiErrorDetail[] | null = null,
  ) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}

type UnauthorizedHandler = (err: ApiError) => void;
let onUnauthorized: UnauthorizedHandler | null = null;

/** Registers a global handler for 401 responses (the SPA redirects to login). */
export function setUnauthorizedHandler(h: UnauthorizedHandler | null): void {
  onUnauthorized = h;
}

type QueryValue = string | number | boolean | null | undefined;
/** Arrays are sent as repeated keys (e.g. `kind=physical&kind=lag`). */
export type Query = Record<string, QueryValue | readonly QueryValue[]>;

export function buildUrl(path: string, query?: Query): string {
  const base = path.startsWith('/api') ? path : `/api${path.startsWith('/') ? '' : '/'}${path}`;
  if (!query) return base;
  const params = new URLSearchParams();
  for (const [k, raw] of Object.entries(query)) {
    for (const v of Array.isArray(raw) ? raw : [raw]) {
      if (v === undefined || v === null || v === '') continue;
      params.append(k, String(v));
    }
  }
  const qs = params.toString();
  return qs ? `${base}?${qs}` : base;
}

/** Parses an error envelope from a failed response; never throws. */
export async function parseError(res: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    const text = await res.text();
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  if (
    body &&
    typeof body === 'object' &&
    'error' in body &&
    body.error &&
    typeof body.error === 'object'
  ) {
    const e = body.error as { code?: unknown; message?: unknown; details?: unknown };
    return new ApiError(
      res.status,
      typeof e.code === 'string' ? e.code : fallbackCode(res.status),
      typeof e.message === 'string' ? e.message : res.statusText,
      Array.isArray(e.details) ? (e.details as ApiErrorDetail[]) : null,
    );
  }
  return new ApiError(res.status, fallbackCode(res.status), res.statusText || `HTTP ${res.status}`);
}

function fallbackCode(status: number): string {
  switch (status) {
    case 400:
      return 'bad_request';
    case 401:
      return 'unauthenticated';
    case 403:
      return 'forbidden';
    case 404:
      return 'not_found';
    case 409:
      return 'conflict';
    case 415:
      return 'unsupported_media_type';
    case 422:
      return 'validation_error';
    case 429:
      return 'rate_limited';
    default:
      return status >= 500 ? 'internal_error' : 'bad_request';
  }
}

export type RequestOptions = {
  query?: Query;
  body?: unknown;
  signal?: AbortSignal;
  /** Do not trigger the global 401 handler (used by the boot sequence and login). */
  skipAuthRedirect?: boolean;
};

export async function request<T>(
  method: string,
  path: string,
  opts: RequestOptions = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  const init: RequestInit = { method, credentials: 'same-origin', headers, signal: opts.signal };
  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }
  let res: Response;
  try {
    res = await fetch(buildUrl(path, opts.query), init);
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    throw new ApiError(0, 'network_error', e instanceof Error ? e.message : 'Network error');
  }
  if (!res.ok) {
    const err = await parseError(res);
    // invalid_credentials (wrong login or current password) is a 401 too, but not a lost session.
    if (
      err.status === 401 &&
      err.code === 'unauthenticated' &&
      !opts.skipAuthRedirect &&
      onUnauthorized
    )
      onUnauthorized(err);
    throw err;
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export const api = {
  get: <T>(path: string, query?: Query, opts?: Omit<RequestOptions, 'query' | 'body'>) =>
    request<T>('GET', path, { ...opts, query }),
  post: <T>(path: string, body?: unknown, opts?: Omit<RequestOptions, 'body'>) =>
    request<T>('POST', path, { ...opts, body }),
  put: <T>(path: string, body?: unknown) => request<T>('PUT', path, { body }),
  patch: <T>(path: string, body?: unknown) => request<T>('PATCH', path, { body }),
  del: <T = void>(path: string, query?: Query) => request<T>('DELETE', path, { query }),
};
