import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { LanguageSwitcher, ThemeToggle } from '../components/Controls';
import { Mark } from '../components/Layout';
import { PulseIcon } from '../components/icons';
import { cx } from '../components/ui';

/** Large controls of the bare screens (prototype .sso / .wide): 48–50px, radius 14. */
export const AUTH_SSO =
  'inline-flex h-12 w-full items-center justify-center gap-2.5 rounded-[14px] bg-surface text-[15px] font-medium text-ink no-underline shadow-card transition-colors hover:bg-surface-hover disabled:cursor-not-allowed disabled:opacity-50';
export const AUTH_WIDE = '!h-[50px] w-full !rounded-[14px] !text-base !font-semibold';

/** "or" between the SSO buttons and the form (prototype .or). */
export function AuthOr({ children }: { children: ReactNode }) {
  return (
    <div className="flex items-center gap-3 text-[13px] text-ink-subtle">
      <span className="h-px flex-1 bg-line" />
      {children}
      <span className="h-px flex-1 bg-line" />
    </div>
  );
}

/** The app mark as a 76px ink tile (prototype .auth .art .mark). */
function BigMark() {
  return (
    <span className="grid h-[76px] w-[76px] place-items-center rounded-[22px] bg-ink text-bg">
      <PulseIcon size={38} strokeWidth={2} />
    </span>
  );
}

/**
 * Sign-in / setup layout (prototype .auth): two halves; the art half is only the app mark,
 * "Network Monitor" and one line, the form half holds a 380px box with a 40px title.
 * `side={false}` (phone-first pages) shows the box only.
 */
export function AuthShell({
  title,
  subtitle,
  children,
  side = true,
  art,
  above,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  side?: boolean;
  /** replaces the art half's title (setup: "Welcome") */
  art?: ReactNode;
  /** shown above the title (setup: the step chips) */
  above?: ReactNode;
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
        <div className="stage-bg relative hidden flex-col items-center justify-center gap-4 overflow-hidden p-10 text-center lg:flex">
          <BigMark />
          {art ?? (
            <b dir="ltr" className="num text-[40px] font-semibold leading-[1.1] tracking-[-0.02em]">
              Network Monitor
            </b>
          )}
          <span className="text-[17px] text-ink-muted">{t('auth.oneLine')}</span>
        </div>
      )}
      <div className="flex items-center justify-center px-6 py-10">
        <div className="grid w-full max-w-[380px] gap-4">
          <div className="flex items-center justify-between gap-2">
            <span className={cx('flex items-center gap-2', side && 'lg:invisible')} dir="ltr">
              <Mark size={24} />
              <span className="num text-[15px] font-semibold">Network Monitor</span>
            </span>
            <span className="flex gap-2">
              <LanguageSwitcher />
              <ThemeToggle />
            </span>
          </div>
          <div className="mt-2 grid gap-2">
            {above}
            <h1 className="m-0 text-[40px] font-semibold leading-[1.1] tracking-[-0.02em]">
              {title}
            </h1>
            {subtitle && <p className="m-0 text-[15px] text-ink-muted">{subtitle}</p>}
          </div>
          <div>{children}</div>
        </div>
      </div>
    </div>
  );
}
