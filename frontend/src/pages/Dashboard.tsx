import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { Status, StatusCounts } from '../api/types';
import { EventsTable } from '../components/EventList';
import { SensorTable } from '../components/SensorTable';
import { StatusBar } from '../components/Status';
import { DeviceFormModal, GroupFormModal } from '../components/EntityForms';
import { PlusIcon } from '../components/icons';
import { useAuth } from '../hooks/auth';
import { Button, Card, ErrorBox, Loading, PageHeader, cx } from '../components/ui';
import { STATUS_DOT } from '../lib/status';

const TILE_ORDER: Status[] = ['down', 'warning', 'up', 'paused', 'unknown'];

function Tile({ status, value, to }: { status: Status | 'total'; value: number; to?: string }) {
  const { t } = useTranslation();
  const body = (
    <div className="flex items-center gap-3 rounded-lg bg-white p-3 shadow-sm ring-1 ring-slate-200 transition hover:ring-sky-400 dark:bg-slate-900 dark:ring-slate-800">
      <span
        className={cx(
          'h-10 w-1.5 rounded-full',
          status === 'total' ? 'bg-sky-600' : STATUS_DOT[status],
        )}
      />
      <div>
        <div className="text-2xl font-semibold tabular-nums">{value}</div>
        <div className="text-xs text-slate-500">
          {status === 'total' ? t('dashboard.total') : t(`status.${status}`)}
        </div>
      </div>
    </div>
  );
  return to ? <Link to={to}>{body}</Link> : body;
}

function CountsRow({ counts }: { counts: StatusCounts }) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      {TILE_ORDER.map((s) => (
        <Tile key={s} status={s} value={counts[s]} />
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
      <div className="grid grid-cols-1 gap-6 2xl:grid-cols-2">
        <Card title={t('dashboard.problems')} bodyClassName="p-0">
          {data.problem_sensors.length ? (
            <SensorTable sensors={data.problem_sensors} showDevice />
          ) : (
            <div className="p-6 text-center text-sm text-green-700 dark:text-green-400">
              {t('dashboard.allGood')}
            </div>
          )}
        </Card>
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
