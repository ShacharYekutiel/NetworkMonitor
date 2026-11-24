import { useEffect, useRef, type ComponentType } from 'react';
import { NavLink, useLocation } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../../hooks/auth';
import { useFeatures } from '../../hooks/features';
import { activeMenuKey, menuSections, type MenuIconKey, type MenuTab } from '../../lib/menu';
import {
  AuditIcon,
  BackupIcon,
  BellIcon,
  ChevronIcon,
  ClockIcon,
  CogIcon,
  DashboardIcon,
  FlowIcon,
  KeyIcon,
  LayersIcon,
  LogsIcon,
  MapIcon,
  NewDeviceIcon,
  LockIcon,
  UnlockIcon,
  RadarIcon,
  ReportIcon,
  SearchIcon,
  SparkIcon,
  UsersIcon,
  WrenchIcon,
  XIcon,
} from '../icons';
import { TreeNav } from '../Tree';
import { cx, SegmentedControl } from '../ui';
import { useSensorCounts } from './Island';

type IconC = ComponentType<{ size?: number; strokeWidth?: number; className?: string }>;
const ICONS: Record<MenuIconKey, IconC> = {
  home: DashboardIcon,
  assistant: SparkIcon,
  events: ClockIcon,
  logs: LogsIcon,
  map: MapIcon,
  flows: FlowIcon,
  find: SearchIcon,
  newDevices: NewDeviceIcon,
  backups: BackupIcon,
  discovery: RadarIcon,
  templates: LayersIcon,
  maintenance: WrenchIcon,
  reports: ReportIcon,
  notifications: BellIcon,
  settings: CogIcon,
  users: UsersIcon,
  audit: AuditIcon,
  tokens: KeyIcon,
  account: UsersIcon,
};

/**
 * The ☰ menu: every screen in sections (grouped lists), plus the inventory tree. Opens as a sheet
 * from the end edge; the pin button docks it as a side panel at the start edge (remembered).
 */
export function MenuPanel({
  id,
  pinned,
  open,
  tab,
  onTab,
  onPin,
  onClose,
}: {
  id: string;
  pinned: boolean;
  open: boolean;
  tab: MenuTab;
  onTab: (t: MenuTab) => void;
  onPin: (p: boolean) => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const { features } = useFeatures();
  const { pathname } = useLocation();
  const counts = useSensorCounts();
  const sections = menuSections({ isAdmin, features });
  const active = activeMenuKey(sections, pathname);
  const ref = useRef<HTMLElement>(null);
  const visible = pinned || open;

  // A closed sheet is out of the tab order and the accessibility tree.
  useEffect(() => {
    if (ref.current) ref.current.inert = !visible;
  }, [visible]);

  // As a sheet: Escape closes it; focus moves in on open and the current page is in view.
  useEffect(() => {
    if (!open || pinned) return;
    const el = ref.current;
    const first = el?.querySelector<HTMLElement>('[aria-current="page"], a[href], button');
    first?.focus({ preventScroll: true });
    first?.scrollIntoView({ block: 'nearest' });
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, pinned, onClose]);

  return (
    <aside
      ref={ref}
      id={id}
      aria-label={t('nav.sidebar')}
      aria-hidden={!visible || undefined}
      data-pinned={pinned || undefined}
      data-open={visible || undefined}
      className={cx(
        'flex w-menu shrink-0 flex-col bg-bg print:hidden',
        pinned
          ? 'relative z-20 h-full border-e border-line pt-header'
          : cx(
              // The sheet opens on the side of the ☰ button (the end side: left in Hebrew); only
              // the pinned menu docks on the start side (right in Hebrew). Owner's decision.
              'fixed inset-y-0 end-0 z-[51] max-w-[92vw] shadow-[0_0_60px_rgba(0,0,0,0.25)] transition-transform duration-500 ease-premium',
              open ? 'translate-x-0' : 'translate-x-[104%] rtl:-translate-x-[104%]',
            ),
      )}
    >
      <div className={cx('flex shrink-0 items-center gap-1.5 px-4 pb-2', pinned ? 'pt-3' : 'pt-5')}>
        <h2
          className={cx(
            'm-0 me-auto ps-1.5 font-semibold leading-tight tracking-[-0.02em]',
            pinned ? 'text-xl' : 'text-[26px]',
          )}
        >
          {t('nav.allScreens')}
        </h2>
        <button
          type="button"
          onClick={() => onPin(!pinned)}
          aria-pressed={pinned}
          aria-label={pinned ? t('nav.unpin') : t('nav.pin')}
          title={pinned ? t('nav.unpin') : t('nav.pin')}
          data-testid="menu-pin"
          className={cx(
            'inline-flex h-9 w-9 items-center justify-center rounded-full transition-colors',
            pinned ? 'bg-accent text-accent-on' : 'text-ink-muted hover:bg-chip hover:text-ink',
          )}
        >
          {pinned ? (
            <LockIcon size={18} strokeWidth={1.8} />
          ) : (
            <UnlockIcon size={18} strokeWidth={1.8} />
          )}
        </button>
        {!pinned && (
          <button
            type="button"
            onClick={onClose}
            aria-label={t('nav.closeMenu')}
            className="inline-flex h-9 w-9 items-center justify-center rounded-full text-ink-muted hover:bg-chip hover:text-ink"
          >
            <XIcon size={18} strokeWidth={1.8} />
          </button>
        )}
      </div>
      <div className="shrink-0 px-4 pb-1">
        <SegmentedControl
          label={t('nav.menuView')}
          value={tab}
          onChange={onTab}
          options={[
            { value: 'screens', label: t('nav.screens') },
            { value: 'inventory', label: t('nav.inventory') },
          ]}
          className="w-full [&>button]:flex-1"
        />
      </div>
      {tab === 'screens' ? (
        <nav
          aria-label={t('nav.screens')}
          className="min-h-0 flex-1 overflow-y-auto px-4 pb-6 [scrollbar-width:thin]"
        >
          {sections.map((s) => (
            <section key={s.key}>
              <h3 className="mx-2.5 mb-2 mt-[18px] text-sm font-semibold text-ink-muted">
                {t(`nav.section.${s.key}`)}
              </h3>
              <ul
                className={cx(
                  'm-0 list-none p-0',
                  // pinned: rows without the card ground (§ 6b)
                  pinned ? 'grid gap-0.5' : 'overflow-hidden rounded-xl bg-surface shadow-card',
                )}
              >
                {s.items.map((it) => {
                  const Icon = ICONS[it.icon];
                  const on = it.key === active;
                  const badge = it.badge === 'down' && counts && counts.down > 0 ? counts.down : 0;
                  return (
                    <li
                      key={it.key}
                      className={cx(
                        'relative',
                        !pinned &&
                          '[&+li]:before:absolute [&+li]:before:end-0 [&+li]:before:start-[58px] [&+li]:before:top-0 [&+li]:before:h-px [&+li]:before:bg-line',
                      )}
                    >
                      <NavLink
                        to={it.to}
                        end={it.to === '/'}
                        aria-current={on ? 'page' : undefined}
                        // the accessible name is the section name (as on the 1.4 nav rail)
                        aria-label={t(it.label)}
                        onClick={() => !pinned && onClose()}
                        className={cx(
                          'flex items-center gap-3 text-[15px] no-underline transition-colors',
                          pinned ? 'rounded-[10px] px-3 py-[9px]' : 'px-4 py-[11px]',
                          on
                            ? 'bg-nav-active font-semibold text-nav-active-ink'
                            : 'text-ink hover:bg-surface-hover',
                        )}
                      >
                        <span
                          className={cx(
                            'inline-flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-[9px]',
                            on ? 'bg-accent text-accent-on' : 'bg-chip text-ink',
                          )}
                        >
                          <Icon size={17} strokeWidth={1.7} />
                        </span>
                        <span className="min-w-0 flex-1 truncate">{t(it.label)}</span>
                        {badge > 0 && (
                          <span className="num inline-flex h-[22px] min-w-[22px] items-center justify-center rounded-full bg-status-down px-[7px] text-[12.5px] font-semibold text-on-status">
                            {badge}
                          </span>
                        )}
                        {!pinned && (
                          <ChevronIcon
                            size={15}
                            className="shrink-0 text-ink-subtle rtl:rotate-180"
                            aria-hidden="true"
                          />
                        )}
                      </NavLink>
                    </li>
                  );
                })}
              </ul>
            </section>
          ))}
        </nav>
      ) : (
        <div className="mx-4 mb-4 mt-3 flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl bg-surface shadow-card">
          <TreeNav />
        </div>
      )}
    </aside>
  );
}
