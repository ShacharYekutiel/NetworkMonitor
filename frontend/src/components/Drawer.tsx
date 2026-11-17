import { useEffect, useId, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { XIcon } from './icons';
import { cx } from './ui';

/**
 * [2b-3] A side panel at the inline end (right in LTR, left in RTL) for record details; premium
 * (prototype .spanel): a card floating under the top bar, radius 28, over a blurred scrim.
 * `narrow` is the 440px panel of "Explain"; record details keep a wider one.
 */
export function Drawer({
  open,
  title,
  onClose,
  children,
  footer,
  testId,
  narrow,
}: {
  open: boolean;
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  testId?: string;
  narrow?: boolean;
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
    <div
      className="fixed inset-0 z-50 flex justify-end pb-3 pe-3 pt-[76px] max-sm:p-2 max-sm:pt-16"
      data-testid={testId}
    >
      <div
        className="absolute inset-0 bg-overlay backdrop-blur-[6px] motion-safe:animate-[nm-fade-in_.25s_ease-out]"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={cx(
          'relative flex h-full w-full flex-col rounded-[28px] bg-surface text-ink shadow-[0_24px_80px_rgba(0,0,0,0.22),0_0_0_1px_var(--border)] focus:outline-none motion-safe:animate-[nm-panel-in_.5s_cubic-bezier(.32,.72,0,1)]',
          narrow ? 'max-w-[440px]' : 'max-w-2xl',
        )}
      >
        <header className="flex items-center justify-between gap-3 px-[22px] pb-2 pt-[22px]">
          <h3
            id={titleId}
            className="min-w-0 truncate text-[21px] font-semibold tracking-[-0.01em]"
          >
            {title}
          </h3>
          <button
            type="button"
            onClick={onClose}
            className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-chip text-ink-muted hover:text-ink"
            aria-label={t('common.close')}
          >
            <XIcon size={16} />
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-[22px] pb-4 pt-2">{children}</div>
        {footer && (
          <footer className="mx-[22px] flex flex-wrap items-center gap-2.5 border-t border-line pb-[18px] pt-3">
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
    <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-[18px] gap-y-2.5 text-[14.5px]">
      {rows.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="whitespace-nowrap text-ink-muted">{k}</dt>
          <dd className="min-w-0 break-words">{v}</dd>
        </div>
      ))}
    </dl>
  );
}
