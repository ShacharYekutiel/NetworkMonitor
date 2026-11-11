import { useEffect, useId, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { Language } from '../api/types';
import { endpoints } from '../api/endpoints';
import { setLanguage } from '../i18n';
import { useAuth } from '../hooks/auth';
import { useTheme, type ThemeChoice } from '../hooks/theme';
import { useLiveState } from '../hooks/live';
import { GlobeIcon, MonitorIcon, MoonIcon, SunIcon } from './icons';
import { cx } from './ui';

const LANGUAGES: { code: Language; label: string }[] = [
  { code: 'en', label: 'English' },
  { code: 'he', label: 'עברית' },
];

export function LanguageSwitcher({ className }: { className?: string }) {
  const { t, i18n } = useTranslation();
  const { user, setUser } = useAuth();
  const current = (i18n.language === 'he' ? 'he' : 'en') as Language;
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return;
    const onPointer = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setOpen(false);
        buttonRef.current?.focus();
      }
    };
    document.addEventListener('mousedown', onPointer);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onPointer);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const choose = (lang: Language) => {
    setOpen(false);
    buttonRef.current?.focus();
    if (lang === current) return;
    setLanguage(lang);
    if (user && user.language !== lang) {
      endpoints
        .updateMe({ language: lang })
        .then((r) => setUser(r.user))
        .catch(() => undefined);
    }
  };

  const currentLabel = LANGUAGES.find((l) => l.code === current)?.label ?? current;

  return (
    <div ref={rootRef} className={cx('relative inline-flex text-sm', className)}>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={menuId}
        aria-label={`${t('common.language')}: ${currentLabel}`}
        className="inline-flex h-[26px] items-center gap-[5px] rounded-sm border border-chrome-border px-2 font-mono text-[11px] font-semibold leading-none text-chrome-muted hover:bg-chrome-field hover:text-chrome-ink"
      >
        <GlobeIcon size={14} strokeWidth={1.75} />
        <span lang={current}>{current === 'he' ? 'עב' : 'EN'}</span>
        <svg
          width="10"
          height="10"
          viewBox="0 0 10 10"
          aria-hidden="true"
          className={cx('transition-transform', open && 'rotate-180')}
        >
          <path d="M1 3l4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.5" />
        </svg>
      </button>
      {open && (
        <ul
          id={menuId}
          role="listbox"
          aria-label={t('common.language')}
          className="absolute end-0 top-full z-50 mt-1 min-w-[8rem] overflow-hidden rounded-lg bg-surface-raised p-1 text-[13px] text-ink shadow-pop"
        >
          {LANGUAGES.map((l) => {
            const selected = l.code === current;
            return (
              <li key={l.code} role="option" aria-selected={selected}>
                <button
                  type="button"
                  lang={l.code}
                  dir={l.code === 'he' ? 'rtl' : 'ltr'}
                  onClick={() => choose(l.code)}
                  className={cx(
                    'flex w-full items-center justify-between gap-3 rounded-sm px-[9px] py-[7px] text-start',
                    selected
                      ? 'bg-accent-soft font-medium text-accent-ink'
                      : 'hover:bg-surface-hover',
                  )}
                >
                  <span>{l.label}</span>
                  {selected && <span aria-hidden="true">✓</span>}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

const NEXT: Record<ThemeChoice, ThemeChoice> = { system: 'light', light: 'dark', dark: 'system' };

export function ThemeToggle() {
  const { t } = useTranslation();
  const { choice, setChoice } = useTheme();
  const Icon = choice === 'dark' ? MoonIcon : choice === 'light' ? SunIcon : MonitorIcon;
  const label = `${t('theme.label')}: ${t(`theme.${choice}`)}`;
  return (
    <button
      type="button"
      onClick={() => setChoice(NEXT[choice])}
      className="inline-flex h-[34px] w-[34px] items-center justify-center rounded-md text-chrome-muted hover:bg-chrome-field hover:text-chrome-ink"
      title={label}
      aria-label={label}
    >
      <Icon size={16} strokeWidth={1.75} />
    </button>
  );
}

export function ConnectionIndicator() {
  const { t } = useTranslation();
  const state = useLiveState();
  // No blinking (design system § Effects): the dot colour alone changes, next to its word.
  const color =
    state === 'open'
      ? 'bg-status-up'
      : state === 'connecting'
        ? 'bg-status-warning'
        : 'bg-status-down';
  return (
    <span
      className="inline-flex items-center gap-1.5 text-xs font-medium leading-none text-chrome-muted"
      title={t(`live.${state}`)}
      data-testid="ws-indicator"
    >
      <span className={cx('h-[7px] w-[7px] shrink-0 rounded-full', color)} />
      <span className="hidden sm:inline">{t(`live.${state}`)}</span>
    </span>
  );
}

function pad(n: number) {
  return String(n).padStart(2, '0');
}

/** Header clock (local time, 24h, LTR), seconds in a smaller muted size. */
export function HeaderClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(id);
  }, []);
  return (
    <time
      dir="ltr"
      dateTime={now.toISOString()}
      className="font-mono text-[13px] font-medium leading-none text-chrome-ink"
    >
      {pad(now.getHours())}:{pad(now.getMinutes())}
      <small className="text-[11px] text-chrome-muted">:{pad(now.getSeconds())}</small>
    </time>
  );
}
