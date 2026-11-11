import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { LanguageSwitcher, ThemeToggle } from '../components/Controls';
import { Mark } from '../components/Layout';
import { cx } from '../components/ui';
import type { Status } from '../api/types';
import { STATUS_DOT } from '../lib/status';

const LEGEND: Status[] = ['up', 'warning', 'down', 'unreachable', 'paused'];

/**
 * Sign-in / setup layout (design system: Login, Setup): a `chrome` side panel with the
 * wordmark and a status legend, and the form card. `side={false}` (phone-first pages) shows
 * the card only.
 */
export function AuthShell({
  title,
  subtitle,
  children,
  side = true,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  side?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <div
      className={cx(
        'grid min-h-screen bg-bg',
        side && 'lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]',
      )}
    >
      {side && (
        <div
          dir="ltr"
          className="relative hidden flex-col justify-between overflow-hidden border-e border-chrome-border bg-chrome px-11 py-10 text-chrome-ink lg:flex"
        >
          <span className="flex items-center gap-3">
            <Mark size={24} />
            <span className="text-base font-semibold tracking-[-0.01em]">
              Network<span className="ms-[0.2em] font-medium text-chrome-muted">Monitor</span>
            </span>
          </span>
          <div>
            <p
              className="m-0 text-[44px] font-semibold leading-[1.05] tracking-[-0.02em]"
              dir="auto"
            >
              {t('auth.tagline1')}
              <br />
              <span className="text-accent">{t('auth.tagline2')}</span>
            </p>
            <svg viewBox="0 0 520 90" className="mt-7 w-full max-w-[520px]" aria-hidden="true">
              <polyline
                points="0,60 120,60 150,20 190,82 220,40 260,40 280,30 300,60 520,60"
                fill="none"
                stroke="var(--accent)"
                strokeWidth="3"
                strokeLinejoin="round"
                strokeLinecap="round"
              />
            </svg>
          </div>
          <div
            className="flex flex-wrap gap-x-[18px] gap-y-1.5 text-xs text-chrome-muted"
            dir="auto"
          >
            {LEGEND.map((s) => (
              <span key={s} className="inline-flex items-center gap-1.5">
                <span className={cx('h-2 w-2 rounded-full', STATUS_DOT[s])} />
                {t(`status.${s}`)}
              </span>
            ))}
          </div>
        </div>
      )}
      <div className="flex items-center justify-center px-4 py-8 sm:px-8">
        <div className="flex w-full max-w-[380px] flex-col gap-[18px]">
          <div className="flex items-center justify-between gap-2">
            <span className={cx('flex items-center gap-2', side && 'lg:invisible')} dir="ltr">
              <Mark size={24} />
              <span className="text-[15px] font-semibold">
                Network<span className="ms-[0.2em] font-medium text-ink-muted">Monitor</span>
              </span>
            </span>
            <span className="flex gap-2">
              <LanguageSwitcher />
              <ThemeToggle />
            </span>
          </div>
          <div>
            <h1 className="m-0 text-[28px] font-semibold leading-[34px] tracking-[-0.01em]">
              {title}
            </h1>
            {subtitle && <p className="mt-1.5 text-sm text-ink-muted">{subtitle}</p>}
          </div>
          <div>{children}</div>
        </div>
      </div>
    </div>
  );
}
