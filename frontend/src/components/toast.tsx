import { createContext, useCallback, useContext, useState, type ReactNode } from 'react';
import { AlertIcon, CheckIcon, XIcon } from './icons';
import { cx } from './ui';

const TONE_ICON = { success: CheckIcon, error: XIcon, info: AlertIcon } as const;

type Toast = { id: number; tone: 'success' | 'error' | 'info'; text: string };
type ToastCtx = { push: (tone: Toast['tone'], text: string) => void };
const Ctx = createContext<ToastCtx>({ push: () => undefined });

let nextId = 1;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((tone: Toast['tone'], text: string) => {
    const id = nextId++;
    setToasts((ts) => [...ts, { id, tone, text }]);
    setTimeout(() => setToasts((ts) => ts.filter((x) => x.id !== id)), 5000);
  }, []);
  return (
    <Ctx.Provider value={{ push }}>
      {children}
      {/* Premium toast (prototype .toast): an ink pill at the bottom centre, rising in. */}
      <div className="pointer-events-none fixed inset-x-0 bottom-6 z-[60] flex flex-col items-center gap-2 px-4">
        {toasts.map((t) => {
          const Icon = TONE_ICON[t.tone];
          return (
            <div
              key={t.id}
              role="status"
              className="pointer-events-auto flex max-w-[min(560px,100%)] items-center gap-2.5 rounded-full bg-accent px-5 py-3 text-sm font-medium text-accent-on shadow-[0_10px_30px_rgba(0,0,0,0.25)] motion-safe:animate-[nm-toast-in_.5s_cubic-bezier(.32,.72,0,1)]"
            >
              <Icon
                size={16}
                className={cx(
                  'shrink-0',
                  t.tone === 'success' && 'text-status-up',
                  t.tone === 'error' && 'text-status-down',
                  t.tone === 'info' && 'opacity-70',
                )}
              />
              <span className="min-w-0">{t.text}</span>
            </div>
          );
        })}
      </div>
    </Ctx.Provider>
  );
}

export function useToast(): ToastCtx {
  return useContext(Ctx);
}
