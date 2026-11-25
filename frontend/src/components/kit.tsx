// Premium v2 shared components (docs/design-system/premium/prototype.html <style> "v2"): built in slice 1
// so that later slices only assemble pages. Tokens only; no inline style attributes.
import { type ComponentType, type ReactNode, useEffect, useId, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  AlertIcon,
  ApIcon,
  CheckIcon,
  ChevronIcon,
  DeviceIcon,
  NasIcon,
  PCIcon,
  PrinterIcon,
  RouterIcon,
  ServerStackIcon,
  ShieldIcon,
  SparkIcon,
} from './icons';
import { Link } from 'react-router';
import { cx, textDir } from './ui';
import type { Severity } from '../api/severity';
import { severityRank } from '../lib/severity';

type IconC = ComponentType<{ size?: number; strokeWidth?: number; className?: string }>;

/** Empty / error block (prototype .empty): 64px icon tile, 22px title, one line, one or two actions. */
export function EmptyState({
  icon: Icon,
  title,
  children,
  actions,
  error,
  className,
}: {
  icon?: IconC;
  title: ReactNode;
  children?: ReactNode;
  actions?: ReactNode;
  error?: boolean;
  className?: string;
}) {
  const I = Icon ?? (error ? AlertIcon : null);
  return (
    <div
      role={error ? 'alert' : undefined}
      className={cx('grid justify-items-center gap-2.5 px-6 py-14 text-center', className)}
    >
      {I && (
        <span
          className={cx(
            'mb-1 grid h-16 w-16 place-items-center rounded-[20px]',
            error ? 'bg-status-down-soft text-status-down' : 'bg-chip text-ink-muted',
          )}
        >
          <I size={28} strokeWidth={1.7} />
        </span>
      )}
      <h3 className="m-0 text-[22px] font-semibold tracking-[-0.01em]">{title}</h3>
      {children && <p className="m-0 max-w-[440px] text-ink-muted">{children}</p>}
      {actions && <div className="mt-2 flex flex-wrap justify-center gap-2.5">{actions}</div>}
    </div>
  );
}

const BANNER_TONES = {
  neutral: { bg: 'bg-chip', ic: 'text-ink-muted' },
  info: { bg: 'bg-accent-soft', ic: 'text-accent-ink' },
  up: { bg: 'bg-status-up-soft', ic: 'text-status-up' },
  warn: { bg: 'bg-status-warning-soft', ic: 'text-status-warning' },
  down: { bg: 'bg-status-down-soft', ic: 'text-status-down' },
} as const;

/** In-page notice (prototype .banner): 16px radius, tinted by status (warn = limits, down = failures). */
export function Banner({
  tone = 'neutral',
  icon: Icon = AlertIcon,
  title,
  children,
  actions,
  className,
}: {
  tone?: keyof typeof BANNER_TONES;
  icon?: IconC;
  title: ReactNode;
  children?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  const t = BANNER_TONES[tone];
  return (
    <div
      role={tone === 'down' ? 'alert' : 'status'}
      className={cx(
        'flex items-start gap-3 rounded-lg px-4 py-3.5 text-[14.5px] text-ink',
        t.bg,
        className,
      )}
    >
      <Icon size={20} strokeWidth={1.7} className={cx('mt-px shrink-0', t.ic)} />
      <div className="grid flex-1 gap-0.5">
        <b className="font-semibold">{title}</b>
        {children && <span className="text-[13.5px] text-ink-muted">{children}</span>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap gap-2">{actions}</div>}
    </div>
  );
}

/** Wizard steps (prototype .wsteps): numbered pills, ✓ when done; done steps go back on click. */
export function WizardSteps({
  steps,
  current,
  onGo,
  label,
}: {
  steps: ReactNode[];
  current: number;
  onGo?: (i: number) => void;
  label: string;
}) {
  return (
    <ol aria-label={label} className="m-0 mb-[22px] flex list-none flex-wrap gap-1.5 p-0">
      {steps.map((s, i) => {
        const done = i < current;
        const on = i === current;
        const pill = cx(
          'inline-flex h-9 items-center gap-2 rounded-full pe-3.5 ps-2 text-sm font-medium',
          on ? 'bg-accent text-accent-on' : 'bg-chip',
          done ? 'text-ink' : !on && 'text-ink-muted',
        );
        const dot = (
          <i
            className={cx(
              'num inline-grid h-[22px] w-[22px] place-items-center rounded-full text-xs font-semibold not-italic',
              on
                ? 'bg-accent-on text-accent'
                : done
                  ? 'bg-status-up text-white'
                  : 'bg-surface text-ink-muted',
            )}
          >
            {done ? <CheckIcon size={12} strokeWidth={3} /> : i + 1}
          </i>
        );
        return (
          <li key={i} aria-current={on ? 'step' : undefined}>
            {done && onGo ? (
              <button type="button" className={pill} onClick={() => onGo(i)}>
                {dot}
                {s}
              </button>
            ) : (
              <span className={pill}>
                {dot}
                {s}
              </span>
            )}
          </li>
        );
      })}
    </ol>
  );
}

/**
 * 20px checkbox (prototype .cb): native input, ink fill when checked. A tri-state "select all" in
 * the mixed state (`input.indeterminate`) shows a dash on the same fill (the indeterminate
 * variants come after `checked:` in Tailwind's order, so they win when both apply).
 */
export const CHECKBOX =
  'h-5 w-5 shrink-0 cursor-pointer appearance-none rounded-[6px] border-[1.6px] border-line-strong bg-transparent bg-center bg-no-repeat transition-colors checked:border-accent checked:bg-accent checked:bg-[image:var(--nm-check)] checked:bg-[length:12px] indeterminate:border-accent indeterminate:bg-accent indeterminate:bg-[image:var(--nm-dash)] indeterminate:bg-[length:12px]';
/** 20px radio (prototype .radio): a thick ink ring when checked. */
export const RADIO =
  'h-5 w-5 shrink-0 cursor-pointer appearance-none rounded-full border-[1.6px] border-line-strong transition-[border] checked:border-[6px] checked:border-accent';

/**
 * Option card for radio choices with a hint (prototype .opt). The title names the radio and the
 * hint describes it, so the accessible name stays the option's word.
 */
export function OptionCard({
  name,
  value,
  checked,
  onChange,
  title,
  hint,
}: {
  name: string;
  value: string;
  checked: boolean;
  onChange: (v: string) => void;
  title: ReactNode;
  hint?: ReactNode;
}) {
  const id = useId();
  return (
    <label
      className={cx(
        'flex w-full cursor-pointer items-start gap-3 rounded-lg px-4 py-3.5 text-start transition-colors',
        checked ? 'bg-surface shadow-[inset_0_0_0_2px_var(--ink)]' : 'bg-chip',
      )}
    >
      <input
        type="radio"
        name={name}
        value={value}
        checked={checked}
        onChange={() => onChange(value)}
        aria-labelledby={`${id}-t`}
        aria-describedby={hint ? `${id}-h` : undefined}
        className={cx(RADIO, 'mt-0.5')}
      />
      <span className="grid gap-0.5">
        <b id={`${id}-t`} className="font-semibold">
          {title}
        </b>
        {hint && (
          <span id={`${id}-h`} className="text-[13.5px] text-ink-muted">
            {hint}
          </span>
        )}
      </span>
    </label>
  );
}

/** Citation chip [n] in AI text (prototype .cite); the caller opens its popover. */
export function CitationChip({
  n,
  on,
  onClick,
  label,
}: {
  n: number;
  on?: boolean;
  onClick?: () => void;
  label?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      aria-pressed={on}
      className={cx(
        'num ms-0.5 inline-flex h-[19px] min-w-[19px] items-center justify-center rounded-[6px] px-[5px] align-[2px] text-[11px] font-semibold transition-colors',
        on
          ? 'bg-accent text-accent-on'
          : 'bg-chip text-ink-muted hover:bg-accent hover:text-accent-on',
      )}
    >
      {n}
    </button>
  );
}

/** "AI ✦" tag on anything generated (prototype .aitag), with the optional fine print. */
export function AiTag({ fine }: { fine?: boolean }) {
  const { t } = useTranslation();
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <span className="num inline-flex h-[22px] items-center gap-[5px] rounded-[7px] bg-chip px-2 text-[11.5px] font-semibold tracking-[0.02em] text-ink-muted">
        AI
        <SparkIcon size={13} />
      </span>
      {fine && <span className="text-[12.5px] text-ink-subtle">{t('kit.aiFine')}</span>}
    </span>
  );
}

/** Progress toast body (prototype .ptoast): title, n / total, a bar that turns green when done. */
export function ProgressCard({
  title,
  done,
  total,
  onCancel,
  cancelLabel,
}: {
  title: ReactNode;
  done: number;
  total: number;
  onCancel?: () => void;
  cancelLabel?: string;
}) {
  const pct = total ? Math.min(100, (done / total) * 100) : 0;
  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed bottom-6 end-6 z-[60] grid w-[340px] gap-2.5 rounded-[20px] bg-surface px-[18px] py-4 shadow-[0_18px_60px_rgba(0,0,0,0.2),0_0_0_1px_var(--border)]"
    >
      <div className="flex items-center justify-between gap-2.5 font-medium">
        <span className="truncate">{title}</span>
        <span dir="ltr" className="num text-[13px] text-ink-muted">
          {done} / {total}
        </span>
      </div>
      <progress
        value={pct}
        max={100}
        className={cx(
          'h-1.5 w-full appearance-none overflow-hidden rounded-full bg-chip [&::-webkit-progress-bar]:bg-chip [&::-webkit-progress-value]:rounded-full [&::-moz-progress-bar]:rounded-full',
          done >= total
            ? '[&::-moz-progress-bar]:bg-status-up [&::-webkit-progress-value]:bg-status-up'
            : '[&::-moz-progress-bar]:bg-ink [&::-webkit-progress-value]:bg-ink',
        )}
      />
      {onCancel && done < total && (
        <button
          type="button"
          onClick={onCancel}
          className="justify-self-start text-[13px] text-ink-muted hover:text-ink"
        >
          {cancelLabel}
        </button>
      )}
    </div>
  );
}

/**
 * Single-choice filter chips (prototype .chips[data-single] / .chip): 32px pills, the chosen one in
 * ink. Buttons with aria-pressed inside a labelled group.
 */
export function FilterChips<T extends string>({
  label,
  value,
  onChange,
  options,
  className,
  testId,
}: {
  label: string;
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: ReactNode }[];
  className?: string;
  testId?: string;
}) {
  return (
    <div
      role="group"
      aria-label={label}
      data-testid={testId}
      className={cx('flex flex-wrap gap-2', className)}
    >
      {options.map((o) => {
        const on = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            aria-pressed={on}
            onClick={() => onChange(o.value)}
            className={cx(
              'inline-flex h-8 items-center gap-1.5 whitespace-nowrap rounded-full px-3.5 text-[13.5px] font-medium transition-colors',
              on ? 'bg-accent text-accent-on' : 'bg-chip text-ink-muted hover:text-ink',
            )}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

const TL_DOT: Record<string, string> = {
  up: 'bg-status-up',
  warning: 'bg-status-warning',
  down: 'bg-status-down',
  paused: 'bg-status-paused',
  unknown: 'bg-status-unknown',
  unreachable: 'bg-status-unreachable',
  neutral: 'bg-ink-subtle',
};

/** Vertical timeline (prototype .tl): time, a line with a status dot, a bold line and a grey one. */
export function Timeline({
  children,
  label,
  testId,
}: {
  children: ReactNode;
  label?: string;
  testId?: string;
}) {
  return (
    <ol
      aria-label={label}
      data-testid={testId}
      className="m-0 grid list-none p-0 [&>li:last-child>div:last-child]:border-b-0"
    >
      {children}
    </ol>
  );
}

/** One timeline entry; `tone` is a status (or "neutral") and colours the dot. */
export function TimelineEntry({
  time,
  timeTitle,
  tone = 'neutral',
  title,
  children,
  end,
  testId,
  rowProps,
}: {
  /** extra attributes / handlers of the entry (e.g. the device / sensor context menu) */
  rowProps?: React.HTMLAttributes<HTMLLIElement>;
  time: ReactNode;
  timeTitle?: string;
  tone?: string;
  title: ReactNode;
  children?: ReactNode;
  end?: ReactNode;
  testId?: string;
}) {
  return (
    <li
      {...rowProps}
      data-testid={testId}
      className="grid grid-cols-[76px_22px_minmax(0,1fr)] gap-3 max-sm:grid-cols-[58px_18px_minmax(0,1fr)] max-sm:gap-2"
    >
      {/* aligned to the line in both directions; the text keeps its own direction ("10:44",
          "30 דק׳") */}
      <span
        title={timeTitle}
        className="num whitespace-nowrap pt-4 text-end text-sm font-medium text-ink-muted"
      >
        <bdi dir={typeof time === 'string' ? textDir(time) : 'ltr'}>{time}</bdi>
      </span>
      <span
        aria-hidden="true"
        className="relative flex justify-center before:absolute before:inset-y-0 before:w-0.5 before:bg-line"
      >
        <span
          className={cx(
            'relative mt-[18px] h-3 w-3 rounded-full shadow-[0_0_0_4px_var(--surface)]',
            TL_DOT[tone] ?? TL_DOT.neutral,
          )}
        />
      </span>
      <div className="flex min-w-0 items-start gap-3 border-b border-line pb-4 pt-3.5">
        <div className="grid min-w-0 flex-1 gap-0.5">
          <b className="min-w-0 break-words font-semibold">{title}</b>
          {children && (
            <span className="min-w-0 break-words text-sm text-ink-muted">{children}</span>
          )}
        </div>
        {end && <div className="flex shrink-0 items-center gap-2">{end}</div>}
      </div>
    </li>
  );
}

const MCHIP_DOT: Record<string, string> = TL_DOT;

/** Member chip of an incident (prototype .mchip): Inter, LTR, a status dot when it has a state. */
export function MemberChip({
  tone,
  children,
  title,
}: {
  tone?: string;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span
      title={title}
      className="inline-flex font-display h-[26px] items-center gap-1.5 whitespace-nowrap rounded-full bg-chip px-2.5 text-[12.5px] font-medium text-ink"
    >
      {tone && (
        <i
          aria-hidden="true"
          className={cx('h-[7px] w-[7px] rounded-full', MCHIP_DOT[tone] ?? MCHIP_DOT.neutral)}
        />
      )}
      {children}
    </span>
  );
}

const CLASS_TILE: Record<string, IconC> = {
  switch: DeviceIcon,
  router: RouterIcon,
  access_point: ApIcon,
  printer: PrinterIcon,
  nas: NasIcon,
  server: ServerStackIcon,
  workstation: PCIcon,
};

/**
 * Icon of a device row's tile (BUILD.md § 6 "Group page": the normal icon tile, switch / shield for
 * firewalls). By the device class; a firewall is recognised by its name, as the home map does.
 */
export function DeviceTileIcon({
  cls,
  name,
  size = 18,
}: {
  cls?: string | null;
  name?: string;
  size?: number;
}) {
  const fw = /(^|[^a-z])(fw|firewall|fortigate|asa|pfsense|palo)([^a-z]|$)/i.test(name ?? '');
  const Icon: IconC = fw ? ShieldIcon : (cls && CLASS_TILE[cls]) || DeviceIcon;
  return <Icon size={size} strokeWidth={1.8} />;
}

/** A share bar (prototype .share): 6px (8px when `thick`), ink on a chip track. */
export function ShareBar({ pct, thick }: { pct: number; thick?: boolean }) {
  return (
    <progress
      value={Math.max(0, Math.min(100, pct))}
      max={100}
      aria-hidden="true"
      className={cx(
        'block w-full appearance-none overflow-hidden rounded-full bg-chip [&::-moz-progress-bar]:rounded-full [&::-moz-progress-bar]:bg-ink [&::-webkit-progress-bar]:bg-chip [&::-webkit-progress-value]:rounded-full [&::-webkit-progress-value]:bg-ink',
        thick ? 'h-2' : 'h-1.5',
      )}
    />
  );
}

/**
 * A multi-choice chip (prototype .chips / .chip.on): a real checkbox inside a 32px pill, ink when
 * checked, so forms keep their checkbox semantics and labels.
 */
export function ToggleChip({
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
    <label
      className={cx(
        'relative inline-flex h-8 cursor-pointer select-none items-center gap-1.5 whitespace-nowrap rounded-full px-3.5 text-[13.5px] font-medium transition-colors has-[:focus-visible]:outline has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-[var(--focus)]',
        checked ? 'bg-accent text-accent-on' : 'bg-chip text-ink-muted hover:text-ink',
        disabled && 'cursor-not-allowed opacity-50',
      )}
    >
      <input
        type="checkbox"
        // transparent and over the whole chip: the checkbox itself takes the click
        className="absolute inset-0 m-0 h-full w-full cursor-pointer appearance-none rounded-full opacity-0 disabled:cursor-not-allowed"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      {label}
    </label>
  );
}

/**
 * A small popover (prototype .pop): a button that opens a floating card under it; closes on
 * Escape and on a click outside.
 */
export function Popover({
  button,
  label,
  children,
  className,
  panelClassName,
  testId,
}: {
  button: ReactNode;
  /** accessible name of the button */
  label: string;
  children: ReactNode;
  className?: string;
  /** replaces the panel width (default min(360px, 90vw)) */
  panelClassName?: string;
  testId?: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const id = useId();
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);
  return (
    <div ref={ref} className="relative inline-block">
      <button
        type="button"
        aria-label={label}
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((o) => !o)}
        className={cx(
          'inline-flex h-8 items-center gap-2 whitespace-nowrap rounded-full bg-chip px-3.5 text-[13px] font-medium text-ink-muted transition-colors hover:text-ink',
          className,
        )}
      >
        {button}
      </button>
      {open && (
        <div
          id={id}
          data-testid={testId}
          className={cx(
            'absolute end-0 top-full z-40 mt-2 rounded-[20px] bg-surface p-4 text-sm text-ink shadow-[0_18px_60px_rgba(0,0,0,0.18),0_0_0_1px_var(--border)] motion-safe:animate-[nm-pop-in_.3s_cubic-bezier(.32,.72,0,1)]',
            panelClassName ?? 'w-[min(360px,90vw)]',
          )}
        >
          {children}
        </div>
      )}
    </div>
  );
}

/**
 * A grouped-list row with its own controls (prototype .row with buttons at the end): the main part
 * is a link (or a button) with a status dot or icon tile, a title and a grey line; `end` holds
 * buttons, switches or tags that must not sit inside the link.
 */
export function ActionRow({
  icon,
  lead,
  dot,
  title,
  subtitle,
  to,
  onClick,
  end,
  dim,
  wrap,
  selected,
  testId,
  rowProps,
}: {
  /** extra attributes / handlers of the row (e.g. the group / device context menu) */
  rowProps?: React.HTMLAttributes<HTMLLIElement>;
  icon?: ReactNode;
  /** shown as is in place of the icon tile (e.g. an Avatar) */
  lead?: ReactNode;
  /** the row shown in a detail card next to the list (prototype: selected row = --chip) */
  selected?: boolean;
  /** the grey line may wrap (rows with several controls at the end) */
  wrap?: boolean;
  /** a status dot instead of an icon tile (Tailwind bg class) */
  dot?: string;
  title: ReactNode;
  subtitle?: ReactNode;
  to?: string;
  onClick?: () => void;
  end?: ReactNode;
  /** a disabled item: muted */
  dim?: boolean;
  testId?: string;
}) {
  const body = (
    <>
      {lead ??
        (dot ? (
          <span aria-hidden="true" className={cx('h-2.5 w-2.5 shrink-0 rounded-full', dot)} />
        ) : (
          icon && (
            <span className="inline-flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-[10px] bg-chip text-ink">
              {icon}
            </span>
          )
        ))}
      <span className="grid min-w-0 flex-1">
        <b className="truncate text-[15.5px] font-medium">{title}</b>
        {subtitle && (
          <span className={cx('text-[13.5px] text-ink-muted', !wrap && 'truncate')}>
            {subtitle}
          </span>
        )}
      </span>
    </>
  );
  const main =
    'flex min-w-0 flex-1 items-center gap-3.5 py-4 ps-5 text-start text-ink no-underline';
  return (
    <li
      {...rowProps}
      data-testid={testId}
      className={cx(
        'flex items-center gap-2 pe-4 transition-colors',
        selected ? 'bg-chip' : (to || onClick) && 'hover:bg-surface-hover',
        dim && 'opacity-60',
      )}
    >
      {to ? (
        <Link to={to} className={main}>
          {body}
          {!end && <ChevronIcon size={16} className="shrink-0 text-ink-subtle rtl:rotate-180" />}
        </Link>
      ) : onClick ? (
        <button
          type="button"
          onClick={onClick}
          className={main}
          aria-current={selected ? 'true' : undefined}
        >
          {body}
        </button>
      ) : (
        <div className={main}>{body}</div>
      )}
      {end && <div className="flex shrink-0 flex-wrap items-center justify-end gap-1">{end}</div>}
    </li>
  );
}

const AVATAR_SIZE = {
  sm: 'h-9 w-9 text-[14px]',
  md: 'h-[38px] w-[38px] text-[15px]',
  lg: 'h-16 w-16 text-[26px]',
} as const;

/** A person's round initial (prototype .me): grey gradient, white Inter initial. */
export function Avatar({ name, size = 'md' }: { name: string; size?: keyof typeof AVATAR_SIZE }) {
  const initial = [...(name.trim() || '?')][0].toUpperCase();
  return (
    <span
      aria-hidden="true"
      className={cx(
        'num inline-flex shrink-0 items-center justify-center rounded-full bg-[linear-gradient(135deg,#8e8e93,#3a3a3c)] font-semibold text-white',
        AVATAR_SIZE[size],
      )}
    >
      {initial}
    </span>
  );
}

/**
 * A labelled row with an Apple switch (prototype .popmenu rows with .sw): a native checkbox drawn
 * as the switch, so lists of on/off choices keep checkbox semantics.
 */
export function SwitchRow({
  label,
  checked,
  onChange,
}: {
  label: ReactNode;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className="flex min-h-[38px] cursor-pointer items-center justify-between gap-3 rounded-[10px] px-2.5 text-sm hover:bg-chip">
      <span className="min-w-0 truncate">{label}</span>
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className={cx(
          "relative h-[22px] w-9 shrink-0 cursor-pointer appearance-none rounded-full bg-chip shadow-[inset_0_0_0_1px_var(--border)] transition-colors duration-300 before:absolute before:start-0.5 before:top-0.5 before:h-[18px] before:w-[18px] before:rounded-full before:bg-white before:shadow-[0_1px_3px_rgba(0,0,0,0.25)] before:transition-[inset-inline-start] before:duration-300 before:content-[''] checked:bg-status-up checked:shadow-none checked:before:start-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]",
        )}
      />
    </label>
  );
}

const SEVERITY_TONE: Record<Severity, string> = {
  critical: 'bg-severity-critical text-severity-on-critical',
  major: 'bg-severity-major text-severity-on-major',
  minor: 'bg-severity-minor-soft text-severity-minor',
  warning: 'bg-severity-warning-soft text-severity-warning',
  info: 'bg-severity-info-soft text-severity-info',
};

/** Four rising bars, as many filled as the level is above "info" (order without colour). */
export function SeverityBars({ severity, className }: { severity: Severity; className?: string }) {
  const filled = severityRank(severity) - 1;
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 12 10"
      width={12}
      height={10}
      className={cx('shrink-0', className)}
    >
      {[0, 1, 2, 3].map((i) => (
        <rect
          key={i}
          x={i * 3}
          y={7 - i * 2.33}
          width={2}
          height={3 + i * 2.33}
          rx={0.6}
          fill="currentColor"
          opacity={i < filled ? 1 : 0.28}
        />
      ))}
    </svg>
  );
}

/**
 * [severity] Alert severity tag: a pill in the level's colour that always carries its word (never
 * colour alone) and rising bars for the order. Critical and Major are solid, the others soft, so
 * they never read as a status tag. Hebrew UI shows the Hebrew word, the English one is in the
 * tooltip.
 */
export function SeverityTag({
  severity,
  size = 'sm',
  className,
  testId = 'severity-tag',
  lng,
}: {
  severity: Severity | null | undefined;
  size?: 'sm' | 'md';
  className?: string;
  testId?: string;
  /** a fixed language (e.g. a report's), else the UI language */
  lng?: string;
}) {
  const { t } = useTranslation();
  if (!severity || !(severity in SEVERITY_TONE)) return null;
  const word = t(`severity.level.${severity}`, { lng });
  const en = t(`severity.level.${severity}`, { lng: 'en' });
  const title = t('severity.tagTitle', { level: word, lng }) + (word !== en ? ` (${en})` : '');
  return (
    <span
      data-testid={testId}
      data-severity={severity}
      title={title}
      className={cx(
        'inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-full align-middle font-semibold leading-none',
        size === 'md' ? 'h-[26px] pe-2.5 ps-2 text-[13px]' : 'h-[22px] pe-2 ps-1.5 text-xs',
        SEVERITY_TONE[severity],
        className,
      )}
    >
      <SeverityBars severity={severity} />
      {word}
    </span>
  );
}
