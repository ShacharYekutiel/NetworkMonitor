import { useTranslation } from 'react-i18next';
import type { Language } from '../api/types';
import { endpoints } from '../api/endpoints';
import { setLanguage } from '../i18n';
import { useAuth } from '../hooks/auth';
import { useTheme, type ThemeChoice } from '../hooks/theme';
import { useLiveState } from '../hooks/live';
import { GlobeIcon, MonitorIcon, MoonIcon, SunIcon } from './icons';
import { cx } from './ui';

export function LanguageSwitcher({ className }: { className?: string }) {
  const { t, i18n } = useTranslation();
  const { user, setUser } = useAuth();
  const current = (i18n.language === 'he' ? 'he' : 'en') as Language;
  const choose = (lang: Language) => {
    setLanguage(lang);
    if (user && user.language !== lang) {
      endpoints
        .updateMe({ language: lang })
        .then((r) => setUser(r.user))
        .catch(() => undefined);
    }
  };
  return (
    <label className={cx('inline-flex items-center gap-1 text-sm', className)}>
      <GlobeIcon size={14} className="hidden text-slate-500 sm:block" />
      <span className="sr-only">{t('common.language')}</span>
      <select
        value={current}
        onChange={(e) => choose(e.target.value as Language)}
        className="rounded border-0 bg-transparent py-0.5 pe-6 ps-1 text-sm focus:ring-2 focus:ring-sky-500"
      >
        <option value="en">English</option>
        <option value="he">עברית</option>
      </select>
    </label>
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
