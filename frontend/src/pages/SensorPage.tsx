import { ExplainButton } from '../components/assistant/ExplainButton';
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
import {
  EditIcon,
  PauseIcon,
  PlayIcon,
  PulseIcon,
  RefreshIcon,
  TrashIcon,
} from '../components/icons';
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
  SEG_GROUP,
  cx,
  segClass,
  Value,
} from '../components/ui';
import { useAuth } from '../hooks/auth';
import { AckAction, AckBadge, AckDetails } from '../components/Acknowledge';
import { StatusMessage } from '../components/Dependency';
import { MaintenanceBadge } from '../components/Maintenance';
import { effectiveValueMapRows, mapEnumValue } from '../lib/sensorConfig';
import { namedValueMap, pickLang } from '../lib/valueMaps';
import { useSensorTypes } from '../hooks/queries';
import { useLocalState } from '../hooks/useLocalState';
import { EventConditionsCard, isEventSensor } from '../components/EventConditions';
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
    // [2b-3] passive event sensors have no measurements
    enabled: !!sensor.data && !isEventSensor(sensor.data),
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
  const passive = isEventSensor(s);

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
            <PulseIcon size={22} className="shrink-0 text-ink-subtle" />
            <span className="truncate">{s.name}</span>
            <StatusBadge status={s.status} labelled />
            {s.acknowledgement && <AckBadge ack={s.acknowledgement} />}
            {s.in_maintenance && (
              <MaintenanceBadge
                endsAt={device.data?.maintenance?.ends_at}
                name={device.data?.maintenance?.name}
              />
            )}
          </>
        }
        subtitle={t(`sensorType.${s.type}`)}
        meta={
          <>
            <span>
              {t('sensors.since')}:{' '}
              <b className="font-mono text-[12.5px] font-medium text-ink">
                <Ltr>{formatDateTime(s.last_status_change_at)}</Ltr>
              </b>
            </span>
            <span>
              {t('sensors.lastCheck')}:{' '}
              <b className="font-medium text-ink" title={formatDateTime(s.last_check_at)}>
                {formatRelative(s.last_check_at)}
              </b>
            </span>
            {!passive && (
              <span>
                {t('sensors.interval')}:{' '}
                <b className="font-mono text-[12.5px] font-medium text-ink">
                  <Ltr>{s.interval_s}</Ltr>
                </b>
              </span>
            )}
            {s.status_message && (
              <span>
                {t('sensors.message')}:{' '}
                <StatusMessage
                  message={s.status_message}
                  status={s.status}
                  deviceId={s.device_id}
                  className="font-medium text-ink"
                />
              </span>
            )}
            {passive && <span>{t('sensors.passiveNote')}</span>}
          </>
        }
        actions={
          isAdmin && (
            <>
              <AckAction sensor={s} size="md" />
              {(s.status === 'down' || s.status === 'warning' || s.status === 'unreachable') && (
                <ExplainButton sensorId={s.id} size="md" />
              )}
              {!passive && (
                <Button
                  variant="primary"
                  busy={run.isPending}
                  disabled={!canRun(s)}
                  title={canRun(s) ? undefined : t('sensors.runPausedHint')}
                  onClick={() => run.mutate()}
                >
                  <RefreshIcon size={14} /> {t('sensors.runNow')}
                </Button>
              )}
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

      {(s.acknowledgement || lastRun) && (
        <div className="space-y-3">
          {s.acknowledgement && <AckDetails ack={s.acknowledgement} />}
          {lastRun && <CheckResultView result={lastRun} />}
        </div>
      )}

      {/* Channel tiles: one strip divided by hairlines; the selected channel drives the chart. */}
      <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-px overflow-hidden rounded-lg border border-line bg-line">
        {s.channels.map((c) => {
          // [2b-1] enum: the tile shows the sensor status (the channel itself stays "up").
          const cs =
            s.type === 'snmp_custom' && s.config.value_type === 'enum' && c.key === 'value'
              ? s.status
              : c.status;
          const selected = c.key === activeChannel;
          return (
            <button
              key={c.key}
              type="button"
              onClick={() => setChannel(c.key)}
              aria-pressed={selected}
              className={cx(
                'min-w-0 px-4 pb-2.5 pt-3 text-start transition-colors',
                selected
                  ? 'bg-surface-raised shadow-[inset_0_-2px_0_var(--accent-ink)]'
                  : 'bg-surface hover:bg-surface-hover',
              )}
            >
              <div className="flex items-center gap-[7px] text-[13px] font-medium leading-4 text-ink-muted">
                <StatusDot status={cs} />
                <span className="truncate">{t(`channel.${c.key}`, { defaultValue: c.key })}</span>
              </div>
              <div
                className={cx(
                  'mt-2 truncate font-display text-[30px] font-semibold leading-9 tabular-nums',
                  cs === 'up' ? 'text-ink' : statusClass(cs, STATUS_TEXT),
                )}
              >
                <Value text={sensorValueText(s, c.key, c.last_value, c.unit)} />
              </div>
            </button>
          );
        })}
      </div>

      {passive && <EventConditionsCard sensor={s} />}
      {!passive && (
        <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
          <Card
            title={`${t('sensors.history')}: ${t(`channel.${activeChannel}`, { defaultValue: activeChannel })}`}
            actions={
              <div className={SEG_GROUP} role="group">
                {RANGES.map((r) => (
                  <button
                    key={r}
                    type="button"
                    onClick={() => setRange(r)}
                    aria-pressed={r === range}
                    className={segClass(r === range)}
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
            {history.data && (!hc || hc.points.length === 0) && (
              <Empty>{t('sensors.noData')}</Empty>
            )}
          </Card>

          <div className="flex min-w-0 flex-col gap-4">
            <Card title={t('thresholds.title')}>
              <ThresholdEditor sensor={s} readOnly={!isAdmin} />
            </Card>

            <Card title={t('sensors.settings')}>
              <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-[18px] gap-y-2 text-[13px]">
                {Object.entries(s.config)
                  .filter(
                    ([k]) =>
                      !(
                        (k === 'value_map' || k === 'value_map_default' || k === 'value_map_ref') &&
                        s.config.value_type === 'enum'
                      ),
                  )
                  .map(([k, v]) => (
                    <div key={k} className="contents">
                      <dt className="text-ink-muted">
                        {t(`sensorConfig.${s.type}.${k}`, { defaultValue: k })}:
                      </dt>
                      <dd className="min-w-0 break-all font-mono text-[12.5px]">
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
                  <div className="col-span-2 text-ink-muted">{t('sensors.noConfig')}</div>
                )}
              </dl>
              {s.type === 'snmp_custom' && s.config.value_type === 'enum' && (
                <ValueMapTable sensor={s} />
              )}
            </Card>
          </div>
        </div>
      )}

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
  const { t, i18n } = useTranslation();
  useSensorTypes(); // [1.3.1] loads the named value maps
  const lang = i18n.language;
  const raw = sensor.channels.find((c) => c.key === 'value')?.last_value ?? null;
  const current = mapEnumValue(sensor.config, raw, lang);
  const rows = effectiveValueMapRows(sensor.config, lang);
  const named = namedValueMap(sensor.config.value_map_ref);
  const rawKey = raw === null ? null : String(Math.round(raw));
  const unmapped = rawKey !== null && !rows.some((r) => r.value === rawKey);
  return (
    <div className="mt-4">
      <div className="mb-1 text-xs font-semibold text-ink-muted">
        {t('sensorConfig.snmp_custom.value_map')}
        {named && (
          <span className="ms-2 font-normal text-slate-500" data-testid="named-value-map">
            {pickLang(named.title, lang)}
          </span>
        )}
      </div>
      <table className="text-sm">
        <thead>
          <tr className="text-xs text-ink-muted">
            <th className="pe-4 text-start font-medium">{t('valueMap.value')}</th>
            <th className="pe-4 text-start font-medium">{t('valueMap.status')}</th>
            <th className="pe-4 text-start font-medium">{t('valueMap.text')}</th>
            {named && <th className="text-start font-medium">{t('valueMap.mib')}</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.value} className={cx(r.value === rawKey && 'bg-accent-soft font-semibold')}>
              <td className="pe-4 font-mono">
                <Ltr>{r.value}</Ltr>
              </td>
              <td className="pe-4 py-0.5">
                <StatusBadge status={r.status} />
              </td>
              <td className="pe-4" dir="auto">
                {r.text}
              </td>
              {named && (
                <td className="font-mono text-xs text-slate-500">
                  <Ltr>{r.mib ?? (r.own ? t('valueMap.own') : '')}</Ltr>
                </td>
              )}
            </tr>
          ))}
          <tr className={cx(unmapped && 'bg-accent-soft font-semibold')}>
            <td className="pe-4 text-ink-muted">{t('valueMap.other')}</td>
            <td className="pe-4 py-0.5">
              <StatusBadge status={String(sensor.config.value_map_default ?? 'warning')} />
            </td>
            <td className="pe-4 text-ink-muted">
              {named
                ? pickLang(named.unknown, lang).replace('{code}', unmapped ? rawKey! : 'N')
                : t('valueMap.unexpected')}
            </td>
            {named && <td />}
          </tr>
        </tbody>
      </table>
      {current && raw !== null && (
        <p className="mt-1 text-xs text-ink-muted">
          {t('valueMap.current')}: <Ltr className="font-mono">{Math.round(raw)}</Ltr> →{' '}
          {current.text || t('valueMap.unexpected')} ({t(`status.${current.status}`)})
        </p>
      )}
    </div>
  );
}
