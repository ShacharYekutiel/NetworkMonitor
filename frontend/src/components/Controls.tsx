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
        className="inline-flex items-center gap-1.5 rounded px-2 py-1 text-slate-700 hover:bg-slate-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 dark:text-slate-200 dark:hover:bg-slate-800"
      >
        <GlobeIcon size={14} className="text-slate-500 dark:text-slate-400" />
        <span>{currentLabel}</span>
        <svg
          width="10"
          height="10"
          viewBox="0 0 10 10"
          aria-hidden="true"
          className={cx('text-slate-400 transition-transform', open && 'rotate-180')}
        >
          <path d="M1 3l4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.5" />
        </svg>
      </button>
      {open && (
        <ul
          id={menuId}
          role="listbox"
          aria-label={t('common.language')}
          className="absolute end-0 top-full z-50 mt-1 min-w-[8rem] overflow-hidden rounded-md border border-slate-200 bg-white py-1 shadow-lg dark:border-slate-700 dark:bg-slate-900"
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
                    'flex w-full items-center justify-between gap-3 px-3 py-1.5 text-start',
                    selected
                      ? 'bg-sky-50 font-medium text-sky-700 dark:bg-sky-950 dark:text-sky-300'
                      : 'text-slate-700 hover:bg-slate-100 dark:text-slate-200 dark:hover:bg-slate-800',
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
      className="rounded p-1.5 text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
      title={label}
      aria-label={label}
    >
      <Icon size={16} />
    </button>
  );
}

export function ConnectionIndicator() {
  const { t } = useTranslation();
  const state = useLiveState();
  const color =
    state === 'open'
      ? 'bg-green-600'
      : state === 'connecting'
        ? 'bg-amber-500 animate-pulse'
        : 'bg-red-600';
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs text-slate-600 ring-1 ring-slate-200 dark:text-slate-300 dark:ring-slate-700"
      title={t(`live.${state}`)}
      data-testid="ws-indicator"
    >
      <span className={cx('h-2 w-2 rounded-full', color)} />
      <span className="hidden sm:inline">{t(`live.${state}`)}</span>
    </span>
  );
}
