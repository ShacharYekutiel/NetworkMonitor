import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import { isApiError } from './api/client';
import { ToastProvider } from './components/toast';
import { AuthProvider } from './hooks/auth';
import { LiveProvider } from './hooks/live';
import { ThemeProvider } from './hooks/theme';
import { AppRoutes } from './router';
import { KioskPage } from './pages/KioskPage';
import { ACK_TOKEN_KEY, ackTokenFromHash } from './lib/kiosk';

export function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        refetchOnWindowFocus: false,
        retry: (count, e) => {
          if (isApiError(e) && e.status >= 400 && e.status < 500) return false;
          return count < 2;
        },
      },
      mutations: { retry: false },
    },
  });
}

const queryClient = createQueryClient();

/**
 * [3] /ack#<token>: keep the token across the login redirect (the fragment is lost there) and
 * remove it from the address bar.
 */
function stashAckToken(): void {
  if (window.location.pathname !== '/ack') return;
  const tok = ackTokenFromHash(window.location.hash);
  if (!tok) return;
  try {
    sessionStorage.setItem(ACK_TOKEN_KEY, tok);
  } catch {
    /* the page falls back to the fragment */
    return;
  }
  window.history.replaceState(null, '', '/ack');
}

export function App() {
  // [3] The NOC wall screen skips the login boot sequence entirely (no session, no app WS).
  if (window.location.pathname === '/noc')
    return (
      <QueryClientProvider client={queryClient}>
        <KioskPage />
      </QueryClientProvider>
    );
  stashAckToken();
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <ToastProvider>
          <BrowserRouter>
            <AuthProvider>
              <LiveProvider>
                <AppRoutes />
              </LiveProvider>
            </AuthProvider>
          </BrowserRouter>
        </ToastProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
