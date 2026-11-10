import { DashboardIncidents } from '../components/assistant/Incidents';
import { ExplainButton } from '../components/assistant/ExplainButton';
import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { Status, StatusCounts } from '../api/types';
import { EventsTable } from '../components/EventList';
import { SensorTable } from '../components/SensorTable';
import { StatusBadge, StatusBar } from '../components/Status';
import { DeviceFormModal, GroupFormModal } from '../components/EntityForms';
import { PlusIcon, WrenchIcon } from '../components/icons';
import { AckAction, AckDetails } from '../components/Acknowledge';
import { Ltr } from '../components/ui';
import { useAuth } from '../hooks/auth';
import { Button, Card, ErrorBox, Loading, PageHeader, cx } from '../components/ui';
import { STATUS_DOT } from '../lib/status';

const TILE_ORDER: Status[] = ['down', 'warning', 'unreachable', 'up', 'paused', 'unknown'];

function Tile({ status, value, to }: { status: Status | 'total'; value: number; to?: string }) {
  const { t } = useTranslation();
  const body = (
    <div className="flex items-center gap-2 rounded-lg bg-white p-2.5 shadow-sm ring-1 ring-slate-200 transition hover:ring-sky-400 dark:bg-slate-900 dark:ring-slate-800 sm:gap-3 sm:p-3">
      <span
        className={cx(
          'h-10 w-1.5 shrink-0 rounded-full',
          status === 'total' ? 'bg-sky-600' : STATUS_DOT[status],
        )}
      />
      <div className="min-w-0">
        <div className="text-2xl font-semibold tabular-nums">{value}</div>
        <div className="truncate text-xs text-slate-500">
          {status === 'total' ? t('dashboard.total') : t(`status.${status}`)}
        </div>
      </div>
    </div>
  );
  return to ? <Link to={to}>{body}</Link> : body;
}

function CountsRow({ counts }: { counts: StatusCounts }) {
  return (
    <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 sm:gap-3 xl:grid-cols-7">
      {TILE_ORDER.map((s) => (
        <Tile key={s} status={s} value={counts[s] ?? 0} />
      ))}
      <Tile status="total" value={counts.total} />
    </div>
  );
}

export function DashboardPage() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const navigate = useNavigate();
  const [addGroup, setAddGroup] = useState(false);
  const [addDevice, setAddDevice] = useState(false);
  const { data, isLoading, error } = useQuery({
    queryKey: qk.dashboard,
    queryFn: endpoints.dashboard,
  });
  if (isLoading) return <Loading />;
  if (error || !data) return <ErrorBox error={error} />;
  return (
    <div className="space-y-6">
      <PageHeader
        title={t('dashboard.title')}
        subtitle={t('dashboard.summary', {
          groups: t('dashboard.groupCount', { count: data.group_count }),
          devices: t('dashboard.deviceCount', { count: data.device_counts.total }),
          sensors: t('dashboard.sensorCount', { count: data.sensor_counts.total }),
        })}
        actions={
          isAdmin && (
            <>
              <Button variant="primary" onClick={() => setAddDevice(true)}>
                <PlusIcon size={14} /> {t('devices.add')}
              </Button>
              <Button onClick={() => setAddGroup(true)}>
                <PlusIcon size={14} /> {t('groups.add')}
              </Button>
            </>
          )
        }
      />
      <GroupFormModal open={addGroup} onClose={() => setAddGroup(false)} parentId={null} />
      <DeviceFormModal
        open={addDevice}
        onClose={() => setAddDevice(false)}
        onCreated={(d) => navigate(`/devices/${d.id}`)}
      />
      <section>
        <h2 className="mb-2 text-sm font-semibold text-slate-600 dark:text-slate-400">
          {t('dashboard.sensors')}
        </h2>
        <CountsRow counts={data.sensor_counts} />
        <div className="mt-3">
          <StatusBar counts={data.sensor_counts} />
        </div>
      </section>
      <section>
        <h2 className="mb-2 text-sm font-semibold text-slate-600 dark:text-slate-400">
          {t('dashboard.devices')}
        </h2>
        <CountsRow counts={data.device_counts} />
      </section>
      {data.maintenance && data.maintenance.active_window_ids.length > 0 && (
        <Link
          to="/maintenance"
          className="flex flex-wrap items-center gap-2 rounded-lg bg-sky-50 px-4 py-2.5 text-sm text-sky-900 ring-1 ring-inset ring-sky-200 hover:ring-sky-400 dark:bg-sky-950 dark:text-sky-100 dark:ring-sky-800"
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
      <DashboardIncidents />
      <div className="grid grid-cols-1 gap-6 2xl:grid-cols-2">
        <Card title={t('dashboard.problems')} bodyClassName="p-0">
          {data.problem_sensors.length ? (
            <SensorTable
              sensors={data.problem_sensors}
              showDevice
              actions={
                isAdmin
                  ? (s) => (
                      <span className="inline-flex items-center gap-1">
                        <ExplainButton sensorId={s.id} />
                        <AckAction sensor={s} />
                      </span>
                    )
                  : undefined
              }
            />
          ) : (
            <div className="p-6 text-center text-sm text-green-700 dark:text-green-400">
              {t('dashboard.allGood')}
            </div>
          )}
        </Card>
        {(data.acknowledged_count ?? 0) > 0 && (
          <Card
            title={
              <span className="inline-flex items-center gap-2">
                {t('dashboard.acknowledged')}
                <span className="rounded bg-emerald-600 px-1.5 text-xs font-medium tabular-nums text-white">
                  {data.acknowledged_count}
                </span>
              </span>
            }
            bodyClassName="p-0"
          >
            <ul className="divide-y divide-slate-100 dark:divide-slate-800">
              {data.acknowledged_sensors.map((s) => (
                <li key={s.id} className="flex flex-wrap items-start gap-x-3 gap-y-1 px-4 py-2.5">
                  <StatusBadge status={s.status} />
                  <div className="min-w-0 flex-1">
                    <div className="text-sm">
                      <Link className="hover:underline" to={`/devices/${s.device_id}`}>
                        {s.device_name}
                      </Link>
                      <span className="mx-1 text-slate-400">/</span>
                      <Link
                        className="font-medium text-sky-700 hover:underline dark:text-sky-400"
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
            </ul>
            {data.acknowledged_count > data.acknowledged_sensors.length && (
              <p className="border-t border-slate-100 px-4 py-2 text-xs text-slate-500 dark:border-slate-800">
                {t('dashboard.andMore', {
                  count: data.acknowledged_count - data.acknowledged_sensors.length,
                })}
              </p>
            )}
          </Card>
        )}
        {(data.unreachable_devices?.length ?? 0) > 0 && (
          <Card title={t('dashboard.unreachableDevices')} bodyClassName="p-0">
            <ul className="divide-y divide-slate-100 dark:divide-slate-800">
              {data.unreachable_devices.map((d) => (
                <li
                  key={d.id}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 text-sm"
                >
                  <StatusBadge status="unreachable" />
                  <Link className="font-medium hover:underline" to={`/devices/${d.id}`}>
                    {d.name}
                  </Link>
                  <Ltr className="text-xs text-slate-500">{d.host}</Ltr>
                  {d.parent_device_id !== null && (
                    <span className="ms-auto text-xs text-slate-600 dark:text-slate-300">
                      {t('dashboard.parent')}{' '}
                      <Link
                        className="font-medium text-violet-700 hover:underline dark:text-violet-300"
                        to={`/devices/${d.parent_device_id}`}
                      >
                        {d.parent_device_name}
                      </Link>
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </Card>
        )}
        <Card
          title={t('dashboard.recentEvents')}
          bodyClassName="p-0"
          actions={
            <Link to="/events" className="text-xs text-sky-700 hover:underline dark:text-sky-400">
              {t('dashboard.allEvents')}
            </Link>
          }
        >
          <EventsTable events={data.recent_events} showKind={false} />
        </Card>
      </div>
    </div>
  );
}
