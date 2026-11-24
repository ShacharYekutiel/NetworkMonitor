import { ExplainButton } from '../components/assistant/ExplainButton';
import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import {
  SENSOR_IMPACTS,
  type CheckResult,
  type HistoryRange,
  type Sensor,
  type SensorImpact,
} from '../api/types';
import { Breadcrumbs } from '../components/Breadcrumbs';
import { CheckResultView } from '../components/CheckResultView';
import { EventRows } from '../components/EventList';
import { HistoryChart } from '../components/HistoryChart';
import { EditIcon, PauseIcon, PlayIcon, RefreshIcon, TrashIcon } from '../components/icons';
import { EditSensorModal, useSensorActions } from '../components/SensorForms';
import { StatusBadge, StatusDot } from '../components/Status';
import { SeverityTag } from '../components/kit';
import { useFeature } from '../hooks/features';
import { ThresholdEditor } from '../components/ThresholdEditor';
import { useToast } from '../components/toast';
import {
  Button,
  Card,
  ConfirmDialog,
  Empty,
  ErrorBox,
  GroupTitle,
  Loading,
  Ltr,
  PageHeader,
  SEG_GROUP,
  SegmentedControl,
  TR_SELECTED,
  cx,
  segClass,
  td,
  th,
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
  const severityOn = useFeature('alert_severity');
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

  const enumSensor = s.type === 'snmp_custom' && s.config.value_type === 'enum';
  const activeMeta = s.channels.find((c) => c.key === activeChannel);
  // [2b-1] enum: the value shows the sensor status (the channel itself stays "up").
  const chStatus = (key: string, st: string) => (enumSensor && key === 'value' ? s.status : st);
  const channelName = (key: string) => t(`channel.${key}`, { defaultValue: key });

  return (
    <div>
      <PageHeader
        breadcrumbs={
          <Breadcrumbs
            groupId={s.group_id}
            extra={[{ to: `/devices/${s.device_id}`, label: s.device_name }]}
          />
        }
        title={
          <>
            <span className="truncate">{s.name}</span>
            <StatusBadge status={s.status} labelled />
            {/* [severity] the current alert's severity while the sensor has one */}
            {severityOn && (s.status === 'down' || s.status === 'warning') && (
              <SeverityTag severity={s.severity} size="md" testId="sensor-severity" />
            )}
            {s.acknowledgement && <AckBadge ack={s.acknowledgement} />}
            {s.in_maintenance && (
              <MaintenanceBadge
                endsAt={device.data?.maintenance?.ends_at}
                name={device.data?.maintenance?.name}
              />
            )}
          </>
        }
        subtitle={
          <span className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-1">
            <bdi dir="ltr" className="font-display">
              {s.device_name}
            </bdi>
            <span>· {t(`sensorType.${s.type}`)}</span>
            {!passive && <span>· {t('sensors.everyN', { count: s.interval_s })}</span>}
          </span>
        }
        meta={
          <>
            <span>
              {t('sensors.since')}:{' '}
              <b className="num font-medium text-ink">
                <Ltr>{formatDateTime(s.last_status_change_at)}</Ltr>
              </b>
            </span>
            <span>
              {t('sensors.lastCheck')}:{' '}
              <b className="font-medium text-ink" title={formatDateTime(s.last_check_at)}>
                {formatRelative(s.last_check_at)}
              </b>
            </span>
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
              {(s.status === 'down' || s.status === 'warning' || s.status === 'unreachable') && (
                <ExplainButton sensorId={s.id} size="md" />
              )}
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
        <div className="mb-4 space-y-3">
          {s.acknowledgement && <AckDetails ack={s.acknowledgement} />}
          {lastRun && <CheckResultView result={lastRun} />}
        </div>
      )}

      {/* prototype .two: the value and its chart at the start, channels and settings at the end */}
      <div className="grid grid-cols-1 items-start gap-6 xl:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
        <div className="flex min-w-0 flex-col gap-4">
          {/* a lookup sensor: its value map first (prototype "חיישן עם מיפוי ערכים") */}
          {enumSensor && (
            <Card title={t('sensorConfig.snmp_custom.value_map')}>
              <ValueMapTable sensor={s} />
            </Card>
          )}
          {passive && <EventConditionsCard sensor={s} />}
          {!passive && (
            <section
              className="min-w-0 rounded-xl bg-surface px-[22px] pb-5 pt-5 shadow-card"
              aria-labelledby="sensor-history"
            >
              <div className="flex flex-wrap items-end justify-between gap-4">
                <div className="grid min-w-0 gap-1.5">
                  <h2 id="sensor-history" className="m-0 text-[13px] font-normal text-ink-muted">
                    {t('sensors.history')}: {channelName(activeChannel)}
                  </h2>
                  <BigValue
                    text={
                      activeMeta
                        ? sensorValueText(s, activeMeta.key, activeMeta.last_value, activeMeta.unit)
                        : '—'
                    }
                    status={activeMeta ? chStatus(activeMeta.key, activeMeta.status) : s.status}
                  />
                  {activeMeta && (
                    <span>
                      <StatusBadge status={chStatus(activeMeta.key, activeMeta.status)} />
                    </span>
                  )}
                </div>
                <div className={SEG_GROUP} role="group" aria-label={t('sensors.range')}>
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
              </div>
              <div className="mt-4">
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
              </div>
            </section>
          )}
          {/* prototype: the events list right under the chart */}
          <section aria-labelledby="sensor-events">
            <GroupTitle id="sensor-events">{t('sensors.events')}</GroupTitle>
            {events.data ? <EventRows events={events.data.items} /> : <Loading />}
          </section>
        </div>

        <div className="flex min-w-0 flex-col gap-4">
          {/* Channels: one row per channel; the chosen one drives the chart. */}
          {s.channels.length > 1 && (
            <Card title={t('sensors.channels')} bodyClassName="px-2.5 pb-3 pt-0">
              <ul className="m-0 grid list-none gap-0.5 p-0">
                {s.channels.map((c) => {
                  const cs = chStatus(c.key, c.status);
                  const selected = c.key === activeChannel;
                  return (
                    <li key={c.key}>
                      <button
                        type="button"
                        onClick={() => setChannel(c.key)}
                        aria-pressed={selected}
                        className={cx(
                          'flex w-full items-center gap-2.5 rounded-[12px] px-3 py-2.5 text-start text-[14.5px] transition-colors',
                          selected ? 'bg-chip' : 'hover:bg-surface-hover',
                        )}
                      >
                        <StatusDot status={cs} />
                        <span className="min-w-0 flex-1 truncate text-ink-muted">
                          {channelName(c.key)}
                        </span>
                        <b
                          className={cx(
                            'num font-semibold',
                            cs === 'up' || cs === 'unknown'
                              ? 'text-ink'
                              : statusClass(cs, STATUS_TEXT),
                          )}
                        >
                          <Value text={sensorValueText(s, c.key, c.last_value, c.unit)} />
                        </b>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </Card>
          )}

          {!passive && (
            <Card title={t('thresholds.title')}>
              <ThresholdEditor sensor={s} readOnly={!isAdmin} />
            </Card>
          )}

          <ImpactCard sensor={s} readOnly={!isAdmin} />

          <Card title={t('sensors.settings')}>
            <dl className="m-0 grid grid-cols-[max-content_minmax(0,1fr)] gap-x-[18px] gap-y-2.5 text-[14px]">
              {!passive && (
                <div className="contents">
                  <dt className="text-ink-muted">{t('sensors.interval')}</dt>
                  <dd className="num m-0 min-w-0 text-end">
                    <Ltr>{s.interval_s}</Ltr>
                  </dd>
                </div>
              )}
              {Object.entries(s.config)
                .filter(
                  ([k]) =>
                    !(
                      (k === 'value_map' || k === 'value_map_default' || k === 'value_map_ref') &&
                      enumSensor
                    ),
                )
                .map(([k, v]) => (
                  <div key={k} className="contents">
                    <dt className="text-ink-muted">
                      {t(`sensorConfig.${s.type}.${k}`, { defaultValue: k })}
                    </dt>
                    <dd className="m-0 min-w-0 break-all text-end font-mono text-[12.5px]">
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
              {!Object.keys(s.config).length && passive && (
                <div className="col-span-2 text-ink-muted">{t('sensors.noConfig')}</div>
              )}
            </dl>
          </Card>
        </div>
      </div>

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

/**
 * The big value of the sensor card (prototype .big): the number in 44px Inter with the unit small
 * after it; a text value (enum) in the same size. Coloured only when it is a problem.
 */
function BigValue({ text, status }: { text: string; status: string }) {
  const m = /^([-+]?[\d.,]+)\s*(.*)$/.exec(text);
  const tone =
    status === 'up' || status === 'unknown' ? 'text-ink' : statusClass(status, STATUS_TEXT);
  return (
    <div
      data-testid="sensor-big-value"
      className={cx('num text-[44px] font-semibold leading-none tracking-[-0.03em]', tone)}
    >
      {m ? (
        <bdi dir="ltr">
          {m[1]}
          {m[2] && (
            <small className="ms-1 text-[18px] font-medium tracking-normal text-ink-muted">
              {m[2]}
            </small>
          )}
        </bdi>
      ) : (
        <Value text={text} />
      )}
    </div>
  );
}

/** [1.3] Impact on the device status (prototype value-map sensor): segmented, saved at once. */
function ImpactCard({ sensor, readOnly }: { sensor: Sensor; readOnly: boolean }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const value: SensorImpact = sensor.impact ?? 'critical';
  const m = useMutation({
    mutationFn: (v: SensorImpact) => endpoints.bulkSensorImpact([sensor.id], v),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.sensor(sensor.id) });
      void qc.invalidateQueries({ queryKey: ['sensors'] });
      void qc.invalidateQueries({ queryKey: qk.tree });
      void qc.invalidateQueries({ queryKey: qk.device(sensor.device_id) });
      toast.push('success', t('impact.saved'));
    },
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  return (
    <Card title={t('impact.label')}>
      <div className="grid gap-2.5" data-testid="sensor-impact">
        {readOnly ? (
          <b className="font-medium">{t(`impact.option.${value}`)}</b>
        ) : (
          <SegmentedControl
            label={t('impact.label')}
            size="sm"
            value={value}
            onChange={(v) => v !== value && m.mutate(v)}
            options={SENSOR_IMPACTS.map((v) => ({ value: v, label: t(`impact.option.${v}`) }))}
          />
        )}
        <span className="text-[13.5px] text-ink-muted">{t(`impact.hint.${value}`)}</span>
      </div>
    </Card>
  );
}

/**
 * [2b-1] The enum value map of an snmp_custom sensor (prototype "מיפוי ערכים": value / MIB name /
 * status / text, a fixed "Other" row); the row of the current raw value is highlighted.
 */
function ValueMapTable({ sensor }: { sensor: Sensor }) {
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
    <div className="grid gap-3">
      <p className="m-0 text-[13.5px] text-ink-muted">
        {t('valueMap.hint')}
        {named && (
          <span className="ms-2 font-medium text-ink" data-testid="named-value-map">
            {pickLang(named.title, lang)}
          </span>
        )}
      </p>
      <div className="overflow-x-auto rounded-lg border border-line">
        <table className="w-full text-sm" data-testid="value-map-table">
          <thead>
            <tr>
              <th className={cx(th, 'w-[72px]')}>{t('valueMap.value')}</th>
              <th className={th}>{t('valueMap.mib')}</th>
              <th className={th}>{t('valueMap.status')}</th>
              <th className={th}>{t('valueMap.text')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr
                key={r.value}
                className={cx(r.value === rawKey && TR_SELECTED)}
                aria-current={r.value === rawKey ? 'true' : undefined}
              >
                <td className={cx(td, 'num font-medium')}>
                  <Ltr>{r.value}</Ltr>
                </td>
                <td className={cx(td, 'font-mono text-[13px] text-ink-muted')}>
                  <Ltr>{r.mib ?? (r.own && named ? t('valueMap.own') : '—')}</Ltr>
                </td>
                <td className={td}>
                  <StatusBadge status={r.status} />
                </td>
                <td className={td} dir="auto">
                  {r.text || <span className="text-ink-subtle">—</span>}
                </td>
              </tr>
            ))}
            <tr className={cx(unmapped && TR_SELECTED)}>
              <td className={cx(td, 'text-ink-muted')}>{t('valueMap.other')}</td>
              <td className={cx(td, 'text-[13px] text-ink-muted')}>{t('valueMap.otherHint')}</td>
              <td className={td}>
                <StatusBadge status={String(sensor.config.value_map_default ?? 'warning')} />
              </td>
              <td className={cx(td, 'text-ink-muted')}>
                {named
                  ? pickLang(named.unknown, lang).replace('{code}', unmapped ? rawKey! : 'N')
                  : t('valueMap.unexpected')}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      {current && raw !== null && (
        <p className="m-0 text-[13px] text-ink-muted">
          {t('valueMap.current')}: <Ltr className="num">{Math.round(raw)}</Ltr> →{' '}
          {current.text || t('valueMap.unexpected')} ({t(`status.${current.status}`)})
        </p>
      )}
      <p className="m-0 text-[12.5px] text-ink-subtle">{t('valueMap.editHint')}</p>
    </div>
  );
}
