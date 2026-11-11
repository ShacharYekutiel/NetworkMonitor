import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import type { Status } from '../api/types';
import { useAuth } from '../hooks/auth';
import { useLocalState } from '../hooks/useLocalState';
import { useTree } from '../hooks/queries';
import { ConnectionIndicator, HeaderClock, LanguageSwitcher, ThemeToggle } from './Controls';
import {
  AuditIcon,
  BackupIcon,
  BellIcon,
  CogIcon,
  DashboardIcon,
  FlowIcon,
  FolderIcon,
  KeyIcon,
  LayersIcon,
  ListIcon,
  LogoutIcon,
  LogsIcon,
  MapIcon,
  MenuIcon,
  NewDeviceIcon,
  RadarIcon,
  ReportIcon,
  SearchIcon,
  UsersIcon,
  WrenchIcon,
  XIcon,
} from './icons';
import { parseSearchInput } from '../lib/ports';
import { COUNT_ORDER, STATUS_DOT, STATUS_TEXT, emptyCounts } from '../lib/status';
import { TreeNav } from './Tree';
import { GlobalMaintenanceBanner } from './Maintenance';
import { cx } from './ui';
import { useFeatures } from '../hooks/features';
import { ingestOn } from '../lib/features';

/** The product mark (docs/design-system/assets/netmon-mark.svg, same as the favicon). */
function Mark({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="6" fill="#0f172a" />
      <path
        d="M4 18h6l3-8 5 14 3-6h7"
        fill="none"
        stroke="#22c55e"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Rail icons: 20px, stroke 1.75 (design system § Iconography). */
const RAIL_ICON = { size: 20, strokeWidth: 1.75 } as const;

/** One rail entry: icon + short label; the accessible name is the full section name. */
function RailItem({
  to,
  icon,
  label,
  short,
  badge,
}: {
  to: string;
  icon: ReactNode;
  label: string;
  short: string;
  badge?: ReactNode;
}) {
  return (
    <NavLink
      to={to}
      end={to === '/'}
      aria-label={label}
      title={label}
      className={({ isActive }) =>
        cx(
          'group relative flex w-16 shrink-0 flex-col items-center gap-[5px] rounded-md pb-[5px] pt-1.5 text-center text-[10.5px] font-medium leading-[1.1] no-underline',
          isActive
            ? 'bg-nav-active font-semibold text-nav-active-ink'
            : 'text-chrome-muted hover:bg-chrome-field hover:text-chrome-ink',
        )
      }
    >
      {({ isActive }) => (
        <>
          <span className={cx('flex', isActive && 'text-accent')}>{icon}</span>
          <span className="max-w-full truncate px-0.5">{short}</span>
          {badge && <span className="absolute end-2 top-1">{badge}</span>}
        </>
      )}
    </NavLink>
  );
}

function RailDivider() {
  return <hr className="my-1 w-9 shrink-0 border-0 border-t border-chrome-border" />;
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
  const item =
    'flex w-full items-center gap-[9px] rounded-sm px-[9px] py-[7px] text-start text-[13px] text-ink hover:bg-surface-hover';
  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-2.5 rounded-md px-1 py-1 text-[13px] text-chrome-ink hover:bg-chrome-field"
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <span className="inline-flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-full bg-chrome-field text-[13px] font-semibold text-chrome-ink shadow-[inset_0_0_0_1px_var(--chrome-border)]">
          {user.username.slice(0, 1)}
        </span>
        <span className="hidden text-start lg:block">
          <b className="block font-semibold leading-4">{user.display_name || user.username}</b>
          <small className="block text-xs leading-[14px] text-chrome-muted">
            {t(`role.${user.role}`)}
          </small>
        </span>
      </button>
      {open && (
        <div
          role="menu"
          className="absolute end-0 z-40 mt-1 w-52 rounded-lg bg-surface-raised p-1 shadow-pop"
        >
          <Link to="/account" role="menuitem" onClick={() => setOpen(false)} className={item}>
            <KeyIcon size={14} /> {t('nav.account')}
          </Link>
          <Link to="/tokens" role="menuitem" onClick={() => setOpen(false)} className={item}>
            <KeyIcon size={14} /> {t('nav.tokens')}
          </Link>
          <hr className="m-1 border-0 border-t border-line" />
          <button
            type="button"
            role="menuitem"
            onClick={() => void logout()}
            className={cx(item, 'text-status-down')}
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
      className="relative hidden w-[260px] items-center px-3 sm:flex"
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
        strokeWidth={1.75}
        className="pointer-events-none absolute start-[22px] top-1/2 -translate-y-1/2 text-chrome-muted"
      />
      <input
        value={v}
        onChange={(e) => setV(e.target.value)}
        placeholder={t('find.headerPlaceholder')}
        aria-label={t('find.label')}
        // MACs/IPs are LTR; the (translated) placeholder follows the page direction.
        dir={v ? 'ltr' : undefined}
        className="block h-8 w-full rounded-md border border-chrome-border bg-chrome-field pe-2.5 ps-8 font-mono text-[12.5px] text-chrome-ink placeholder:font-sans placeholder:text-chrome-muted focus:border-transparent focus:outline focus:outline-2 focus:-outline-offset-1 focus:outline-focus"
      />
    </form>
  );
}

/** Statuses in the header strip, worst first; unknown only when there is any. */
const STRIP: Status[] = COUNT_ORDER;
const HOT: Status[] = ['down', 'warning', 'unreachable', 'unknown'];

/** Sensor counts per status for the whole visible tree (root groups count recursively). */
function useSensorCounts() {
  const { data } = useTree();
  return useMemo(() => {
    if (!data) return null;
    const c = emptyCounts();
    for (const g of data.groups)
      for (const s of [...STRIP, 'total'] as const) c[s] += g.status_counts[s] ?? 0;
    return c;
  }, [data]);
}

/** Header health strip: dot + count + word per status, visible from every page. */
function HealthStrip() {
  const { t } = useTranslation();
  const counts = useSensorCounts();
  if (!counts) return null;
  return (
    <ul
      aria-label={t('nav.health')}
      className="m-0 hidden list-none items-stretch border-e border-chrome-border p-0 xl:flex"
    >
      {STRIP.filter((s) => s !== 'unknown' || counts.unknown > 0).map((s) => {
        const n = counts[s];
        const hot = HOT.includes(s) && n > 0;
        return (
          <li
            key={s}
            className="flex items-center gap-[7px] border-e border-chrome-border px-3 last:border-e-0"
          >
            <span className={cx('h-2 w-2 shrink-0 rounded-full', STATUS_DOT[s])} />
            <span
              className={cx(
                'font-display text-lg font-semibold leading-none tabular-nums',
                hot ? STATUS_TEXT[s] : s === 'up' ? 'text-chrome-ink' : 'text-chrome-muted',
              )}
            >
              <bdi>{n}</bdi>
            </span>
            <span className="text-xs font-medium leading-[1.1] text-chrome-muted">
              {t(`status.${s}`)}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function HeaderCell({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cx('flex shrink-0 items-center border-s border-chrome-border', className)}>
      {children}
    </div>
  );
}

export function Layout() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const { features } = useFeatures();
  const counts = useSensorCounts();
  const [collapsed, setCollapsed] = useLocalState('netmon.sidebarCollapsed', false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();
  useEffect(() => setMobileOpen(false), [location.pathname]);

  // ≥ 1024px the inventory panel is collapsible (remembered); below it is a drawer.
  const toggle = () => {
    if (window.matchMedia('(min-width: 1024px)').matches) setCollapsed((c) => !c);
    else setMobileOpen((o) => !o);
  };

  const downPill =
    counts && counts.down > 0 ? (
      <span className="inline-flex h-4 min-w-[18px] items-center justify-center rounded-sm bg-status-down px-1 font-mono text-[10px] font-bold text-on-status">
        {counts.down}
      </span>
    ) : undefined;

  // Four groups divided by hairlines (design system § Navigation); disabled features hide
  // their entry, admin-only sections are hidden from viewers.
  const groups: { key: string; items: (false | ReactNode)[] }[] = [
    {
      key: 'monitor',
      items: [
        <RailItem
          key="dashboard"
          to="/"
          icon={<DashboardIcon {...RAIL_ICON} />}
          label={t('nav.dashboard')}
          short={t('nav.short.dashboard')}
        />,
        <RailItem
          key="events"
          to="/events"
          icon={<ListIcon {...RAIL_ICON} />}
          label={t('nav.events')}
          short={t('nav.short.events')}
          badge={downPill}
        />,
        ingestOn(features) && (
          <RailItem
            key="logs"
            to="/logs"
            icon={<LogsIcon {...RAIL_ICON} />}
            label={t('nav.logs')}
            short={t('nav.short.logs')}
          />
        ),
        <RailItem
          key="map"
          to="/topology"
          icon={<MapIcon {...RAIL_ICON} />}
          label={t('nav.map')}
          short={t('nav.short.map')}
        />,
        features.flows && (
          <RailItem
            key="flows"
            to="/flows"
            icon={<FlowIcon {...RAIL_ICON} />}
            label={t('nav.flows')}
            short={t('nav.short.flows')}
          />
        ),
      ],
    },
    {
      key: 'network',
      items: [
        features.new_device_alerts && (
          <RailItem
            key="newDevices"
            to="/new-devices"
            icon={<NewDeviceIcon {...RAIL_ICON} />}
            label={t('nav.newDevices')}
            short={t('nav.short.newDevices')}
          />
        ),
        features.config_backup && (
          <RailItem
            key="backups"
            to="/config-backups"
            icon={<BackupIcon {...RAIL_ICON} />}
            label={t('nav.backups')}
            short={t('nav.short.backups')}
          />
        ),
        isAdmin && (
          <RailItem
            key="discovery"
            to="/discovery"
            icon={<RadarIcon {...RAIL_ICON} />}
            label={t('nav.discovery')}
            short={t('nav.short.discovery')}
          />
        ),
        <RailItem
          key="templates"
          to="/templates"
          icon={<LayersIcon {...RAIL_ICON} />}
          label={t('nav.templates')}
          short={t('nav.short.templates')}
        />,
        <RailItem
          key="find"
          to="/find"
          icon={<SearchIcon {...RAIL_ICON} />}
          label={t('nav.find')}
          short={t('nav.short.find')}
        />,
      ],
    },
    {
      key: 'operations',
      items: [
        <RailItem
          key="maintenance"
          to="/maintenance"
          icon={<WrenchIcon {...RAIL_ICON} />}
          label={t('nav.maintenance')}
          short={t('nav.short.maintenance')}
        />,
        features.reports && (
          <RailItem
            key="reports"
            to="/reports"
            icon={<ReportIcon {...RAIL_ICON} />}
            label={t('nav.reports')}
            short={t('nav.short.reports')}
          />
        ),
      ],
    },
    {
      key: 'admin',
      items: isAdmin
        ? [
            <RailItem
              key="notifications"
              to="/notifications"
              icon={<BellIcon {...RAIL_ICON} />}
              label={t('nav.notifications')}
              short={t('nav.short.notifications')}
            />,
            <RailItem
              key="settings"
              to="/settings"
              icon={<CogIcon {...RAIL_ICON} />}
              label={t('nav.settings')}
              short={t('nav.short.settings')}
            />,
            <RailItem
              key="users"
              to="/users"
              icon={<UsersIcon {...RAIL_ICON} />}
              label={t('nav.users')}
              short={t('nav.short.users')}
            />,
            features.audit && (
              <RailItem
                key="audit"
                to="/audit"
                icon={<AuditIcon {...RAIL_ICON} />}
                label={t('nav.audit')}
                short={t('nav.short.audit')}
              />
            ),
          ]
        : [],
    },
  ];
  const visibleGroups = groups.filter((g) => g.items.some(Boolean));

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-bg print:block print:h-auto print:overflow-visible">
      <header className="flex h-header shrink-0 items-stretch border-b border-chrome-border bg-chrome text-chrome-ink print:hidden">
        <div className="flex w-rail shrink-0 items-center justify-center border-e border-chrome-border">
          <button
            type="button"
            onClick={toggle}
            className="inline-flex h-[34px] w-[34px] items-center justify-center rounded-md text-chrome-muted hover:bg-chrome-field hover:text-chrome-ink md:hidden"
            aria-label={t('nav.toggleSidebar')}
          >
            <MenuIcon strokeWidth={1.75} />
          </button>
          <Link to="/" className="hidden md:flex" aria-label="NetworkMonitor">
            <Mark />
          </Link>
        </div>
        <Link
          to="/"
          dir="ltr"
          className="hidden shrink-0 items-center border-e border-chrome-border px-4 text-base font-semibold leading-none tracking-[-0.01em] text-chrome-ink no-underline sm:flex"
        >
          Network<span className="ms-[0.2em] font-medium text-chrome-muted">Monitor</span>
        </Link>
        <HealthStrip />
        <div className="flex-1" />
        <HeaderSearch />
        <HeaderCell className="gap-2.5 px-3.5">
          <ConnectionIndicator />
          <span className="hidden md:inline">
            <HeaderClock />
          </span>
        </HeaderCell>
        <HeaderCell className="gap-0.5 px-1.5">
          <LanguageSwitcher />
          <ThemeToggle />
        </HeaderCell>
        <HeaderCell className="px-2 sm:px-3">
          <UserMenu />
        </HeaderCell>
      </header>
      <div className="relative flex min-h-0 flex-1 print:block">
        {mobileOpen && (
          <div
            className="fixed inset-0 top-header z-20 bg-overlay lg:hidden"
            onClick={() => setMobileOpen(false)}
          />
        )}
        <aside
          className={cx(
            'z-30 flex print:hidden',
            // Below md the rail and the inventory slide in together as a drawer.
            'fixed bottom-0 start-0 top-header shadow-pop transition-transform md:static md:translate-x-0 md:shadow-none md:rtl:translate-x-0',
            mobileOpen ? 'translate-x-0' : '-translate-x-full rtl:translate-x-full',
          )}
          aria-label={t('nav.sidebar')}
        >
          <nav className="flex w-rail shrink-0 flex-col items-center gap-px overflow-y-auto border-e border-chrome-border bg-chrome py-2 [scrollbar-width:none]">
            {visibleGroups.map((g, i) => (
              <Fragment key={g.key}>
                {i > 0 && <RailDivider />}
                {g.items}
              </Fragment>
            ))}
            <div className="hidden min-h-2 flex-1 md:block" />
            {/* Pinned to the rail's bottom edge, also while the rail scrolls. */}
            <button
              type="button"
              onClick={toggle}
              aria-label={t('nav.toggleInventory')}
              aria-controls="nm-inventory"
              title={t('nav.toggleInventory')}
              className="sticky bottom-0 hidden w-16 shrink-0 flex-col items-center gap-[5px] rounded-md bg-chrome pb-[5px] pt-1.5 text-center text-[10.5px] font-medium leading-[1.1] text-chrome-muted shadow-[0_-8px_0_var(--chrome)] hover:bg-chrome-field hover:text-chrome-ink md:flex"
            >
              <FolderIcon {...RAIL_ICON} />
              <span>{t('nav.short.inventory')}</span>
            </button>
          </nav>
          <div
            id="nm-inventory"
            className={cx(
              'flex w-tree shrink-0 flex-col overflow-hidden border-e border-line bg-surface-sunken',
              // md..lg: a drawer next to the rail; ≥ lg: a static, collapsible panel.
              'md:fixed md:bottom-0 md:start-rail md:top-header md:z-30 md:shadow-pop lg:static lg:z-auto lg:shadow-none',
              !mobileOpen && 'md:hidden',
              collapsed ? 'lg:hidden' : 'lg:flex',
            )}
          >
            <div className="flex h-11 shrink-0 items-center justify-between border-b border-line pe-2 ps-3.5 text-[13px] font-semibold text-ink">
              <h2 className="m-0 text-[13px] font-semibold">{t('nav.inventory')}</h2>
              <button
                type="button"
                onClick={toggle}
                aria-label={t('nav.toggleInventory')}
                className="inline-flex h-[26px] w-[26px] items-center justify-center rounded-md text-ink-muted hover:bg-surface-hover hover:text-ink"
              >
                <XIcon size={14} strokeWidth={1.75} />
              </button>
            </div>
            <TreeNav />
          </div>
        </aside>
        <main className="min-w-0 flex-1 overflow-y-auto bg-bg print:overflow-visible">
          <div className="print:hidden">
            <GlobalMaintenanceBanner />
          </div>
          <div className="max-w-content p-4 print:p-0 lg:px-7 lg:pb-8 lg:pt-[22px]">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
