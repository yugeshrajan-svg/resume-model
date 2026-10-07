import type { Session, Tokens } from './types';

const KEY = 'shortlist.session';
export class SessionStore {
  private value: Session | null = null;
  private listeners = new Set<() => void>();
  revision = 0;
  epoch = 0;
  constructor(private storage?: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>) {
    try {
      const parsed = JSON.parse(storage?.getItem(KEY) || 'null') as Session | null;
      if (
        parsed &&
        typeof parsed.access_token === 'string' &&
        typeof parsed.refresh_token === 'string' &&
        typeof parsed.company_id === 'string' &&
        typeof parsed.user_id === 'string' &&
        Number.isFinite(parsed.expiresAt) &&
        ['admin', 'recruiter', 'hiring_manager'].includes(parsed.role)
      ) {
        this.value = parsed;
      }
    } catch {
      /* A blocked or corrupted storage must not prevent sign-in. */
    }
  }
  get = () => this.value;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  set(tokens: Tokens | null, rotating = false) {
    this.value = tokens ? { ...tokens, expiresAt: Date.now() + tokens.expires_in * 1000 } : null;
    this.revision++;
    if (!rotating) this.epoch++;
    try {
      if (this.value) this.storage?.setItem(KEY, JSON.stringify(this.value));
      else this.storage?.removeItem(KEY);
    } catch {
      /* Authentication still works in memory when storage is unavailable. */
    }
    this.listeners.forEach((listener) => listener());
  }
}
function browserStorage() {
  try {
    return window.sessionStorage;
  } catch {
    return undefined;
  }
}
export const session = new SessionStore(browserStorage());
