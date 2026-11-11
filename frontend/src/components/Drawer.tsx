import { useEffect, useId, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { XIcon } from './icons';

/** [2b-3] A side panel at the inline end (right in LTR, left in RTL) for record details. */
export function Drawer({
  open,
  title,
  onClose,
  children,
  footer,
  testId,
}: {
  open: boolean;
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  testId?: string;
}) {
  const { t } = useTranslation();
  const titleId = useId();
  const ref = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onCloseRef.current();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      if (previous && document.contains(previous)) previous.focus();
    };
  }, [open]);
  if (!open) return null;
  return createPortal(
    <div className="fixed inset-0 z-50 flex justify-end" data-testid={testId}>
      <div className="absolute inset-0 bg-overlay" onClick={onClose} aria-hidden="true" />
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="relative flex h-full w-full max-w-2xl flex-col bg-surface shadow-pop ring-1 ring-line focus:outline-none"
      >
        <header className="flex items-center justify-between gap-2 border-b border-line px-4 py-3">
          <h3 id={titleId} className="min-w-0 truncate text-base font-semibold">
            {title}
          </h3>
          <button
            type="button"
            onClick={onClose}
            className="rounded p-1 text-ink-muted hover:bg-surface-hover"
            aria-label={t('common.close')}
          >
            <XIcon />
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">{children}</div>
        {footer && (
          <footer className="flex flex-wrap justify-end gap-2 border-t border-line px-4 py-3">
            {footer}
          </footer>
        )}
      </div>
    </div>,
    document.body,
  );
}

/** Label/value rows of a detail panel. */
export function DetailRows({ rows }: { rows: [ReactNode, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
      {rows.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="whitespace-nowrap text-ink-muted">{k}</dt>
          <dd className="min-w-0 break-words">{v}</dd>
        </div>
      ))}
    </dl>
  );
}
