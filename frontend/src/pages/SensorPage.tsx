import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { CheckResult, HistoryRange } from '../api/types';
import { Breadcrumbs } from '../components/Breadcrumbs';
import { CheckResultView } from '../components/CheckResultView';
import { EventsTable } from '../components/EventList';
import { HistoryChart } from '../components/HistoryChart';
import { EditIcon, PauseIcon, PlayIcon, RefreshIcon, TrashIcon } from '../components/icons';
import { EditSensorModal, useSensorActions } from '../components/SensorForms';
import { StatusBadge, StatusDot } from '../components/Status';
import { ThresholdEditor } from '../components/ThresholdEditor';
import { useToast } from '../components/toast';
import {
  Button,
  Card,
  ConfirmDialog,
  Empty,
  ErrorBox,
  Loading,
  Ltr,
  PageHeader,
  cx,
  Value,
} from '../components/ui';
import { useAuth } from '../hooks/auth';
import { AckAction, AckBadge, AckDetails } from '../components/Acknowledge';
import { StatusMessage } from '../components/Dependency';
import { MaintenanceBadge } from '../components/Maintenance';
import { mapEnumValue, valueMapToRows } from '../lib/sensorConfig';
import { useLocalState } from '../hooks/useLocalState';
import { errorMessage } from '../lib/errors';
import { formatDateTime, formatRelative, sensorValueText } from '../lib/format';
import { canRun, STATUS_TEXT, statusClass } from '../lib/status';

const RANGES: HistoryRange[] = ['1h', '24h', '7d', '30d'];

export function SensorPage() {
  const { id: idParam } = useParams();
  const id = Number(idParam);
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const toast = useToast();
  const [range, setRange] = useLocalState<HistoryRange>('netmon.historyRange', '24h');
  const [channel, setChannel] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [lastRun, setLastRun] = useState<CheckResult | null>(null);

  const sensor = useQuery({ queryKey: qk.sensor(id), queryFn: () => endpoints.sensor(id) });
  const history = useQuery({
    queryKey: qk.history(id, range),
    queryFn: () => endpoints.history(id, range),
    placeholderData: keepPreviousData,
    refetchInterval: range === '1h' ? 60_000 : 300_000,
  });
  const events = useQuery({
    queryKey: qk.events({ sensor_id: id, page_size: 20 }),
    queryFn: () => endpoints.events({ sensor_id: id, page_size: 20 }),
  });
  const { toggle } = useSensorActions();
  const deviceId = sensor.data?.device_id;
  const device = useQuery({
    queryKey: qk.device(deviceId ?? 0),
    queryFn: () => endpoints.device(deviceId as number),
    enabled: !!deviceId && !!sensor.data?.in_maintenance,
  });

  useEffect(() => {
    setChannel(null);
    setLastRun(null);
  }, [id]);

  const run = useMutation({
    mutationFn: () => endpoints.runSensor(id),
    onSuccess: (r) => {
      setLastRun(r);
      void qc.invalidateQueries({ queryKey: qk.sensor(id) });
      void qc.invalidateQueries({ queryKey: ['history', id] });
    },
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  const del = useMutation({
    mutationFn: () => endpoints.deleteSensor(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.tree });
      void qc.invalidateQueries({ queryKey: ['sensors'] });
      qc.removeQueries({ queryKey: qk.sensor(id) });
      toast.push('success', t('common.deleted'));
      navigate(sensor.data ? `/devices/${sensor.data.device_id}` : '/');
    },
  });

  if (sensor.isLoading) return <Loading />;
  if (sensor.error || !sensor.data) return <ErrorBox error={sensor.error} />;
  const s = sensor.data;
  const activeChannel = channel ?? s.primary_channel;
  const hc = history.data?.channels.find((c) => c.key === activeChannel);
  const unit = hc?.unit ?? s.channels.find((c) => c.key === activeChannel)?.unit ?? '';

  return (
    <div className="space-y-4">
      <PageHeader
        breadcrumbs={
          <Breadcrumbs
            groupId={s.group_id}
            extra={[{ to: `/devices/${s.device_id}`, label: s.device_name }]}
          />
        }
        title={
          <>
            <StatusDot status={s.status} className="h-3 w-3" /> {s.name}
          </>
        }
        subtitle={t(`sensorType.${s.type}`)}
        actions={
          isAdmin && (
            <>
              <AckAction sensor={s} size="md" />
              <Button
                variant="primary"
                busy={run.isPending}
                disabled={!canRun(s)}
                title={canRun(s) ? undefined : t('sensors.runPausedHint')}
                onClick={() => run.mutate()}
              >
                <RefreshIcon size={14} /> {t('sensors.runNow')}
              </Button>
              <Button busy={toggle.isPending} onClick={() => toggle.mutate(s)}>
                {s.enabled ? (
                  <PauseIcon size={14} />
                ) : (
                  <PlayIcon size={14} className="rtl:rotate-180" />
                )}
                {s.enabled ? t('sensors.pause') : t('sensors.resume')}
              </Button>
              <Button onClick={() => setEditing(true)}>
                <EditIcon size={14} /> {t('common.edit')}
              </Button>
              <Button variant="danger" onClick={() => setDeleting(true)}>
                <TrashIcon size={14} /> {t('common.delete')}
              </Button>
            </>
          )
        }
      />

      <Card>
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
          <span className="inline-flex flex-wrap items-center gap-1.5">
            <StatusBadge status={s.status} />
            {s.acknowledgement && <AckBadge ack={s.acknowledgement} />}
            {s.in_maintenance && (
              <MaintenanceBadge
                endsAt={device.data?.maintenance?.ends_at}
                name={device.data?.maintenance?.name}
              />
            )}
          </span>
          {s.status_message && (
            <StatusMessage
              message={s.status_message}
              status={s.status}
              deviceId={s.device_id}
              className="text-slate-600 dark:text-slate-300"
            />
          )}
          <span className="text-slate-500">
            {t('sensors.lastCheck')}:{' '}
            <span title={formatDateTime(s.last_check_at)}>{formatRelative(s.last_check_at)}</span>
          </span>
          <span className="text-slate-500">
            {t('sensors.since')}: {formatDateTime(s.last_status_change_at)}
          </span>
          <span className="text-slate-500">
            {t('sensors.interval')}: <Ltr>{s.interval_s}</Ltr>
          </span>
        </div>
        {s.acknowledgement && (
          <div className="mt-2">
            <AckDetails ack={s.acknowledgement} />
          </div>
        )}
        {lastRun && (
          <div className="mt-3">
            <CheckResultView result={lastRun} />
          </div>
        )}
      </Card>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
        {s.channels.map((c) => {
          // [2b-1] enum: the tile shows the sensor status (the channel itself stays "up").
          const cs =
            s.type === 'snmp_custom' && s.config.value_type === 'enum' && c.key === 'value'
              ? s.status
              : c.status;
          return (
            <button
              key={c.key}
              type="button"
              onClick={() => setChannel(c.key)}
              className={cx(
                'rounded-lg bg-white p-3 text-start shadow-sm ring-1 transition dark:bg-slate-900',
                c.key === activeChannel
                  ? 'ring-2 ring-sky-500'
                  : 'ring-slate-200 hover:ring-sky-300 dark:ring-slate-800',
              )}
            >
              <div className="flex items-center gap-1.5 text-xs text-slate-500">
                <StatusDot status={cs} />
                <span className="truncate">{t(`channel.${c.key}`, { defaultValue: c.key })}</span>
              </div>
              <div
                className={cx(
                  'mt-1 text-lg font-semibold tabular-nums',
                  statusClass(cs, STATUS_TEXT),
                )}
              >
                <Value text={sensorValueText(s, c.key, c.last_value, c.unit)} />
              </div>
            </button>
          );
        })}
      </div>

      <Card
        title={`${t('sensors.history')}: ${t(`channel.${activeChannel}`, { defaultValue: activeChannel })}`}
        actions={
          <div
            className="inline-flex rounded-md ring-1 ring-slate-300 dark:ring-slate-600"
            role="group"
          >
            {RANGES.map((r) => (
              <button
                key={r}
                type="button"
                onClick={() => setRange(r)}
                aria-pressed={r === range}
                className={cx(
                  'px-2.5 py-1 text-xs first:rounded-s-md last:rounded-e-md',
                  r === range
                    ? 'bg-sky-600 text-white'
                    : 'hover:bg-slate-100 dark:hover:bg-slate-800',
                )}
              >
                {t(`range.${r}`)}
              </button>
            ))}
          </div>
        }
      >
        <ErrorBox error={history.error} />
        {history.isLoading && <Loading />}
        {history.data && hc && hc.points.length > 0 && (
          <HistoryChart
            points={hc.points}
            bucketS={history.data.bucket_s}
            unit={unit}
            label={activeChannel}
            from={history.data.from}
            to={history.data.to}
            events={events.data?.items}
          />
        )}
        {history.data && (!hc || hc.points.length === 0) && <Empty>{t('sensors.noData')}</Empty>}
      </Card>

      <Card title={t('thresholds.title')}>
        <ThresholdEditor sensor={s} readOnly={!isAdmin} />
      </Card>

      <Card title={t('sensors.settings')}>
        <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2 lg:grid-cols-3">
          {Object.entries(s.config)
            .filter(
              ([k]) =>
                !(
                  (k === 'value_map' || k === 'value_map_default') &&
                  s.config.value_type === 'enum'
                ),
            )
            .map(([k, v]) => (
              <div key={k} className="flex gap-2">
                <dt className="text-slate-500">
                  {t(`sensorConfig.${s.type}.${k}`, { defaultValue: k })}:
                </dt>
                <dd className="min-w-0 break-all">
                  <Ltr>
                    {v === null || v === ''
                      ? '—'
                      : typeof v === 'object'
                        ? JSON.stringify(v)
                        : t(`sensorConfigOption.${s.type}.${k}.${String(v)}`, {
                            defaultValue: String(v),
                          })}
                  </Ltr>
                </dd>
              </div>
            ))}
          {!Object.keys(s.config).length && (
            <div className="text-slate-500">{t('sensors.noConfig')}</div>
          )}
        </dl>
        {s.type === 'snmp_custom' && s.config.value_type === 'enum' && <ValueMapTable sensor={s} />}
      </Card>

      <Card title={t('sensors.events')} bodyClassName="p-0">
        {events.data ? <EventsTable events={events.data.items} showSource={false} /> : <Loading />}
      </Card>

      {isAdmin && <EditSensorModal open={editing} onClose={() => setEditing(false)} sensor={s} />}
      <ConfirmDialog
        open={deleting}
        title={t('sensors.deleteTitle')}
        message={t('sensors.deleteConfirm', { name: s.name })}
        onConfirm={() => del.mutate()}
        onClose={() => setDeleting(false)}
        busy={del.isPending}
        error={del.error}
      />
    </div>
  );
}

/** [2b-1] The enum value map of an snmp_custom sensor; the current raw value is highlighted. */
function ValueMapTable({ sensor }: { sensor: import('../api/types').Sensor }) {
  const { t } = useTranslation();
  const raw = sensor.channels.find((c) => c.key === 'value')?.last_value ?? null;
  const current = mapEnumValue(sensor.config, raw);
  const rows = valueMapToRows(sensor.config.value_map);
  const rawKey = raw === null ? null : String(Math.round(raw));
  const unmapped = rawKey !== null && !rows.some((r) => r.value === rawKey);
  return (
    <div className="mt-4">
      <div className="mb-1 text-xs font-semibold text-slate-600 dark:text-slate-300">
        {t('sensorConfig.snmp_custom.value_map')}
      </div>
      <table className="text-sm">
        <thead>
          <tr className="text-xs text-slate-500">
            <th className="pe-4 text-start font-medium">{t('valueMap.value')}</th>
            <th className="pe-4 text-start font-medium">{t('valueMap.status')}</th>
            <th className="text-start font-medium">{t('valueMap.text')}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr
              key={r.value}
              className={cx(r.value === rawKey && 'bg-sky-50 font-semibold dark:bg-sky-950/50')}
            >
              <td className="pe-4 font-mono">
                <Ltr>{r.value}</Ltr>
              </td>
              <td className="pe-4 py-0.5">
                <StatusBadge status={r.status} />
              </td>
              <td dir="auto">{r.text}</td>
            </tr>
          ))}
          <tr className={cx(unmapped && 'bg-sky-50 font-semibold dark:bg-sky-950/50')}>
            <td className="pe-4 text-slate-500">{t('valueMap.other')}</td>
            <td className="pe-4 py-0.5">
              <StatusBadge status={String(sensor.config.value_map_default ?? 'warning')} />
            </td>
            <td className="text-slate-500">{t('valueMap.unexpected')}</td>
          </tr>
        </tbody>
      </table>
      {current && raw !== null && (
        <p className="mt-1 text-xs text-slate-500">
          {t('valueMap.current')}: <Ltr className="font-mono">{Math.round(raw)}</Ltr> →{' '}
          {current.text || t('valueMap.unexpected')} ({t(`status.${current.status}`)})
        </p>
      )}
    </div>
  );
}
