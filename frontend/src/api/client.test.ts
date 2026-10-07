import { describe, expect, it, vi } from 'vitest';
import { ApiClient } from './client';
import { SessionStore } from './session';
import type { Tokens } from './types';

const tokens: Tokens = {
  access_token: 'old-access',
  refresh_token: 'old-refresh-token-12345',
  expires_in: 3600,
  company_id: 'company-1',
  user_id: 'user-1',
  role: 'admin',
  token_type: 'bearer',
};
const renewed = { ...tokens, access_token: 'new-access', refresh_token: 'rotated-refresh-token-12345' };
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const deferred = <T>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
};
describe('authenticated requests', () => {
  it('preserves uploaded bytes and metadata when authentication refreshes', async () => {
    const store = new SessionStore();
    store.set(tokens);
    const upload = new Blob(['original resume bytes']);
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(json({}, 401))
      .mockResolvedValueOnce(json(renewed))
      .mockResolvedValueOnce(json({ received: true }));
    await new ApiClient(store, fetcher).request('/upload', {
      method: 'POST',
      rawBody: upload,
      headers: { 'x-filename': 'resume.txt' },
    });
    for (const index of [0, 2]) {
      expect(fetcher.mock.calls[index][1]?.body).toBe(upload);
      expect(fetcher.mock.calls[index][1]?.headers).toMatchObject({ 'x-filename': 'resume.txt' });
    }
    expect(fetcher.mock.calls[2][1]?.headers).toMatchObject({ Authorization: 'Bearer new-access' });
  });
  it('submits public applications without using a staff session', async () => {
    const store = new SessionStore();
    store.set(tokens);
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json({ received: true }));
    await new ApiClient(store, fetcher).request('/public/jobs/one/apply', {
      public: true,
      method: 'POST',
      rawBody: new Blob(['resume']),
      headers: { 'x-candidate-consent': 'yes' },
    });
    expect(fetcher.mock.calls[0][1]?.headers).not.toHaveProperty('Authorization');
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('refreshes authentication before returning a resume download as a blob', async () => {
    const store = new SessionStore();
    store.set(tokens);
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(json({ detail: 'Expired' }, 401))
      .mockResolvedValueOnce(json(renewed))
      .mockResolvedValueOnce(
        new Response('resume bytes', { headers: { 'Content-Type': 'application/pdf' } }),
      );
    const blob = await new ApiClient(store, fetcher).request<Blob>('/resumes/one/download', { blob: true });
    expect(await blob.text()).toBe('resume bytes');
    expect(fetcher).toHaveBeenCalledTimes(3);
  });
  it('discards a late successful response after another user signs in', async () => {
    const store = new SessionStore();
    store.set(tokens);
    const pending = deferred<Response>();
    const request = new ApiClient(store, async () => pending.promise).request('/jobs');
    store.set({ ...tokens, company_id: 'other-company', user_id: 'other-user' });
    pending.resolve(json([{ title: 'Private old workspace' }]));
    await expect(request).rejects.toThrow('Session changed');
  });
  it('does not replay old mutations after the same user signs out and back in', async () => {
    const store = new SessionStore();
    store.set(tokens);
    const pending = deferred<Response>();
    const fetcher = vi.fn<typeof fetch>(async () => pending.promise);
    const request = new ApiClient(store, fetcher).request('/jobs', {
      method: 'POST',
      body: { title: 'Old draft' },
    });
    store.set(null);
    store.set(renewed);
    pending.resolve(json({}, 401));
    await expect(request).rejects.toThrow('Session changed');
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('shares one refresh across concurrent unauthorized requests and uses rotated tokens', async () => {
    const store = new SessionStore();
    store.set(tokens);
    const pending = deferred<Response>();
    const fetcher = vi.fn<typeof fetch>(async (url, init) =>
      url === '/v1/auth/refresh'
        ? pending.promise
        : (init?.headers as Record<string, string>).Authorization === 'Bearer old-access'
          ? json({ detail: 'Expired' }, 401)
          : json({ ok: true }),
    );
    const api = new ApiClient(store, fetcher);
    const first = api.request('/jobs');
    const second = api.request('/jobs');
    await vi.waitFor(() =>
      expect(fetcher.mock.calls.filter(([url]) => url === '/v1/auth/refresh')).toHaveLength(1),
    );
    pending.resolve(json(renewed));
    expect(await Promise.all([first, second])).toEqual([{ ok: true }, { ok: true }]);
    expect(store.get()?.refresh_token).toBe(renewed.refresh_token);
  });
  it('uses already rotated credentials when an old-token 401 arrives late', async () => {
    const store = new SessionStore();
    store.set(tokens);
    const late = deferred<Response>();
    let oldCalls = 0;
    const fetcher = vi.fn<typeof fetch>(async (url, init) => {
      if (url === '/v1/auth/refresh') return json(renewed);
      if ((init?.headers as Record<string, string>).Authorization === 'Bearer old-access')
        return ++oldCalls === 1 ? json({}, 401) : late.promise;
      return json({ ok: true });
    });
    const api = new ApiClient(store, fetcher);
    const first = api.request('/jobs');
    const second = api.request('/jobs');
    await first;
    late.resolve(json({}, 401));
    await second;
    expect(fetcher.mock.calls.filter(([url]) => url === '/v1/auth/refresh')).toHaveLength(1);
  });
  it('refreshes before sending an expired access token', async () => {
    const store = new SessionStore();
    store.set({ ...tokens, expires_in: -1 });
    const fetcher = vi.fn<typeof fetch>(async (url) =>
      url === '/v1/auth/refresh' ? json(renewed) : json([]),
    );
    await new ApiClient(store, fetcher).request('/jobs');
    expect(fetcher.mock.calls[0][0]).toBe('/v1/auth/refresh');
    expect((fetcher.mock.calls[1][1]?.headers as Record<string, string>).Authorization).toBe(
      'Bearer new-access',
    );
  });
  it('clears an invalid refresh session and does not loop on unauthorized responses', async () => {
    const store = new SessionStore();
    store.set(tokens);
    const fetcher = vi.fn<typeof fetch>(async () => json({ detail: 'Sign in again' }, 401));
    await expect(new ApiClient(store, fetcher).request('/jobs')).rejects.toThrow('Sign in again');
    expect(store.get()).toBeNull();
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
  it('keeps the session on a temporary refresh outage', async () => {
    const store = new SessionStore();
    store.set({ ...tokens, expires_in: -1 });
    await expect(
      new ApiClient(store, async () => json({ detail: 'Unavailable' }, 503)).request('/jobs'),
    ).rejects.toThrow('Unavailable');
    expect(store.get()?.user_id).toBe(tokens.user_id);
  });
  it('does not restore a session after signing out during refresh', async () => {
    const store = new SessionStore();
    store.set({ ...tokens, expires_in: -1 });
    const pending = deferred<Response>();
    const request = new ApiClient(store, async () => pending.promise).request('/jobs');
    store.set(null);
    pending.resolve(json(renewed));
    await expect(request).rejects.toThrow('Session changed');
    expect(store.get()).toBeNull();
  });
  it('does not clear another user’s session after a late unauthorized request', async () => {
    const store = new SessionStore();
    store.set(tokens);
    const pending = deferred<Response>();
    const request = new ApiClient(store, async () => pending.promise).request('/jobs');
    store.set({ ...tokens, user_id: 'another-user', company_id: 'another-company' });
    pending.resolve(json({}, 401));
    await expect(request).rejects.toThrow('Session changed');
    expect(store.get()?.user_id).toBe('another-user');
  });
  it('formats backend validation errors without exposing request bodies', async () => {
    const store = new SessionStore();
    store.set(tokens);
    await expect(
      new ApiClient(store, async () =>
        json({ detail: [{ loc: ['body', 'title'], msg: 'Too short' }] }, 422),
      ).request('/jobs'),
    ).rejects.toThrow('title: Too short');
  });
  it('recovers safely from malformed or blocked browser storage', () => {
    expect(
      new SessionStore({ getItem: () => '{broken', setItem: vi.fn(), removeItem: vi.fn() }).get(),
    ).toBeNull();
    const store = new SessionStore({
      getItem: () => {
        throw new Error('Blocked');
      },
      setItem: () => {
        throw new Error('Blocked');
      },
      removeItem: () => {
        throw new Error('Blocked');
      },
    });
    store.set(tokens);
    expect(store.get()?.access_token).toBe(tokens.access_token);
    store.set(null);
    expect(store.get()).toBeNull();
  });
});
