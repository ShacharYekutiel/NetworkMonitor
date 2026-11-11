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
      <div className="pointer-events-none fixed bottom-4 end-4 z-[60] flex w-80 flex-col gap-2">
        {toasts.map((t) => {
          const Icon = TONE_ICON[t.tone];
          return (
            <div
              key={t.id}
              role="status"
              className="pointer-events-auto flex items-center gap-2.5 rounded-lg border border-line bg-surface-raised px-3.5 py-[11px] text-[13px] text-ink shadow-pop"
            >
              <Icon
                size={16}
                className={cx(
                  'shrink-0',
                  t.tone === 'success' && 'text-status-up',
                  t.tone === 'error' && 'text-status-down',
                  t.tone === 'info' && 'text-accent-ink',
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
