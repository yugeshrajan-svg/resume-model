import { session, SessionStore } from './session';
import type { Tokens } from './types';

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public details?: unknown,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}
async function decode(response: Response) {
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const body = data as { detail?: unknown; details?: unknown } | null;
    let message = `Request failed (${response.status}).`;
    if (typeof body?.detail === 'string') message = body.detail;
    else if (Array.isArray(body?.detail))
      message = body.detail
        .map(
          (item: { loc?: string[]; msg?: string }) =>
            `${item.loc?.slice(1).join(' ') || 'Input'}: ${item.msg || 'Invalid value'}`,
        )
        .join('. ');
    throw new ApiError(message, response.status, body?.details);
  }
  return data;
}

export class ApiClient {
  private refreshing: Promise<Tokens> | null = null;
  constructor(
    private store: SessionStore,
    private transport: typeof fetch = (...args) => fetch(...args),
  ) {}
  private refresh(): Promise<Tokens> {
    if (this.refreshing) return this.refreshing;
    const before = this.store.get();
    const revision = this.store.revision;
    if (!before) return Promise.reject(new ApiError('Please sign in again.', 401));
    const task = (async () => {
      try {
        const response = await this.transport('/v1/auth/refresh', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: before.refresh_token }),
        });
        const tokens = (await decode(response)) as Tokens;
        if (revision !== this.store.revision) throw new ApiError('Session changed. Please retry.', 401);
        this.store.set(tokens, true);
        return tokens;
      } catch (error) {
        if (error instanceof ApiError && error.status === 401 && revision === this.store.revision)
          this.store.set(null);
        throw error;
      }
    })();
    this.refreshing = task;
    void task
      .finally(() => {
        if (this.refreshing === task) this.refreshing = null;
      })
      .catch(() => {});
    return task;
  }
  async request<T>(
    path: string,
    options: { method?: string; body?: unknown; signal?: AbortSignal; public?: boolean } = {},
  ): Promise<T> {
    const before = this.store.get();
    const epoch = this.store.epoch;
    if (!options.public && !before) throw new ApiError('Please sign in again.', 401);
    if (!options.public && before && before.expiresAt <= Date.now() + 15_000) await this.refresh();
    if (!options.public && epoch !== this.store.epoch)
      throw new ApiError('Session changed. Please retry.', 401);
    const token = options.public ? undefined : this.store.get()?.access_token;
    const send = (accessToken?: string) =>
      this.transport(`/v1${path}`, {
        method: options.method || 'GET',
        signal: options.signal,
        headers: {
          ...(options.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
          ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
        },
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
      });
    let response = await send(token);
    if (!options.public && epoch !== this.store.epoch)
      throw new ApiError('Session changed. Please retry.', 401);
    if (response.status === 401 && !options.public) {
      const current = this.store.get();
      if (!current || current.company_id !== before?.company_id || current.user_id !== before?.user_id) {
        throw new ApiError('Session changed. Please sign in again.', 401);
      }
      const tokens = current.access_token !== token ? current : await this.refresh();
      response = await send(tokens.access_token);
      if (epoch !== this.store.epoch) throw new ApiError('Session changed. Please retry.', 401);
      if (response.status === 401 && this.store.get()?.access_token === tokens.access_token)
        this.store.set(null);
    }
    const data = await decode(response);
    if (!options.public && epoch !== this.store.epoch)
      throw new ApiError('Session changed. Please retry.', 401);
    return data as T;
  }
}
export const api = new ApiClient(session);
export function message(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong. Please try again.';
}
