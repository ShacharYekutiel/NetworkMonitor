import { useEffect, useRef, useState } from 'react';
import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../hooks/auth';
import { useLocalState } from '../hooks/useLocalState';
import { ConnectionIndicator, LanguageSwitcher, ThemeToggle } from './Controls';
import {
  BellIcon,
  CogIcon,
  DashboardIcon,
  KeyIcon,
  LayersIcon,
  ListIcon,
  LogoutIcon,
  MapIcon,
  MenuIcon,
  WrenchIcon,
  PulseIcon,
  RadarIcon,
  SearchIcon,
  UsersIcon,
} from './icons';
import { parseSearchInput } from '../lib/ports';
import { TreeNav } from './Tree';
import { GlobalMaintenanceBanner } from './Maintenance';
import { cx } from './ui';
import {
  AuditIcon,
  BackupIcon,
  FlowIcon,
  LogsIcon,
  NewDeviceIcon,
  ReportIcon,
  SparkIcon,
} from './icons';
import { useFeatures } from '../hooks/features';
import { useSensorTypes } from '../hooks/queries';
import { ingestOn } from '../lib/features';

function NavItem({ to, icon, label }: { to: string; icon: React.ReactNode; label: string }) {
  return (
    <NavLink
      to={to}
      end={to === '/'}
      className={({ isActive }) =>
        cx(
          'flex items-center gap-2 rounded px-2 py-1.5 text-sm',
          isActive
            ? 'bg-sky-100 font-medium text-sky-900 dark:bg-sky-900/40 dark:text-sky-100'
            : 'text-slate-700 hover:bg-slate-200/70 dark:text-slate-300 dark:hover:bg-slate-800',
        )
      }
    >
      {icon}
      {label}
    </NavLink>
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
    document.addEventListener('mousedown', h);
    return () => document.removeEventListener('mousedown', h);
  }, [open]);
  if (!user) return null;
  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-2 rounded px-2 py-1 text-sm hover:bg-slate-100 dark:hover:bg-slate-800"
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <span className="flex h-6 w-6 items-center justify-center rounded-full bg-sky-600 text-xs font-semibold uppercase text-white">
          {user.username.slice(0, 1)}
        </span>
        <span className="hidden sm:inline">{user.display_name || user.username}</span>
        <span className="hidden text-xs text-slate-500 md:inline">({t(`role.${user.role}`)})</span>
      </button>
      {open && (
        <div
          role="menu"
          className="absolute end-0 z-40 mt-1 w-48 rounded-md bg-white py-1 shadow-lg ring-1 ring-slate-200 dark:bg-slate-900 dark:ring-slate-700"
        >
          <Link
            to="/account"
            role="menuitem"
            onClick={() => setOpen(false)}
            className="flex items-center gap-2 px-3 py-1.5 text-sm hover:bg-slate-100 dark:hover:bg-slate-800"
          >
            <KeyIcon size={14} /> {t('nav.account')}
          </Link>
          <Link
            to="/tokens"
            role="menuitem"
            onClick={() => setOpen(false)}
            className="flex items-center gap-2 px-3 py-1.5 text-sm hover:bg-slate-100 dark:hover:bg-slate-800"
          >
            <KeyIcon size={14} /> {t('nav.tokens')}
          </Link>
          <button
            type="button"
            role="menuitem"
            onClick={() => void logout()}
            className="flex w-full items-center gap-2 px-3 py-1.5 text-start text-sm hover:bg-slate-100 dark:hover:bg-slate-800"
          >
            <LogoutIcon size={14} className="rtl:rotate-180" /> {t('nav.logout')}
          </button>
        </div>
      )}
    </div>
  );
}

/** [2b-2] Header MAC/IP search (≥ sm); opens the Find page. */
function HeaderSearch() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [v, setV] = useState('');
  return (
    <form
      role="search"
      className="relative hidden w-56 sm:block"
      onSubmit={(e) => {
        e.preventDefault();
        const raw = v.trim();
        const p = parseSearchInput(raw);
        // An invalid query still goes to the Find page, which shows the validation error.
        const qs = new URLSearchParams(p ? (p as Record<string, string>) : { q: raw }).toString();
        navigate(raw ? `/find?${qs}` : '/find');
        setV('');
      }}
    >
      <SearchIcon
        size={14}
        className="pointer-events-none absolute start-2 top-1/2 -translate-y-1/2 text-slate-400"
      />
      <input
        value={v}
        onChange={(e) => setV(e.target.value)}
        placeholder={t('find.headerPlaceholder')}
        aria-label={t('find.label')}
        // MACs/IPs are LTR; the (translated) placeholder follows the page direction.
        dir={v ? 'ltr' : undefined}
        className="block w-full rounded-md border-0 bg-slate-100 py-1 pe-2 ps-7 text-sm text-slate-900 ring-1 ring-inset ring-slate-200 placeholder:text-slate-400 focus:bg-white focus:ring-2 focus:ring-sky-500 dark:bg-slate-800 dark:text-slate-100 dark:ring-slate-700 dark:focus:bg-slate-900"
      />
    </form>
  );
}

export function Layout() {
  const { t } = useTranslation();
  useSensorTypes(); // [1.3.1] named value maps for enum value texts everywhere
  const { isAdmin } = useAuth();
  const { features } = useFeatures();
  const [collapsed, setCollapsed] = useLocalState('netmon.sidebarCollapsed', false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();
  useEffect(() => setMobileOpen(false), [location.pathname]);

  const toggle = () => {
    if (window.matchMedia('(min-width: 1024px)').matches) setCollapsed((c) => !c);
    else setMobileOpen((o) => !o);
  };

  return (
    <div className="flex h-screen flex-col overflow-hidden print:block print:h-auto print:overflow-visible">
      <header className="flex h-12 shrink-0 items-center gap-1 border-b print:hidden sm:gap-2 border-slate-200 bg-white px-2 dark:border-slate-800 dark:bg-slate-900">
        <button
          type="button"
          onClick={toggle}
          className="rounded p-1.5 text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
          aria-label={t('nav.toggleSidebar')}
        >
          <MenuIcon />
        </button>
        <Link to="/" className="flex items-center gap-1.5 font-semibold">
          <PulseIcon className="text-green-600" />
          <span className="hidden sm:inline">NetworkMonitor</span>
        </Link>
        <div className="flex-1" />
        <HeaderSearch />
        <ConnectionIndicator />
        <LanguageSwitcher />
        <ThemeToggle />
        <UserMenu />
      </header>
      <div className="relative flex min-h-0 flex-1 print:block">
        {mobileOpen && (
          <div
            className="fixed inset-0 top-12 z-20 bg-slate-900/60 lg:hidden"
            onClick={() => setMobileOpen(false)}
          />
        )}
        <aside
          className={cx(
            'z-30 flex w-72 shrink-0 flex-col border-e print:hidden border-slate-200 bg-slate-100 shadow-xl dark:border-slate-800 dark:bg-slate-900 lg:shadow-none lg:dark:bg-slate-900/60',
            'fixed bottom-0 top-12 start-0 transition-transform lg:static lg:translate-x-0 lg:rtl:translate-x-0',
            mobileOpen ? 'translate-x-0' : '-translate-x-full rtl:translate-x-full',
            collapsed && 'lg:hidden',
          )}
          aria-label={t('nav.sidebar')}
        >
          <nav className="space-y-0.5 p-2">
            <NavItem to="/" icon={<DashboardIcon size={16} />} label={t('nav.dashboard')} />
            {/* [3b] admins, feature `assistant` ON */}
            {isAdmin && features.assistant && (
              <NavItem to="/assistant" icon={<SparkIcon size={16} />} label={t('nav.assistant')} />
            )}
            <NavItem to="/events" icon={<ListIcon size={16} />} label={t('nav.events')} />
            {ingestOn(features) && (
              <NavItem to="/logs" icon={<LogsIcon size={16} />} label={t('nav.logs')} />
            )}
            <NavItem to="/topology" icon={<MapIcon size={16} />} label={t('nav.map')} />
            {/* [3] entries of features that are ON */}
            {features.flows && (
              <NavItem to="/flows" icon={<FlowIcon size={16} />} label={t('nav.flows')} />
            )}
            {features.new_device_alerts && (
              <NavItem
                to="/new-devices"
                icon={<NewDeviceIcon size={16} />}
                label={t('nav.newDevices')}
              />
            )}
            {features.config_backup && (
              <NavItem
                to="/config-backups"
                icon={<BackupIcon size={16} />}
                label={t('nav.backups')}
              />
            )}
            {features.reports && (
              <NavItem to="/reports" icon={<ReportIcon size={16} />} label={t('nav.reports')} />
            )}
            <NavItem to="/find" icon={<SearchIcon size={16} />} label={t('nav.find')} />
            <NavItem
              to="/maintenance"
              icon={<WrenchIcon size={16} />}
              label={t('nav.maintenance')}
            />
            {isAdmin && (
              <NavItem to="/discovery" icon={<RadarIcon size={16} />} label={t('nav.discovery')} />
            )}
            <NavItem to="/templates" icon={<LayersIcon size={16} />} label={t('nav.templates')} />
            {isAdmin && (
              <>
                <NavItem
                  to="/notifications"
                  icon={<BellIcon size={16} />}
                  label={t('nav.notifications')}
                />
                <NavItem to="/settings" icon={<CogIcon size={16} />} label={t('nav.settings')} />
                <NavItem to="/users" icon={<UsersIcon size={16} />} label={t('nav.users')} />
                {features.audit && (
                  <NavItem to="/audit" icon={<AuditIcon size={16} />} label={t('nav.audit')} />
                )}
              </>
            )}
          </nav>
          <div className="px-3 pb-1 pt-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
            {t('nav.tree')}
          </div>
          <TreeNav />
        </aside>
        <main className="min-w-0 flex-1 overflow-y-auto print:overflow-visible">
          <div className="print:hidden">
            <GlobalMaintenanceBanner />
          </div>
          <div className="p-4 print:p-0 lg:p-6">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
