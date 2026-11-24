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
import { Link, useLocation } from 'react-router';
import { useTranslation } from 'react-i18next';
import { ChevronIcon, XIcon } from './icons';
import { CHECKBOX } from './kit';
import { errorMessage, type ErrorContext } from '../lib/errors';

export function cx(...c: (string | false | null | undefined)[]): string {
  return c.filter(Boolean).join(' ');
}

type Variant = 'primary' | 'secondary' | 'danger' | 'ghost';
/**
 * Premium buttons (prototype .pbtn / .sbtn): pills; primary is the one ink colour, secondary sits
 * on the chip ground, danger is chip ground with red text (Apple's destructive style).
 */
const VARIANTS: Record<Variant, string> = {
  primary: 'bg-accent text-accent-on hover:bg-accent-hover',
  secondary: 'bg-chip text-ink hover:bg-[color-mix(in_srgb,var(--ink)_10%,transparent)]',
  danger: 'bg-chip text-status-down hover:bg-status-down-soft',
  ghost: 'text-ink-muted hover:bg-chip hover:text-ink',
};

const BUTTON_BASE =
  'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-full border-0 font-medium transition-[background-color,color,transform] duration-200 active:scale-[0.97] focus:outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus';
const BUTTON_SIZE = { sm: 'h-8 px-3.5 text-[13px]', md: 'h-10 px-[18px] text-[14.5px]' };
/** Square icon-only buttons (always with an aria-label). */
export const ICON_BUTTON = { sm: '!w-8 !px-0', md: '!w-10 !px-0' };

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

/**
 * Inputs (prototype .input): a filled, borderless 12px-radius field with a faint inset edge so it
 * is found on grey pages; focus draws a 2px ink ring. Disabled fields are flat and dim.
 */
const inputBase =
  'block rounded-md border-0 bg-field px-3.5 py-2.5 text-[14.5px] leading-5 text-ink shadow-[inset_0_0_0_1px_var(--field-border)] placeholder:text-ink-subtle focus:shadow-[inset_0_0_0_2px_var(--focus)] focus:outline-none disabled:cursor-not-allowed disabled:bg-field-disabled disabled:text-ink-subtle disabled:shadow-[inset_0_0_0_1px_var(--border)]';

/** Full width unless the caller sets a width (`w-44` would otherwise lose to `w-full`). */
function fieldClass(className: string | undefined, extra?: string): string {
  const sized = !!className && /(^|\s)!?w-/.test(className);
  return cx(inputBase, !sized && 'w-full', extra, className);
}

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...rest }, ref) {
    return <input ref={ref} className={fieldClass(className)} {...rest} />;
  },
);

export function Select({ className, children, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={fieldClass(className, 'pe-8')} {...rest}>
      {children}
    </select>
  );
}

export function Textarea({ className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={fieldClass(className)} {...rest} />;
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
      <span className="mb-1.5 block text-[13.5px] text-ink-muted">
        {label}
        {required && <span className="ms-0.5 text-status-down">*</span>}
      </span>
      {children}
      {hint && <span className="mt-1.5 block text-xs text-ink-subtle">{hint}</span>}
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
      <span id={id} className="mb-1.5 block text-[13.5px] text-ink-muted">
        {label}
        {required && <span className="ms-0.5 text-status-down">*</span>}
      </span>
      {children}
      {hint && <span className="mt-1.5 block text-xs text-ink-subtle">{hint}</span>}
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
    <label className="inline-flex cursor-pointer items-center gap-2 text-[14.5px]">
      <input
        type="checkbox"
        className={CHECKBOX}
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
  anchor,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  /** `data-anchor` for in-page links (e.g. the Settings search). */
  anchor?: string;
}) {
  return (
    <section
      data-anchor={anchor}
      className={cx(
        // min-w-0: as a grid/flex item the card must not grow to its table's width;
        // wide tables scroll inside the card instead.
        'min-w-0 overflow-hidden rounded-xl bg-surface shadow-card',
        className,
      )}
    >
      {(title || actions) && (
        <header className="flex min-h-[56px] flex-wrap items-center justify-between gap-2.5 px-[22px] pb-2 pt-4">
          <h2 className="min-w-0 flex-1 text-[17px] font-semibold leading-snug">{title}</h2>
          {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
        </header>
      )}
      {/* bodyClassName replaces the default padding ("p-4 p-0" would keep p-4: later in the CSS). */}
      <div
        className={bodyClassName ?? (title || actions ? 'px-[22px] pb-5 pt-1' : 'px-[22px] py-5')}
      >
        {children}
      </div>
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
      className={cx('rounded-lg bg-status-down-soft px-4 py-3 text-[14.5px] text-ink', className)}
    >
      {errorMessage(t, error, context)}
    </div>
  );
}

/** Inline message (premium: a quiet rounded panel on the tone's soft ground, ink text). */
export const BANNER = 'rounded-lg px-4 py-3 text-[14px] text-ink';
export const BANNER_TONE = {
  info: 'bg-accent-soft',
  success: 'bg-status-up-soft',
  warning: 'bg-status-warning-soft',
  error: 'bg-status-down-soft',
  neutral: 'bg-chip',
} as const;

export function Notice({
  children,
  tone = 'info',
  className,
}: {
  children: ReactNode;
  tone?: keyof typeof BANNER_TONE;
  className?: string;
}) {
  return <div className={cx(BANNER, BANNER_TONE[tone], className)}>{children}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="p-8 text-center text-[14.5px] text-ink-muted">{children}</div>;
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
  sheet,
  aboveMenu,
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
  /** premium side panel (prototype .spanel): a 440px card floating at the end edge, instead of the
   * centred sheet; for explanations and detail cards */
  sheet?: boolean;
  /** [tree-move] above the menu sheet (z 51): dialogs opened from the unpinned menu's tree */
  aboveMenu?: boolean;
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
      className={cx(
        'fixed inset-0 m-0 flex bg-overlay backdrop-blur-[6px] motion-safe:animate-[nm-fade-in_.25s_ease-out]',
        aboveMenu ? 'z-[55]' : 'z-50',
        sheet
          ? 'justify-end pb-3 pe-3 pt-[76px]'
          : 'items-start justify-center overflow-y-auto p-6 pt-[8vh]',
      )}
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
          // prototype .msheet: centred card, radius 28; .spanel: 440px floating panel at the end
          'w-full rounded-[28px] bg-surface text-ink shadow-[0_30px_90px_rgba(0,0,0,0.28),0_0_0_1px_var(--border)] focus:outline-none',
          sheet
            ? 'flex h-full max-w-[min(440px,94vw)] flex-col motion-safe:animate-[nm-panel-in_.5s_cubic-bezier(.32,.72,0,1)]'
            : cx(
                'motion-safe:animate-[nm-pop-in_.45s_cubic-bezier(.32,.72,0,1)]',
                wide ? 'max-w-[760px]' : 'max-w-[560px]',
              ),
        )}
      >
        <header className="flex items-center justify-between gap-3 px-[26px] pb-2 pt-6">
          <h3
            id={titleId}
            className="min-w-0 text-2xl font-semibold leading-tight tracking-[-0.02em]"
          >
            {title}
          </h3>
          <button
            type="button"
            onClick={onClose}
            className="inline-flex h-8 w-8 items-center justify-center rounded-full bg-chip text-ink-muted hover:text-ink"
            aria-label={t('common.close')}
          >
            <XIcon size={16} />
          </button>
        </header>
        <div
          data-modal-body
          className={cx(
            'overflow-y-auto px-[26px] pb-4 pt-2',
            sheet ? 'min-h-0 flex-1' : 'max-h-[70vh]',
          )}
        >
          {children}
        </div>
        {footer && (
          // actions at the bottom start, primary first (prototype .mfoot)
          <footer className="flex flex-row-reverse flex-wrap justify-end gap-2.5 px-[26px] pb-[22px] pt-1">
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

/** iOS-style back link ("‹ Overview") above a page title; hidden on the overview itself. */
export function BackLink({ to = '/', label }: { to?: string; label?: ReactNode }) {
  const { t } = useTranslation();
  const { pathname } = useLocation();
  if (to === '/' && pathname === '/') return null;
  return (
    <Link
      to={to}
      className="-ms-2 inline-flex h-9 items-center gap-1 self-start rounded-[10px] pe-2.5 ps-1 font-medium text-ink no-underline hover:bg-chip"
    >
      <ChevronIcon size={18} strokeWidth={2} className="rotate-180 rtl:rotate-0" />
      {label ?? t('nav.short.dashboard')}
    </Link>
  );
}

/**
 * Page header (prototype .pg-h): the back link (or the breadcrumbs of a tree page), a 32–44px
 * title, a grey subline, and the actions at the end.
 */
export function PageHeader({
  title,
  subtitle,
  actions,
  breadcrumbs,
  meta,
  back,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  breadcrumbs?: ReactNode;
  /** A row of facts under the subtitle (design system: PageHeader "meta"). */
  meta?: ReactNode;
  /** false: no back link (e.g. the overview) */
  back?: false;
}) {
  return (
    <div className="mb-6 mt-1 grid gap-1.5">
      {breadcrumbs ?? (back !== false && <BackLink />)}
      <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
        <div className="min-w-0">
          <h1 className="m-0 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 break-words text-[clamp(32px,3.6vw,44px)] font-semibold leading-[1.08] tracking-[-0.025em]">
            {title}
          </h1>
          {subtitle && <div className="mt-2 text-base text-ink-muted">{subtitle}</div>}
          {meta && (
            <div className="mt-2.5 flex flex-wrap items-center gap-x-[22px] gap-y-1.5 text-sm text-ink-muted">
              {meta}
            </div>
          )}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2.5">{actions}</div>}
      </div>
    </div>
  );
}

export function Table({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm [&_tbody>tr:last-child>td]:border-b-0 [&_tbody>tr:hover>td]:bg-surface-hover">
        {children}
      </table>
    </div>
  );
}

/** Dense table cells (operator lists); the outer cells line up with the card's 22px padding. */
/** Table cells (prototype .tbl): 12.5px grey headers, hairline rows, hover card2, outer cells on the card's padding. */
export const th =
  'whitespace-nowrap border-b border-line px-3.5 pb-2.5 pt-3 text-start text-[12.5px] font-medium text-ink-muted first:ps-5 last:pe-5';
export const td =
  'border-b border-line px-3.5 py-3 align-middle transition-colors first:ps-5 last:pe-5';
/** A selected table row (prototype .tbl tr.sel) and a row with an error (tr.bad). */
export const TR_SELECTED = '[&>td]:bg-chip';
export const TR_BAD = '[&>td]:bg-status-down-soft';

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

/** A tab of a role=tablist row: a segment of the premium segmented control (prototype .seg). */
export function tabClass(on: boolean): string {
  return cx(
    'inline-flex h-8 shrink-0 items-center gap-2 whitespace-nowrap rounded-[9px] px-4 text-sm font-medium transition-[background-color,color,box-shadow] duration-200',
    on
      ? 'bg-surface text-ink shadow-[0_1px_3px_rgba(0,0,0,0.12)]'
      : 'text-ink-muted hover:text-ink',
  );
}

/** The track of a segmented control / tab row. */
export const SEG_TRACK =
  'inline-flex max-w-full gap-0.5 self-start overflow-x-auto rounded-[11px] bg-chip p-[3px] [scrollbar-width:none]';

/** A tab of a wrapping tablist (many tabs, e.g. Settings): a chip; the selected one is ink. */
export function wrapTabClass(on: boolean): string {
  return cx(
    'inline-flex h-8 items-center gap-2 whitespace-nowrap rounded-full px-3.5 text-[13.5px] font-medium transition-colors',
    on ? 'bg-accent text-accent-on' : 'bg-chip text-ink-muted hover:text-ink',
  );
}

/** Segmented control: the group and its options (prototype .seg). */
export const SEG_GROUP = SEG_TRACK;
export function segClass(on: boolean): string {
  return cx(
    'h-7 whitespace-nowrap rounded-[8px] px-3 text-[13px] font-medium transition-[background-color,color,box-shadow] duration-200',
    on
      ? 'bg-surface text-ink shadow-[0_1px_3px_rgba(0,0,0,0.12)]'
      : 'text-ink-muted hover:text-ink',
  );
}

/** A segmented control as tabs (role=tablist / tab). */
export function SegmentedControl<T extends string>({
  label,
  value,
  onChange,
  options,
  className,
  size = 'md',
}: {
  label: string;
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: ReactNode }[];
  className?: string;
  size?: 'sm' | 'md';
}) {
  return (
    <div role="tablist" aria-label={label} className={cx(SEG_TRACK, className)}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="tab"
          aria-selected={value === o.value}
          onClick={() => onChange(o.value)}
          className={size === 'sm' ? segClass(value === o.value) : tabClass(value === o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** Apple switch (prototype .sw): role=switch, 51×31, green when on. */
export function Switch({
  checked,
  onChange,
  label,
  disabled,
  className,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cx(
        'relative h-[31px] w-[51px] shrink-0 rounded-full transition-colors duration-300 disabled:cursor-not-allowed disabled:opacity-50',
        checked ? 'bg-status-up' : 'bg-chip',
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cx(
          'absolute top-[2px] h-[27px] w-[27px] rounded-full bg-white shadow-[0_2px_6px_rgba(0,0,0,0.2)] transition-[inset-inline-start] duration-300 ease-premium',
          checked ? 'start-[22px]' : 'start-[2px]',
        )}
      />
    </button>
  );
}

/** Chip: a neutral or accent label (prototype .chip). */
export const CHIP =
  'inline-flex h-7 items-center gap-1.5 whitespace-nowrap rounded-full px-3 text-[13px] font-medium';
export const CHIP_TONE = {
  neutral: 'bg-chip text-ink-muted',
  accent: 'bg-accent-soft text-accent-ink',
  ack: 'bg-ack-soft text-ack',
} as const;
export function Chip({
  tone = 'neutral',
  children,
  className,
  title,
}: {
  tone?: keyof typeof CHIP_TONE;
  children: ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <span title={title} className={cx(CHIP, CHIP_TONE[tone], className)}>
      {children}
    </span>
  );
}

/** Tag inside rows and lists (prototype .tag): a soft pill, 600 weight. */
export const TAG =
  'inline-flex h-[22px] items-center gap-1 whitespace-nowrap rounded-full px-2 align-middle text-xs font-semibold leading-none';
export const TAG_TONE = {
  neutral: 'bg-chip text-ink-muted',
  accent: 'bg-accent-soft text-accent-ink',
  warning: 'bg-status-warning-soft text-status-warning',
  down: 'bg-status-down-soft text-status-down',
  up: 'bg-status-up-soft text-status-up',
  paused: 'bg-status-paused-soft text-status-paused',
} as const;

/** A count next to a card title (neutral pill, Inter numerals). */
export function CountPill({ n, className }: { n: number; className?: string }) {
  return (
    <span
      className={cx(
        'num inline-flex h-[22px] min-w-[22px] items-center justify-center rounded-full bg-chip px-[7px] text-xs font-semibold text-ink-muted',
        className,
      )}
    >
      {n}
    </span>
  );
}

/** Key-value list (prototype .kv): grey keys, values at the end in Inter. */
export function KeyValue({
  items,
  className,
}: {
  items: { k: ReactNode; v: ReactNode; testId?: string }[];
  className?: string;
}) {
  return (
    <dl
      className={cx(
        'm-0 grid grid-cols-[auto_minmax(0,1fr)] gap-x-[18px] gap-y-2.5 text-[14.5px]',
        className,
      )}
    >
      {items.map((it, i) => (
        <div key={i} className="contents" data-testid={it.testId}>
          <dt className="text-ink-muted">{it.k}</dt>
          {/* the value keeps its own direction (bdi) but lines up with the page's end edge:
              `unicode-bidi: plaintext` on the dd aligned LTR values (IPs, dates) to the other side
              in Hebrew */}
          <dd className="m-0 min-w-0 break-words text-end">
            <bdi>{it.v}</bdi>
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** Title above a grouped list (prototype .gtitle): 22px, optional link at the end. */
export function GroupTitle({
  children,
  action,
  id,
}: {
  children: ReactNode;
  action?: ReactNode;
  id?: string;
}) {
  return (
    <div className="mx-1.5 mb-3 mt-[26px] flex items-baseline justify-between gap-3 first:mt-0">
      <h2 id={id} className="m-0 text-[22px] font-semibold tracking-[-0.01em]">
        {children}
      </h2>
      {action && <span className="text-sm text-ink-muted">{action}</span>}
    </div>
  );
}

/** Grouped list (prototype .group): rounded card, rows divided by inset hairlines. */
export function GroupList({
  children,
  className,
  label,
}: {
  children: ReactNode;
  className?: string;
  label?: string;
}) {
  return (
    <ul
      aria-label={label}
      className={cx(
        'm-0 list-none overflow-hidden rounded-xl bg-surface p-0 shadow-card',
        // hairline between rows, inset past the icon tile
        '[&>li+li]:relative [&>li+li]:before:absolute [&>li+li]:before:end-0 [&>li+li]:before:start-[66px] [&>li+li]:before:top-0 [&>li+li]:before:h-px [&>li+li]:before:bg-line',
        className,
      )}
    >
      {children}
    </ul>
  );
}

/**
 * A grouped-list row (prototype .row): icon tile or status dot, title + subline, a value, a chevron.
 * `to` makes the whole row a link; otherwise extra controls go in `end`.
 */
export function ListRow({
  icon,
  dot,
  title,
  subtitle,
  value,
  end,
  to,
  onClick,
  chevron,
  className,
  testId,
  rowProps,
  lead,
}: {
  /** extra attributes / handlers of the row (e.g. the group / device context menu) */
  rowProps?: React.HTMLAttributes<HTMLLIElement>;
  /** [tree-move] a control before the row's link (a selection checkbox), outside the link */
  lead?: ReactNode;
  icon?: ReactNode;
  /** a status dot instead of an icon tile (pass the Tailwind bg class) */
  dot?: string;
  title: ReactNode;
  subtitle?: ReactNode;
  value?: ReactNode;
  end?: ReactNode;
  to?: string;
  onClick?: () => void;
  chevron?: boolean;
  className?: string;
  testId?: string;
}) {
  const inner = (
    <>
      {dot ? (
        <span aria-hidden="true" className={cx('h-2.5 w-2.5 shrink-0 rounded-full', dot)} />
      ) : (
        icon && (
          <span className="inline-flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-[10px] bg-chip text-ink">
            {icon}
          </span>
        )
      )}
      <span className="grid min-w-0 flex-1">
        <b className="truncate text-[15.5px] font-medium">{title}</b>
        {subtitle && <span className="truncate text-[13.5px] text-ink-muted">{subtitle}</span>}
      </span>
      {value !== undefined && value !== null && (
        <span className="num shrink-0 text-sm font-medium text-ink-muted [unicode-bidi:plaintext]">
          {value}
        </span>
      )}
      {end}
      {(chevron ?? !!(to || onClick)) && (
        <ChevronIcon size={16} className="shrink-0 text-ink-subtle rtl:rotate-180" />
      )}
    </>
  );
  const cls = cx(
    'flex w-full items-center gap-3.5 px-5 py-4 text-start text-ink no-underline transition-colors',
    (to || onClick) && 'hover:bg-surface-hover',
    !!lead && 'min-w-0 flex-1 ps-3.5',
  );
  return (
    <li {...rowProps} className={cx(!!lead && 'flex items-center', className)} data-testid={testId}>
      {!!lead && <span className="flex shrink-0 items-center ps-5">{lead}</span>}
      {to ? (
        <Link to={to} className={cls}>
          {inner}
        </Link>
      ) : onClick ? (
        <button type="button" onClick={onClick} className={cls}>
          {inner}
        </button>
      ) : (
        <div className={cls}>{inner}</div>
      )}
    </li>
  );
}
