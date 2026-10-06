import { useSyncExternalStore } from 'react';
import { session } from '../api/session';
export function useSession() {
  return useSyncExternalStore(session.subscribe, session.get);
}
