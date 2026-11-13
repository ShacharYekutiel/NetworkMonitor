import { DashboardIncidents } from '../components/assistant/Incidents';
import { ExplainButton } from '../components/assistant/ExplainButton';
import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import { EventsTable } from '../components/EventList';
import { SensorTable } from '../components/SensorTable';
import { StatusBadge, StatusBar, StatusCountPills, StatusTiles } from '../components/Status';
import { DeviceFormModal, GroupFormModal } from '../components/EntityForms';
import { CheckIcon, PlusIcon, WrenchIcon } from '../components/icons';
import { AckAction, AckDetails } from '../components/Acknowledge';
import { Ltr } from '../components/ui';
import { useAuth } from '../hooks/auth';
import { Button, Card, ErrorBox, Loading, PageHeader, buttonClass } from '../components/ui';
import { countStatuses } from '../lib/status';

const SECTION_TITLE = 'mb-2.5 text-[13px] font-semibold leading-4 text-ink-muted';

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
    <div className="space-y-5">
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
        <h2 className={SECTION_TITLE}>{t('dashboard.sensors')}</h2>
        <StatusTiles counts={data.sensor_counts} total />
        <div className="mt-2.5">
          <StatusBar counts={data.sensor_counts} />
        </div>
      </section>
      <section>
        <h2 className={SECTION_TITLE}>{t('dashboard.devices')}</h2>
        <StatusTiles counts={data.device_counts} total />
      </section>
      {data.maintenance && data.maintenance.active_window_ids.length > 0 && (
        <Link
          to="/maintenance"
          className="flex flex-wrap items-center gap-3 rounded-lg border border-transparent bg-accent-soft px-3.5 py-2.5 text-[13px] text-ink hover:border-accent"
        >
          <WrenchIcon size={16} className="text-accent-ink" />
          <span>
            {t('dashboard.maintenanceActive', { count: data.maintenance.active_window_ids.length })}
            {' · '}
            {t('dashboard.maintenanceDevices', { count: data.maintenance.device_count })}
          </span>
        </Link>
      )}
      {/* [3b] open incidents (feature ai_correlation) */}
      <DashboardIncidents />
      <div className="grid grid-cols-1 items-start gap-4 2xl:grid-cols-2">
        <Card
          title={
            <span className="inline-flex items-center gap-2">
              {t('dashboard.problems')}
              <StatusCountPills counts={countStatuses(data.problem_sensors.map((p) => p.status))} />
            </span>
          }
          bodyClassName="p-0"
        >
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
            <div className="flex flex-col items-center gap-2.5 px-4 py-9 text-center text-[13px] font-medium text-status-up">
              <CheckIcon size={30} />
              {t('dashboard.allGood')}
            </div>
          )}
        </Card>
        <div className="flex min-w-0 flex-col gap-4">
          {(data.acknowledged_count ?? 0) > 0 && (
            <Card
              title={
                <span className="inline-flex items-center gap-2">
                  {t('dashboard.acknowledged')}
                  <span className="inline-flex h-[18px] min-w-[20px] items-center justify-center rounded-sm bg-ack px-[5px] font-mono text-[11px] font-bold tabular-nums text-on-status">
                    {data.acknowledged_count}
                  </span>
                </span>
              }
              bodyClassName="p-0"
            >
              <ul className="divide-y divide-line">
                {data.acknowledged_sensors.map((s) => (
                  <li
                    key={s.id}
                    className="flex flex-wrap items-start gap-x-3 gap-y-1 px-4 py-2.5 text-[13px]"
                  >
                    <StatusBadge status={s.status} />
                    <div className="min-w-0 flex-1">
                      <div>
                        <Link className="text-ink hover:underline" to={`/devices/${s.device_id}`}>
                          {s.device_name}
                        </Link>
                        <span className="mx-1 text-ink-subtle">/</span>
                        <Link
                          className="font-semibold text-accent-ink hover:underline"
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
                <p className="border-t border-line px-4 py-2 text-xs text-ink-muted">
                  {t('dashboard.andMore', {
                    count: data.acknowledged_count - data.acknowledged_sensors.length,
                  })}
                </p>
              )}
            </Card>
          )}
          {(data.unreachable_devices?.length ?? 0) > 0 && (
            <Card title={t('dashboard.unreachableDevices')} bodyClassName="p-0">
              <ul className="divide-y divide-line">
                {data.unreachable_devices.map((d) => (
                  <li
                    key={d.id}
                    className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 text-[13px]"
                  >
                    <StatusBadge status="unreachable" />
                    <Link
                      className="font-semibold text-ink hover:underline"
                      to={`/devices/${d.id}`}
                    >
                      {d.name}
                    </Link>
                    <Ltr className="font-mono text-xs text-ink-subtle">{d.host}</Ltr>
                    {d.parent_device_id !== null && (
                      <span className="ms-auto text-xs text-ink-muted">
                        {t('dashboard.parent')}{' '}
                        <Link
                          className="font-medium text-status-unreachable hover:underline"
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
              <Link to="/events" className={buttonClass('ghost', 'sm')}>
                {t('dashboard.allEvents')}
              </Link>
            }
          >
            <EventsTable events={data.recent_events} showKind={false} />
          </Card>
        </div>
      </div>
    </div>
  );
}
