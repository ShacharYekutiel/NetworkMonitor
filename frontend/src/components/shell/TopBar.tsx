import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../../hooks/auth';
import { parseSearchInput } from '../../lib/ports';
import { LanguageSwitcher, ThemeToggle } from '../Controls';
import { KeyIcon, LogoutIcon, MenuIcon, SearchIcon, UsersIcon } from '../icons';
import { cx } from '../ui';
import { Island } from './Island';

/** The product mark: an ink tile with the pulse line in the page colour. */
export function Mark({ size = 26, className }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      aria-hidden="true"
      className={cx('shrink-0 text-ink', className)}
    >
      <rect width="32" height="32" rx="8" fill="currentColor" />
      <path
        d="M5 18h6l3-8 5 14 3-6h6"
        fill="none"
        className="stroke-bg"
        strokeWidth="2.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Round 36px tool button of the top bar. */
export const ROUND =
  'inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-chrome-muted transition-colors hover:bg-chrome-field hover:text-chrome-ink';

/** [2b-2] MAC/IP quick search: the round button opens a field; Enter opens the Find page. */
function QuickSearch() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [v, setV] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    inputRef.current?.focus();
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);
  return (
    <div ref={rootRef} className="relative flex items-center">
      <button
        type="button"
        className={cx(ROUND, open && 'bg-chrome-field text-chrome-ink')}
        aria-label={t('nav.search')}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <SearchIcon size={20} strokeWidth={1.7} />
      </button>
      {open && (
        <form
          role="search"
          className="absolute end-0 top-[calc(100%+10px)] z-40 w-[340px] animate-[nm-fade-in_.2s_ease-out]"
          onSubmit={(e) => {
            e.preventDefault();
            const raw = v.trim();
            const p = parseSearchInput(raw);
            // An invalid query still goes to the Find page, which shows the validation error.
            const qs = new URLSearchParams(
              p ? (p as Record<string, string>) : { q: raw },
            ).toString();
            navigate(raw ? `/find?${qs}` : '/find');
            setV('');
            setOpen(false);
          }}
          onKeyDown={(e) => {
            if (e.key === 'Escape') setOpen(false);
          }}
        >
          <SearchIcon
            size={18}
            strokeWidth={1.7}
            className="pointer-events-none absolute start-4 top-1/2 -translate-y-1/2 text-ink-muted"
          />
          <input
            ref={inputRef}
            value={v}
            onChange={(e) => setV(e.target.value)}
            placeholder={t('find.headerPlaceholder')}
            aria-label={t('find.label')}
            // MACs/IPs are LTR; the (translated) placeholder follows the page direction.
            dir={v ? 'ltr' : undefined}
            className="num block h-12 w-full rounded-full border-0 bg-surface-raised pe-4 ps-11 text-[15px] text-ink shadow-pop placeholder:font-sans placeholder:text-ink-subtle focus:outline focus:outline-2 focus:outline-offset-0 focus:outline-focus"
          />
        </form>
      )}
    </div>
  );
}

function UserMenu() {
  const { t } = useTranslation();
  const { user, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const k = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false);
    document.addEventListener('mousedown', h);
    document.addEventListener('keydown', k);
    return () => {
      document.removeEventListener('mousedown', h);
      document.removeEventListener('keydown', k);
    };
  }, [open]);
  if (!user) return null;
  const name = user.display_name || user.username;
  const item =
    'flex w-full items-center gap-2.5 rounded-[10px] px-3 py-2 text-start text-sm text-ink no-underline hover:bg-surface-hover';
  return (
    <div className="relative ms-0.5 shrink-0 sm:ms-1.5" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center rounded-full"
        aria-haspopup="menu"
        aria-expanded={open}
        title={`${name} · ${t(`role.${user.role}`)}`}
      >
        <span
          aria-hidden="true"
          className="num inline-flex h-8 w-8 items-center justify-center rounded-full bg-[linear-gradient(135deg,#8e8e93,#3a3a3c)] text-[13px] font-semibold text-white"
        >
          {name.slice(0, 1).toUpperCase()}
        </span>
        {/* the accessible name: who is signed in and the role */}
        <span className="sr-only">
          {name === user.username ? name : `${name} (${user.username})`} · {t(`role.${user.role}`)}
        </span>
      </button>
      {open && (
        <div
          role="menu"
          className="absolute end-0 z-40 mt-2 w-60 rounded-lg bg-surface-raised p-1.5 shadow-pop"
        >
          <div className="px-3 pb-2 pt-1.5">
            <b className="block text-sm font-semibold">{name}</b>
            <small className="block text-xs text-ink-muted">{t(`role.${user.role}`)}</small>
          </div>
          <hr className="mx-1 my-1 border-0 border-t border-line" />
          <Link to="/account" role="menuitem" onClick={() => setOpen(false)} className={item}>
            <UsersIcon size={15} /> {t('nav.account')}
          </Link>
          <Link to="/tokens" role="menuitem" onClick={() => setOpen(false)} className={item}>
            <KeyIcon size={15} /> {t('nav.tokens')}
          </Link>
          <hr className="mx-1 my-1 border-0 border-t border-line" />
          <button
            type="button"
            role="menuitem"
            onClick={() => void logout()}
            className={cx(item, 'text-status-down')}
          >
            <LogoutIcon size={15} className="rtl:rotate-180" /> {t('nav.logout')}
          </button>
        </div>
      )}
    </div>
  );
}

/**
 * Top bar (64px, translucent with a blur, content scrolls under it): logo at the start, the
 * Dynamic Island in the middle, search / language / theme / ☰ / avatar at the end. On a phone
 * (below `sm`) the island takes only the room left between the logo and the tools, so the tools
 * and the avatar always stay inside the screen (the centred 1fr / auto / 1fr grid pushed the
 * avatar half off the end edge at 390 px).
 */
export function TopBar({
  menuId,
  menuOpen,
  onMenu,
}: {
  menuId: string;
  menuOpen: boolean;
  onMenu: () => void;
}) {
  const { t } = useTranslation();
  return (
    <header className="absolute inset-x-0 top-0 z-30 grid h-header grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2 bg-[color-mix(in_srgb,var(--chrome)_78%,transparent)] px-4 text-chrome-ink sm:grid-cols-[1fr_auto_1fr] sm:gap-0 sm:px-6 backdrop-blur-[20px] backdrop-saturate-[1.8] print:hidden">
      <Link
        to="/"
        dir="ltr"
        aria-label="NetworkMonitor"
        className="flex items-center gap-2.5 justify-self-start text-base font-semibold tracking-[-0.01em] text-chrome-ink no-underline [font-family:var(--font-display)]"
      >
        <Mark />
        <span className="hidden sm:inline">Network Monitor</span>
      </Link>
      <Island />
      <div className="flex items-center gap-0.5 justify-self-end sm:gap-1.5">
        <QuickSearch />
        <LanguageSwitcher compact />
        <ThemeToggle />
        <button
          type="button"
          className={cx(ROUND, menuOpen && 'bg-chrome-field text-chrome-ink')}
          aria-label={t('nav.openMenu')}
          aria-controls={menuId}
          aria-expanded={menuOpen}
          onClick={onMenu}
          data-testid="menu-button"
        >
          <MenuIcon size={20} strokeWidth={1.7} />
        </button>
        <UserMenu />
      </div>
    </header>
  );
}
