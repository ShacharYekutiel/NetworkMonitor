import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { setUnauthorizedHandler, isApiError } from '../api/client';
import { endpoints } from '../api/endpoints';
import type { Language, User } from '../api/types';
import { setLanguage, storedLanguage } from '../i18n';

export type BootState = 'loading' | 'setup' | 'anonymous' | 'authenticated' | 'error';

type AuthCtx = {
  state: BootState;
  user: User | null;
  isAdmin: boolean;
  bootError: unknown;
  /** Called after login / setup succeeds. */
  signedIn: (user: User) => void;
  setUser: (user: User) => void;
  logout: () => Promise<void>;
  /** Session expired or was revoked. */
  expire: () => void;
  setupRequired: () => void;
  retry: () => void;
};

const Ctx = createContext<AuthCtx | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [state, setState] = useState<BootState>('loading');
  const [user, setUserState] = useState<User | null>(null);
  const [bootError, setBootError] = useState<unknown>(null);
  const [bootKey, setBootKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const st = await endpoints.setupStatus();
        if (cancelled) return;
        if (st.setup_required) {
          await applyPreLoginLanguage();
          if (!cancelled) setState('setup');
          return;
        }
        try {
          const me = await endpoints.me();
          if (cancelled) return;
          setUserState(me.user);
          setLanguage(me.user.language);
          setState('authenticated');
        } catch (e) {
          if (isApiError(e) && e.status === 401) {
            await applyPreLoginLanguage();
            if (!cancelled) setState('anonymous');
          } else if (isApiError(e) && e.code === 'setup_required') {
            if (!cancelled) setState('setup');
          } else throw e;
        }
      } catch (e) {
        if (!cancelled) {
          setBootError(e);
          setState('error');
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [bootKey]);

  const expire = useCallback(() => {
    setUserState(null);
    qc.clear();
    setState((s) => (s === 'setup' ? s : 'anonymous'));
  }, [qc]);

  useEffect(() => {
    setUnauthorizedHandler(() => expire());
    return () => setUnauthorizedHandler(null);
  }, [expire]);

  const signedIn = useCallback((u: User) => {
    setUserState(u);
    setLanguage(u.language);
    setState('authenticated');
  }, []);

  const logout = useCallback(async () => {
    try {
      await endpoints.logout();
    } catch {
      /* ignore */
    }
    expire();
  }, [expire]);

  const value = useMemo<AuthCtx>(
    () => ({
      state,
      user,
      isAdmin: user?.role === 'admin',
      bootError,
      signedIn,
      setUser: setUserState,
      logout,
      expire,
      setupRequired: () => setState('setup'),
      retry: () => {
        setState('loading');
        setBootKey((k) => k + 1);
      },
    }),
    [state, user, bootError, signedIn, logout, expire],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

async function applyPreLoginLanguage(): Promise<void> {
  const stored = storedLanguage();
  if (stored) {
    setLanguage(stored, false);
    return;
  }
  try {
    const pub = await endpoints.publicSettings();
    setLanguage(pub.default_language as Language, false);
  } catch {
    /* keep current */
  }
}

export function useAuth(): AuthCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error('useAuth outside AuthProvider');
  return c;
}
