import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import { isApiError } from './api/client';
import { ToastProvider } from './components/toast';
import { AuthProvider } from './hooks/auth';
import { LiveProvider } from './hooks/live';
import { ThemeProvider } from './hooks/theme';
import { AppRoutes } from './router';

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

export function App() {
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
