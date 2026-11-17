import { DashboardIncidents } from '../components/assistant/Incidents';
import { ExplainButton } from '../components/assistant/ExplainButton';
import { useEffect, useState, type ComponentType, type ReactNode } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import { premium, qkPremium } from '../api/premium';
import type { Sensor, Status, StatusCounts } from '../api/types';
import { EventsTable } from '../components/EventList';
import { DeviceFormModal, GroupFormModal } from '../components/EntityForms';
import {
  AlertIcon,
  BackupIcon,
  ChevronIcon,
  ClockIcon,
  EditIcon,
  FlowIcon,
  FolderIcon,
  LayersIcon,
  LogsIcon,
  MapIcon,
  NewDeviceIcon,
  PlusIcon,
  ReportIcon,
  SearchIcon,
  WrenchIcon,
} from '../components/icons';
import { AckAction, AckDetails } from '../components/Acknowledge';
import { HomeMap } from '../components/home/HomeMap';
import { MapEditSheet } from '../components/home/MapEditSheet';
import { useShell } from '../components/shell/shellContext';
import { useAuth } from '../hooks/auth';
import { useFeatures } from '../hooks/features';
import { useTree } from '../hooks/queries';
import { useLocalizeMessage } from '../hooks/localize';
import { ingestOn } from '../lib/features';
import { elapsed } from '../lib/homeMap';
import { STATUS_DOT, STATUS_TEXT, statusClass } from '../lib/status';
import { flattenGroups } from '../lib/tree';
import { Card, cx, ErrorBox, GroupList, GroupTitle, ListRow, Loading, Ltr } from '../components/ui';

type IconC = ComponentType<{ size?: number; strokeWidth?: number; className?: string }>;

/** A ticking clock for the "time in state" column. */
function useNow(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(id);
  }, [ms]);
  return now;
}

/** The scope group of the home (site picker chip; `?site=<group id>`). */
function SitePicker({
  value,
  onChange,
  name,
}: {
  value: number | null;
  onChange: (id: number | null) => void;
  name?: string;
}) {
  const { t } = useTranslation();
  const tree = useTree();
  const groups = tree.data ? flattenGroups(tree.data.groups).filter((g) => g.depth < 2) : [];
  const current =
    name ?? groups.find((g) => g.group.id === value)?.group.name ?? t('home.allSites');
  return (
    <label className="relative inline-flex h-8 items-center gap-1.5 rounded-full bg-chip pe-3 ps-3.5 text-sm font-medium text-ink-muted hover:text-ink">
      <span dir="auto">{value === null ? t('home.allSites') : current}</span>
      <ChevronIcon size={15} className="rotate-90" />
      <select
        aria-label={t('home.site')}
        className="absolute inset-0 cursor-pointer opacity-0"
        value={value ?? ''}
        onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}
      >
        <option value="">{t('home.allSites')}</option>
        {groups.map(({ group, depth }) => (
          <option key={group.id} value={group.id}>
            {depth ? '   ' : ''}
            {group.name}
          </option>
        ))}
      </select>
    </label>
  );
}

/** Round 62px quick action with its label (prototype .qa). */
function QuickAction({
  icon: Icon,
  label,
  to,
  onClick,
  on,
}: {
  icon: IconC;
  label: string;
  to?: string;
  onClick?: () => void;
  on?: boolean;
}) {
  const inner = (
    <>
      <span
        className={cx(
          'inline-flex h-[62px] w-[62px] items-center justify-center rounded-full shadow-card transition-transform duration-500 ease-premium group-hover:-translate-y-[3px] group-active:scale-[0.94]',
          on ? 'bg-accent text-accent-on' : 'bg-surface text-ink',
        )}
      >
        <Icon size={22} strokeWidth={1.7} />
      </span>
      {label}
    </>
  );
  const cls =
    'group grid justify-items-center gap-2.5 text-[13.5px] font-medium text-ink-muted no-underline transition-colors hover:text-ink';
  return (
    <li>
      {to ? (
        <Link to={to} className={cls}>
          {inner}
        </Link>
      ) : (
        <button type="button" onClick={onClick} className={cls}>
          {inner}
        </button>
      )}
    </li>
  );
}

const COUNTERS: Status[] = ['down', 'warning', 'unreachable', 'up'];

/** Big counters in status colours divided by hairlines (prototype .counts). */
function Counters({ counts }: { counts: StatusCounts }) {
  const { t } = useTranslation();
  const extra = (['paused', 'unknown'] as Status[]).filter((s) => counts[s] > 0);
  return (
    <ul
      aria-label={t('home.counters')}
      className="mx-auto mb-2 mt-11 flex w-full max-w-[860px] list-none justify-center p-0 [&>li+li]:border-s [&>li+li]:border-line"
    >
      {[...COUNTERS, ...extra].map((s) => (
        <li key={s} className="grid flex-1 justify-items-center gap-0.5 px-2 py-1">
          <b
            className={cx(
              'num text-[clamp(30px,3.4vw,42px)] font-medium leading-[1.05] tracking-[-0.03em]',
              s !== 'up' && counts[s] > 0 ? statusClass(s, STATUS_TEXT) : 'text-ink',
            )}
          >
            {counts[s]}
          </b>
          <span className="whitespace-nowrap text-sm text-ink-muted">{t(`status.${s}`)}</span>
        </li>
      ))}
    </ul>
  );
}

function ProblemRow({ s, now }: { s: Sensor; now: number }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const localize = useLocalizeMessage();
  const e = elapsed(s.last_status_change_at, now);
  return (
    <li className="flex items-center gap-3.5 px-5 py-3.5" data-testid="problem-row">
      <span
        aria-label={t(`status.${s.status}`)}
        title={t(`status.${s.status}`)}
        className={cx('h-2.5 w-2.5 shrink-0 rounded-full', statusClass(s.status, STATUS_DOT))}
      />
      {/* the status word with the colour (status never by colour alone) */}
      <span className="sr-only">{t(`status.${s.status}`)}</span>
      <span className="grid min-w-0 flex-1">
        <span className="flex min-w-0 items-baseline gap-1.5 text-[15.5px]">
          <Link
            to={`/devices/${s.device_id}`}
            dir="ltr"
            className="truncate font-medium text-ink no-underline hover:underline [font-family:var(--font-display)]"
          >
            {s.device_name}
          </Link>
          <span className="text-ink-subtle">·</span>
          <Link to={`/sensors/${s.id}`} className="truncate text-ink no-underline hover:underline">
            {s.name}
          </Link>
        </span>
        <span dir="auto" className="truncate text-[13.5px] text-ink-muted">
          {localize(s.status_message) || t(`status.${s.status}`)}
        </span>
      </span>
      {e && (
        <span className="num shrink-0 text-[13px] font-medium text-ink-muted" dir="ltr">
          {'clock' in e ? e.clock : t('home.days', { count: e.days })}
        </span>
      )}
      {isAdmin && (
        <span className="inline-flex shrink-0 items-center gap-1">
          <ExplainButton sensorId={s.id} />
          <AckAction sensor={s} />
        </span>
      )}
    </li>
  );
}

function Section({ children, id }: { children: ReactNode; id?: string }) {
  return (
    <section id={id} className="min-w-0 scroll-mt-24">
      {children}
    </section>
  );
}

/**
 * Home (docs/design-system/premium/BUILD.md § 4): site chip and the headline, the network map hero,
 * quick actions, counters, then "needs attention" and "network" side by side, recent events last.
 */
export function DashboardPage() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const { features } = useFeatures();
  const navigate = useNavigate();
  const shell = useShell();
  const now = useNow();
  const [params, setParams] = useSearchParams();
  const site =
    params.get('site') && /^\d+$/.test(params.get('site')!) ? Number(params.get('site')) : null;
  const [addGroup, setAddGroup] = useState(false);
  const [addDevice, setAddDevice] = useState(false);
  const [editMap, setEditMap] = useState(false);
  const { data, isLoading, error } = useQuery({
    queryKey: qk.dashboard,
    queryFn: endpoints.dashboard,
  });
  const map = useQuery({
    queryKey: qkPremium.overviewMap(site),
    queryFn: () => premium.overviewMap(site),
    retry: false,
  });
  if (isLoading) return <Loading />;
  if (error || !data) return <ErrorBox error={error} />;

  const n = data.problem_sensors.length;
  const scrollToProblems = () =>
    document.getElementById('nm-problems')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  const showMap = !!map.data && map.data.nodes.length > 0;
  const nav: { key: string; to: string; icon: IconC; on: boolean }[] = [
    { key: 'map', to: '/topology', icon: MapIcon, on: true },
    { key: 'events', to: '/events', icon: ClockIcon, on: true },
    { key: 'logs', to: '/logs', icon: LogsIcon, on: ingestOn(features) },
    { key: 'flows', to: '/flows', icon: FlowIcon, on: !!features.flows },
    {
      key: 'newDevices',
      to: '/new-devices',
      icon: NewDeviceIcon,
      on: !!features.new_device_alerts,
    },
    { key: 'backups', to: '/config-backups', icon: BackupIcon, on: !!features.config_backup },
    { key: 'maintenance', to: '/maintenance', icon: WrenchIcon, on: true },
    { key: 'reports', to: '/reports', icon: ReportIcon, on: !!features.reports },
    { key: 'templates', to: '/templates', icon: LayersIcon, on: true },
  ];

  return (
    <div className="grid grid-cols-1">
      <div className="grid justify-items-center gap-1.5 pt-[18px] text-center">
        <SitePicker
          value={site}
          name={map.data?.scope.name}
          onChange={(id) => setParams(id === null ? {} : { site: String(id) }, { replace: true })}
        />
        <h1 className="m-0 mt-2.5 text-[clamp(44px,6vw,76px)] font-semibold leading-none tracking-[-0.035em]">
          <span className="sr-only">{t('dashboard.title')}: </span>
          {n > 0 ? (
            <>
              <span className="num font-semibold text-status-down">{n}</span>{' '}
              {t('home.needAttention', { count: n })}
            </>
          ) : (
            t('home.allGood')
          )}
        </h1>
        <p className="m-0 mt-1.5 text-[17px] text-ink-muted">
          {t('home.subline', {
            sensors: t('dashboard.sensorCount', { count: data.sensor_counts.total }),
            devices: t('dashboard.deviceCount', { count: data.device_counts.total }),
          })}
          {n > 0 && ` · ${t('home.restOk')}`}
        </p>
      </div>

      {/* The map hero: only when the server has the map (older servers: counters + lists). */}
      {showMap && (
        <div className="relative -mx-6 mt-2 pb-2 pt-10">
          <div
            aria-hidden="true"
            className="stage-bg pointer-events-none absolute inset-0 [mask-image:radial-gradient(ellipse_62%_58%_at_50%_46%,#000_40%,transparent_100%)]"
          />
          {isAdmin && (
            <button
              type="button"
              onClick={() => setEditMap(true)}
              className="absolute end-6 top-[18px] z-[3] inline-flex h-[34px] items-center gap-2 rounded-full bg-surface px-3.5 text-[13.5px] font-medium text-ink shadow-[0_0_0_1px_var(--border),var(--shadow-card)]"
            >
              <EditIcon size={15} /> {t('home.map.edit')}
            </button>
          )}
          <div className="relative px-6">
            <HomeMap map={map.data!} onSetup={isAdmin ? () => setEditMap(true) : undefined} />
          </div>
        </div>
      )}

      <ul
        aria-label={t('home.quick')}
        className={cx(
          'relative z-[2] m-0 flex list-none flex-wrap justify-center gap-x-[clamp(18px,4vw,44px)] gap-y-5 p-0',
          showMap ? 'mt-2' : 'mt-10',
        )}
      >
        <QuickAction
          icon={AlertIcon}
          label={t('home.qa.problems')}
          onClick={scrollToProblems}
          on={n > 0}
        />
        <QuickAction icon={ClockIcon} label={t('home.qa.events')} to="/events" />
        <QuickAction icon={WrenchIcon} label={t('home.qa.maintenance')} to="/maintenance" />
        <QuickAction icon={SearchIcon} label={t('home.qa.find')} to="/find" />
        <QuickAction
          icon={FolderIcon}
          label={t('home.qa.devices')}
          onClick={() => shell.openMenu('inventory')}
        />
        {isAdmin && (
          <QuickAction
            icon={PlusIcon}
            label={t('devices.add')}
            onClick={() => setAddDevice(true)}
          />
        )}
        {isAdmin && (
          <QuickAction icon={PlusIcon} label={t('groups.add')} onClick={() => setAddGroup(true)} />
        )}
      </ul>

      <Counters counts={data.sensor_counts} />

      {data.maintenance && data.maintenance.active_window_ids.length > 0 && (
        <Link
          to="/maintenance"
          className="mx-auto mt-6 flex flex-wrap items-center gap-3 rounded-full bg-accent-soft px-5 py-2.5 text-sm text-ink no-underline hover:underline"
        >
          <WrenchIcon size={16} />
          <span>
            {t('dashboard.maintenanceActive', { count: data.maintenance.active_window_ids.length })}
            {' · '}
            {t('dashboard.maintenanceDevices', { count: data.maintenance.device_count })}
          </span>
        </Link>
      )}

      {/* [3b] open incidents (feature ai_correlation) */}
      <div className="mt-6 empty:hidden">
        <DashboardIncidents />
      </div>

      <div className="mt-[30px] grid grid-cols-1 items-start gap-7 lg:grid-cols-2">
        <div className="grid min-w-0 gap-0">
          <Section id="nm-problems">
            <GroupTitle
              action={
                <Link to="/events" className="text-ink-muted no-underline hover:text-ink">
                  {t('home.all')}
                </Link>
              }
            >
              {t('dashboard.problems')}
            </GroupTitle>
            {n > 0 ? (
              <GroupList>
                {data.problem_sensors.map((s) => (
                  <ProblemRow key={s.id} s={s} now={now} />
                ))}
              </GroupList>
            ) : (
              <GroupList>
                <ListRow
                  dot="bg-status-up"
                  title={t('home.allGood')}
                  subtitle={t('dashboard.allGood')}
                />
              </GroupList>
            )}
          </Section>
          {(data.acknowledged_count ?? 0) > 0 && (
            <Section>
              <GroupTitle>
                {t('dashboard.acknowledged')}{' '}
                <span className="num ms-1 inline-flex h-[22px] min-w-[22px] items-center justify-center rounded-full bg-ack px-[7px] align-middle text-xs font-semibold text-on-status">
                  {data.acknowledged_count}
                </span>
              </GroupTitle>
              <GroupList>
                {data.acknowledged_sensors.map((s) => (
                  <li
                    key={s.id}
                    className="flex flex-wrap items-start gap-x-3.5 gap-y-1 px-5 py-3.5"
                  >
                    <span
                      aria-label={t(`status.${s.status}`)}
                      className={cx(
                        'mt-2 h-2.5 w-2.5 shrink-0 rounded-full',
                        statusClass(s.status, STATUS_DOT),
                      )}
                    />
                    <div className="min-w-0 flex-1 text-[15px]">
                      <div>
                        <Link
                          className="text-ink no-underline hover:underline"
                          to={`/devices/${s.device_id}`}
                        >
                          {s.device_name}
                        </Link>
                        <span className="mx-1.5 text-ink-subtle">·</span>
                        <Link
                          className="font-medium text-ink no-underline hover:underline"
                          to={`/sensors/${s.id}`}
                        >
                          {s.name}
                        </Link>
                      </div>
                      {s.acknowledgement && <AckDetails ack={s.acknowledgement} />}
                    </div>
                    {isAdmin && <AckAction sensor={s} />}
                  </li>
                ))}
              </GroupList>
              {data.acknowledged_count > data.acknowledged_sensors.length && (
                <p className="mx-1.5 mt-2 text-[13px] text-ink-muted">
                  {t('dashboard.andMore', {
                    count: data.acknowledged_count - data.acknowledged_sensors.length,
                  })}
                </p>
              )}
            </Section>
          )}
          {(data.unreachable_devices?.length ?? 0) > 0 && (
            <Section>
              <GroupTitle>{t('dashboard.unreachableDevices')}</GroupTitle>
              <GroupList>
                {data.unreachable_devices.map((d) => (
                  <li
                    key={d.id}
                    className="flex flex-wrap items-center gap-x-3.5 gap-y-1 px-5 py-3.5 text-[15px]"
                  >
                    <span
                      aria-label={t('status.unreachable')}
                      className="h-2.5 w-2.5 shrink-0 rounded-full bg-status-unreachable"
                    />
                    <Link
                      className="font-medium text-ink no-underline hover:underline"
                      to={`/devices/${d.id}`}
                    >
                      {d.name}
                    </Link>
                    <Ltr className="num text-[13px] text-ink-muted">{d.host}</Ltr>
                    {d.parent_device_id !== null && (
                      <span className="ms-auto text-[13px] text-ink-muted">
                        {t('dashboard.parent')}{' '}
                        <Link
                          className="font-medium text-status-unreachable no-underline hover:underline"
                          to={`/devices/${d.parent_device_id}`}
                        >
                          {d.parent_device_name}
                        </Link>
                      </span>
                    )}
                  </li>
                ))}
              </GroupList>
            </Section>
          )}
        </div>
        <Section>
          <GroupTitle>{t('home.network')}</GroupTitle>
          <GroupList>
            {nav
              .filter((x) => x.on)
              .map((x) => (
                <ListRow
                  key={x.key}
                  icon={<x.icon size={18} strokeWidth={1.7} />}
                  title={t(`nav.${x.key}`)}
                  subtitle={t(`home.sub.${x.key}`)}
                  to={x.to}
                />
              ))}
            <ListRow
              icon={<LayersIcon size={18} strokeWidth={1.7} />}
              title={t('nav.allScreens')}
              subtitle={t('home.sub.all')}
              onClick={() => shell.openMenu('screens')}
            />
          </GroupList>
        </Section>
      </div>

      <div className="mt-[30px]">
        <Card
          title={t('dashboard.recentEvents')}
          bodyClassName="p-0"
          actions={
            <Link to="/events" className="text-sm text-ink-muted no-underline hover:text-ink">
              {t('dashboard.allEvents')}
            </Link>
          }
        >
          <EventsTable events={data.recent_events} showKind={false} />
        </Card>
      </div>

      <GroupFormModal open={addGroup} onClose={() => setAddGroup(false)} parentId={null} />
      <DeviceFormModal
        open={addDevice}
        onClose={() => setAddDevice(false)}
        onCreated={(d) => navigate(`/devices/${d.id}`)}
      />
      {isAdmin && <MapEditSheet open={editMap} onClose={() => setEditMap(false)} />}
    </div>
  );
}
