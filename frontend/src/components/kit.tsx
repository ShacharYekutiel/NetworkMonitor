// Premium v2 shared components (docs/design-system/premium/prototype.html <style> "v2"): built in slice 1
// so that later slices only assemble pages. Tokens only; no inline style attributes.
import { type ComponentType, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertIcon, CheckIcon, SparkIcon } from './icons';
import { cx } from './ui';

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

/** 20px checkbox (prototype .cb): native input, ink fill when checked. */
export const CHECKBOX =
  'h-5 w-5 shrink-0 cursor-pointer appearance-none rounded-[6px] border-[1.6px] border-line-strong bg-transparent bg-center bg-no-repeat transition-colors checked:border-accent checked:bg-accent checked:bg-[image:var(--nm-check)] checked:bg-[length:12px]';
/** 20px radio (prototype .radio): a thick ink ring when checked. */
export const RADIO =
  'h-5 w-5 shrink-0 cursor-pointer appearance-none rounded-full border-[1.6px] border-line-strong transition-[border] checked:border-[6px] checked:border-accent';

/** Option card for radio choices with a hint (prototype .opt). */
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
        className={cx(RADIO, 'mt-0.5')}
      />
      <span className="grid gap-0.5">
        <b className="font-semibold">{title}</b>
        {hint && <span className="text-[13.5px] text-ink-muted">{hint}</span>}
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
