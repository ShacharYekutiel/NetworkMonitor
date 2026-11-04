import { createContext, useCallback, useContext, useState, type ReactNode } from 'react';
import { cx } from './ui';

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
        {toasts.map((t) => (
          <div
            key={t.id}
            role="status"
            className={cx(
              'pointer-events-auto rounded-md px-3 py-2 text-sm text-white shadow-lg',
              t.tone === 'success' && 'bg-green-700',
              t.tone === 'error' && 'bg-red-700',
              t.tone === 'info' && 'bg-slate-800',
            )}
          >
            {t.text}
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}

export function useToast(): ToastCtx {
  return useContext(Ctx);
}
