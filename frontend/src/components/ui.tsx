import {
  forwardRef,
  useEffect,
  useId,
  useRef,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { XIcon } from './icons';
import { errorMessage, type ErrorContext } from '../lib/errors';

export function cx(...c: (string | false | null | undefined)[]): string {
  return c.filter(Boolean).join(' ');
}

type Variant = 'primary' | 'secondary' | 'danger' | 'ghost';
const VARIANTS: Record<Variant, string> = {
  primary: 'border border-transparent bg-accent text-accent-on hover:bg-accent-hover',
  secondary: 'border border-line-strong bg-surface text-ink hover:border-ink-muted',
  danger: 'border border-transparent bg-danger text-on-danger hover:opacity-90',
  ghost: 'border border-transparent text-ink-muted hover:bg-surface-hover hover:text-ink',
};

const BUTTON_BASE =
  'inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-md font-semibold transition-colors focus:outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus';
const BUTTON_SIZE = { sm: 'h-[26px] px-[9px] text-xs', md: 'h-[34px] px-3.5 text-[13px]' };

export const Button = forwardRef<
  HTMLButtonElement,
  ButtonHTMLAttributes<HTMLButtonElement> & {
    variant?: Variant;
    size?: 'sm' | 'md';
    busy?: boolean;
  }
>(function Button(
  { variant = 'secondary', size = 'md', busy, className, children, disabled, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type="button"
      disabled={disabled || busy}
      className={cx(
        BUTTON_BASE,
        'disabled:cursor-not-allowed disabled:opacity-50',
        BUTTON_SIZE[size],
        VARIANTS[variant],
        className,
      )}
      {...rest}
    >
      {busy && <Spinner className="h-3.5 w-3.5" />}
      {children}
    </button>
  );
});

/** Classes of a Button, for router <Link>s that look like buttons (never nest <button> in <a>). */
export function buttonClass(variant: Variant = 'secondary', size: 'sm' | 'md' = 'md'): string {
  return cx(BUTTON_BASE, BUTTON_SIZE[size], VARIANTS[variant]);
}

const inputBase =
  'block w-full rounded-md border border-line-strong bg-surface-sunken px-2.5 py-1.5 text-sm text-ink placeholder:text-ink-subtle focus:border-transparent focus:outline focus:outline-2 focus:-outline-offset-1 focus:outline-focus disabled:border-dashed disabled:text-ink-subtle';

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...rest }, ref) {
    return <input ref={ref} className={cx(inputBase, className)} {...rest} />;
  },
);

export function Select({ className, children, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={cx(inputBase, 'pe-8', className)} {...rest}>
      {children}
    </select>
  );
}

export function Textarea({ className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cx(inputBase, className)} {...rest} />;
}

export function Field({
  label,
  hint,
  children,
  className,
  required,
}: {
  label: ReactNode;
  hint?: ReactNode;
  children: ReactNode;
  className?: string;
  required?: boolean;
}) {
  return (
    <label className={cx('block min-w-0', className)}>
      <span className="mb-1.5 block text-xs font-medium text-ink-muted">
        {label}
        {required && <span className="ms-0.5 text-status-down">*</span>}
      </span>
      {children}
      {hint && <span className="mt-1 block text-xs text-ink-subtle">{hint}</span>}
    </label>
  );
}

/** A labelled group of controls (e.g. checkboxes) — never nest <label>s. */
export function FieldGroup({
  label,
  hint,
  children,
  className,
  required,
}: {
  label: ReactNode;
  hint?: ReactNode;
  children: ReactNode;
  className?: string;
  required?: boolean;
}) {
  const id = useId();
  return (
    <div role="group" aria-labelledby={id} className={cx('block min-w-0', className)}>
      <span id={id} className="mb-1.5 block text-xs font-medium text-ink-muted">
        {label}
        {required && <span className="ms-0.5 text-status-down">*</span>}
      </span>
      {children}
      {hint && <span className="mt-1 block text-xs text-ink-subtle">{hint}</span>}
    </div>
  );
}

export function Checkbox({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: ReactNode;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label className="inline-flex cursor-pointer items-center gap-2 text-sm">
      <input
        type="checkbox"
        className="m-0 h-4 w-4 accent-accent"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span>{label}</span>
    </label>
  );
}

export function Card({
  title,
  actions,
  children,
  className,
  bodyClassName,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section
      className={cx(
        // min-w-0: as a grid/flex item the card must not grow to its table's width;
        // wide tables scroll inside the card instead.
        'min-w-0 rounded-lg border border-line bg-surface',
        className,
      )}
    >
      {(title || actions) && (
        <header className="flex min-h-[44px] items-center justify-between gap-2 border-b border-line px-3.5 py-2">
          <h2 className="text-sm font-semibold">{title}</h2>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
      )}
      {/* bodyClassName replaces the default padding ("p-4 p-0" would keep p-4: later in the CSS). */}
      <div className={bodyClassName ?? 'p-4'}>{children}</div>
    </section>
  );
}

export function Spinner({ className }: { className?: string }) {
  return (
    <svg
      className={cx('animate-spin', className ?? 'h-5 w-5')}
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <circle
        cx="12"
        cy="12"
        r="10"
        stroke="currentColor"
        strokeWidth="3"
        fill="none"
        opacity="0.25"
      />
      <path d="M22 12a10 10 0 00-10-10" stroke="currentColor" strokeWidth="3" fill="none" />
    </svg>
  );
}

export function Loading() {
  const { t } = useTranslation();
  return (
    <div className="flex items-center gap-2 p-6 text-sm text-ink-muted" role="status">
      <Spinner /> {t('common.loading')}
    </div>
  );
}

export function ErrorBox({
  error,
  className,
  context,
}: {
  error: unknown;
  className?: string;
  context?: ErrorContext;
}) {
  const { t } = useTranslation();
  if (!error) return null;
  return (
    <div
      role="alert"
      className={cx(
        'rounded-lg bg-status-down-soft px-3.5 py-2.5 text-sm border-s-2 border-status-down text-ink',
        className,
      )}
    >
      {errorMessage(t, error, context)}
    </div>
  );
}

export function Notice({
  children,
  tone = 'info',
}: {
  children: ReactNode;
  tone?: 'info' | 'success';
}) {
  return (
    <div
      className={cx(
        'rounded-lg border-s-2 px-3.5 py-2.5 text-sm text-ink',
        tone === 'success'
          ? 'border-status-up bg-status-up-soft'
          : 'border-accent-ink bg-accent-soft',
      )}
    >
      {children}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="p-6 text-center text-sm text-ink-muted">{children}</div>;
}

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function Modal({
  open,
  title,
  onClose,
  children,
  footer,
  wide,
  closeOnBackdrop,
  testId,
}: {
  open: boolean;
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
  /** [2b-2] a click on the backdrop closes the dialog (read-only dialogs; forms keep their data) */
  closeOnBackdrop?: boolean;
  testId?: string;
}) {
  const { t } = useTranslation();
  const titleId = useId();
  // A press that starts inside the dialog (e.g. selecting text) must not close it on release outside.
  const downOnBackdrop = useRef(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const dialog = dialogRef.current;
    // Focus the first field (or keep an autoFocus'ed one), so keyboard users land inside the dialog.
    if (dialog && !dialog.contains(document.activeElement)) {
      const body = dialog.querySelector<HTMLElement>(`[data-modal-body] :is(${FOCUSABLE})`);
      (body ?? dialog).focus();
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (e.key !== 'Tab' || !dialogRef.current) return;
      const items = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null,
      );
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      if (previous && document.contains(previous)) previous.focus();
    };
  }, [open]);

  if (!open) return null;
  // Rendered into <body> so the fixed overlay is not affected by the parent's layout (e.g. space-y margins).
  return createPortal(
    <div
      className="fixed inset-0 z-50 m-0 flex items-start justify-center overflow-y-auto bg-overlay p-4 pt-16"
      data-testid={testId ? `${testId}-backdrop` : undefined}
      onMouseDown={(e) => {
        downOnBackdrop.current = e.target === e.currentTarget;
      }}
      onClick={(e) => {
        if (closeOnBackdrop && downOnBackdrop.current && e.target === e.currentTarget) onClose();
        downOnBackdrop.current = false;
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        data-testid={testId}
        tabIndex={-1}
        className={cx(
          'w-full rounded-lg bg-surface-raised text-ink shadow-pop focus:outline-none',
          wide ? 'max-w-3xl' : 'max-w-lg',
        )}
      >
        <header className="flex items-center justify-between gap-2 border-b border-line px-4 py-3.5">
          <h3 id={titleId} className="min-w-0 text-base font-semibold">
            {title}
          </h3>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-1 text-ink-muted hover:bg-surface-hover hover:text-ink"
            aria-label={t('common.close')}
          >
            <XIcon />
          </button>
        </header>
        <div data-modal-body className="max-h-[70vh] overflow-y-auto p-4">
          {children}
        </div>
        {footer && (
          <footer className="flex justify-end gap-2 rounded-b-lg border-t border-line bg-surface-sunken px-4 py-3">
            {footer}
          </footer>
        )}
      </div>
    </div>,
    document.body,
  );
}

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel,
  onConfirm,
  onClose,
  busy,
  error,
}: {
  open: boolean;
  title: ReactNode;
  message: ReactNode;
  confirmLabel?: ReactNode;
  onConfirm: () => void;
  onClose: () => void;
  busy?: boolean;
  error?: unknown;
}) {
  const { t } = useTranslation();
  return (
    <Modal
      open={open}
      title={title}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button variant="danger" busy={busy} onClick={onConfirm}>
            {confirmLabel ?? t('common.delete')}
          </Button>
        </>
      }
    >
      <p className="text-sm">{message}</p>
      {!!error && <ErrorBox className="mt-3" error={error} />}
    </Modal>
  );
}

export function PageHeader({
  title,
  subtitle,
  actions,
  breadcrumbs,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  breadcrumbs?: ReactNode;
}) {
  return (
    <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        {breadcrumbs && <div className="mb-2 text-xs text-ink-subtle">{breadcrumbs}</div>}
        <h1 className="flex items-center gap-2.5 truncate text-2xl font-semibold leading-[30px] tracking-[-0.01em]">
          {title}
        </h1>
        {subtitle && <div className="mt-1.5 text-[13px] text-ink-muted">{subtitle}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Table({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">{children}</table>
    </div>
  );
}

export const th =
  'whitespace-nowrap border-b border-line bg-surface-sunken px-3 py-2 text-start text-xs font-medium text-ink-muted';
export const td = 'border-b border-line px-3 py-[7px] align-middle';

/** Left-to-right isolate for numbers, IPs, hostnames and URLs inside RTL text. */
export function Ltr({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <bdi dir="ltr" className={className}>
      {children}
    </bdi>
  );
}

/** Text direction for a formatted value: RTL when it contains Hebrew (e.g. a duration), else LTR. */
export function textDir(text: string): 'rtl' | 'ltr' {
  return /[\u0590-\u05FF]/.test(text) ? 'rtl' : 'ltr';
}

/** A formatted measurement, isolated so numbers/units read correctly in both directions. */
export function Value({ text, className }: { text: string; className?: string }) {
  return (
    <bdi dir={textDir(text)} className={className}>
      {text}
    </bdi>
  );
}

/**
 * A single LTR line (IPs, OIDs, sysDescr, target lists) truncated with an ellipsis at its
 * logical end, also inside RTL pages, aligned to the page's start edge.
 */
export function TruncLtr({
  children,
  className,
  title,
}: {
  children: ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <div dir="ltr" title={title} className={cx('truncate text-left rtl:text-right', className)}>
      {children}
    </div>
  );
}
