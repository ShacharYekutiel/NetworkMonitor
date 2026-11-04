import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk, type SensorInput } from '../api/endpoints';
import type {
  CheckResult,
  Device,
  Sensor,
  SensorType,
  SnmpInterface,
  SnmpStorage,
} from '../api/types';
import { SENSOR_TYPES } from '../api/types';
import { useSensorTypes } from '../hooks/queries';
import {
  buildSensorConfig,
  rawFromConfig,
  visibleConfigFields,
  type ConfigErrors,
  type RawConfig,
} from '../lib/sensorConfig';
import { formatValue, storageUsedPct } from '../lib/format';
import { errorMessage } from '../lib/errors';
import { CheckResultView } from './CheckResultView';
import { SensorConfigFields } from './SensorConfigFields';
import { useToast } from './toast';
import {
  Button,
  cx,
  Empty,
  ErrorBox,
  Field,
  Input,
  Loading,
  Ltr,
  Modal,
  Select,
  Table,
  td,
  th,
  Value,
} from './ui';

function parseInterval(s: string): number | undefined | 'invalid' {
  const v = s.trim();
  if (!v) return undefined;
  if (!/^\d+$/.test(v)) return 'invalid';
  const n = Number(v);
  return n >= 10 && n <= 86400 ? n : 'invalid';
}

export function AddSensorModal({
  open,
  onClose,
  device,
}: {
  open: boolean;
  onClose: () => void;
  device: Device;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const types = useSensorTypes();
  const [type, setType] = useState<SensorType>('ping');
  const [name, setName] = useState('');
  const [nameTouched, setNameTouched] = useState(false);
  const [interval, setInterval] = useState('');
  const [raw, setRaw] = useState<RawConfig>({});
  const [errors, setErrors] = useState<ConfigErrors>({});
  const [testResult, setTestResult] = useState<CheckResult | null>(null);

  const def = types.data?.find((x) => x.type === type);
  const available = SENSOR_TYPES.filter((x) => types.data?.some((d) => d.type === x));

  useEffect(() => {
    if (!open) return;
    setType('ping');
    setNameTouched(false);
    setInterval('');
    setTestResult(null);
  }, [open]);

  useEffect(() => {
    if (!def) return;
    setRaw(rawFromConfig(def.config_fields));
    setErrors({});
    setTestResult(null);
    if (!nameTouched) setName(t(`sensorType.${type}`));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [def, type]);

  const close = () => {
    create.reset();
    test.reset();
    onClose();
  };

  const create = useMutation({
    mutationFn: (body: SensorInput) => endpoints.createSensor(body),
    onSuccess: (s) => {
      qc.setQueryData(qk.sensor(s.id), s);
      void qc.invalidateQueries({ queryKey: ['sensors'] });
      void qc.invalidateQueries({ queryKey: qk.tree });
      toast.push('success', t('sensors.created', { name: s.name }));
      close();
    },
  });
  const test = useMutation({
    mutationFn: (config: Record<string, unknown>) =>
      endpoints.testSensor({ device_id: device.id, type, config }),
    onSuccess: setTestResult,
  });

  const build = () => {
    if (!def) return null;
    const { config, errors: errs } = buildSensorConfig(type, def.config_fields, raw);
    setErrors(errs);
    return Object.keys(errs).length ? null : config;
  };

  const intervalVal = parseInterval(interval);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const config = build();
    if (!config || intervalVal === 'invalid') return;
    create.mutate({
      device_id: device.id,
      type,
      name: defaultSensorName(t, type, name, nameTouched, config),
      config,
      interval_s: intervalVal,
      enabled: true,
    });
  };

  return (
    <Modal open={open} onClose={close} title={t('sensors.add')} wide>
      {types.isLoading && <Loading />}
      <ErrorBox error={types.error} />
      {def && (
        <div className="space-y-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Field label={t('sensors.type')}>
              <Select value={type} onChange={(e) => setType(e.target.value as SensorType)}>
                {available.map((x) => (
                  <option key={x} value={x}>
                    {t(`sensorType.${x}`)}
                  </option>
                ))}
              </Select>
            </Field>
            {type !== 'snmp_interface' && (
              <Field label={t('common.name')}>
                <Input
                  value={name}
                  maxLength={128}
                  onChange={(e) => {
                    setName(e.target.value);
                    setNameTouched(true);
                  }}
                />
              </Field>
            )}
            {/* Shown for every type: interface sensors created by the picker use it too. */}
            <Field
              label={t('sensors.interval')}
              hint={
                intervalVal === 'invalid' ? t('validation.interval') : t('sensors.intervalDefault')
              }
            >
              <Input
                type="number"
                min={10}
                max={86400}
                value={interval}
                placeholder={t('common.default')}
                onChange={(e) => setInterval(e.target.value)}
                dir="ltr"
              />
            </Field>
          </div>
          {def.requires_snmp && (
            <p className="text-xs text-slate-500">
              {t('sensors.requiresSnmp', {
                source: t(`snmp.source.${device.effective_snmp_source}`),
              })}
            </p>
          )}
          {type === 'snmp_interface' ? (
            <InterfacePicker device={device} interval={intervalVal} onDone={close} />
          ) : (
            <form onSubmit={submit} className="space-y-4">
              {type === 'snmp_disk' && (
                <StoragePicker
                  deviceId={device.id}
                  mode="multi"
                  interval={intervalVal}
                  onPick={(descr) => setRaw((r) => ({ ...r, storage_descr: descr }))}
                  onDone={close}
                />
              )}
              <SensorConfigFields
                type={type}
                fields={visibleConfigFields(type, def.config_fields, raw)}
                raw={raw}
                onChange={setRaw}
                errors={errors}
              />
              {testResult && <CheckResultView result={testResult} />}
              <ErrorBox error={test.error ?? create.error} />
              <div className="flex justify-end gap-2">
                <Button onClick={close}>{t('common.cancel')}</Button>
                <Button
                  busy={test.isPending}
                  onClick={() => {
                    const c = build();
                    if (c) test.mutate(c);
                  }}
                >
                  {t('sensors.test')}
                </Button>
                <Button type="submit" variant="primary" busy={create.isPending}>
                  {t('sensors.create')}
                </Button>
              </div>
            </form>
          )}
        </div>
      )}
    </Modal>
  );
}

function InterfacePicker({
  device,
  interval,
  onDone,
}: {
  device: Device;
  interval: number | undefined | 'invalid';
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({
    queryKey: qk.snmpInterfaces(device.id),
    queryFn: () => endpoints.snmpInterfaces(device.id),
    retry: false,
    staleTime: 0,
  });
  const [selected, setSelected] = useState<number[]>([]);
  const [filter, setFilter] = useState('');
  const items = useMemo(() => {
    const f = filter.trim().toLowerCase();
    const all = q.data?.items ?? [];
    return f
      ? all.filter((i) => [i.name, i.descr, i.alias].some((x) => x?.toLowerCase().includes(f)))
      : all;
  }, [q.data, filter]);

  const add = useMutation({
    mutationFn: async (ifs: SnmpInterface[]) => {
      const results = await Promise.allSettled(
        ifs.map((i) =>
          endpoints.createSensor({
            device_id: device.id,
            type: 'snmp_interface',
            name: i.name || i.descr || `if${i.if_index}`,
            config: { if_index: i.if_index, if_name: i.name || i.descr },
            interval_s: interval === 'invalid' ? undefined : interval,
            enabled: true,
          }),
        ),
      );
      const failed = results.filter((r) => r.status === 'rejected');
      if (failed.length === results.length && failed[0])
        throw (failed[0] as PromiseRejectedResult).reason;
      return { ok: results.length - failed.length, failed: failed.length };
    },
    onSuccess: ({ ok, failed }) => {
      void qc.invalidateQueries({ queryKey: ['sensors'] });
      void qc.invalidateQueries({ queryKey: qk.tree });
      void qc.invalidateQueries({ queryKey: qk.snmpInterfaces(device.id) });
      toast.push(failed ? 'error' : 'success', t('sensors.interfacesAdded', { ok, failed }));
      onDone();
    },
  });

  if (q.isLoading) return <Loading />;
  if (q.error)
    return (
      <div className="space-y-2">
        <ErrorBox error={q.error} />
        <Button size="sm" onClick={() => void q.refetch()}>
          {t('common.retry')}
        </Button>
      </div>
    );
  const all = q.data?.items ?? [];
  if (!all.length) return <Empty>{t('sensors.noInterfaces')}</Empty>;
  const selectable = items.filter((i) => i.existing_sensor_id === null);
  const allSelected =
    selectable.length > 0 && selectable.every((i) => selected.includes(i.if_index));

  return (
    <div className="space-y-3">
      <Input
        placeholder={t('common.filter')}
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      <div className="max-h-80 overflow-y-auto rounded ring-1 ring-slate-200 dark:ring-slate-700">
        <Table>
          <thead>
            <tr>
              <th className={th}>
                <input
                  type="checkbox"
                  aria-label={t('common.selectAll')}
                  checked={allSelected}
                  onChange={(e) =>
                    setSelected(e.target.checked ? selectable.map((i) => i.if_index) : [])
                  }
                />
              </th>
              <th className={th}>#</th>
              <th className={th}>{t('iface.name')}</th>
              <th className={th}>{t('iface.alias')}</th>
              <th className={th}>{t('iface.speed')}</th>
              <th className={th}>{t('iface.status')}</th>
            </tr>
          </thead>
          <tbody>
            {items.map((i) => {
              const exists = i.existing_sensor_id !== null;
              return (
                <tr key={i.if_index} className={exists ? 'opacity-50' : undefined}>
                  <td className={td}>
                    <input
                      type="checkbox"
                      disabled={exists}
                      checked={exists || selected.includes(i.if_index)}
                      onChange={(e) =>
                        setSelected((s) =>
                          e.target.checked ? [...s, i.if_index] : s.filter((x) => x !== i.if_index),
                        )
                      }
                      aria-label={i.name}
                    />
                  </td>
                  <td className={`${td} tabular-nums`}>{i.if_index}</td>
                  <td className={td}>
                    <div className="font-medium">
                      <Ltr>{i.name}</Ltr>
                    </div>
                    <div className="text-xs text-slate-500">
                      <Ltr>{i.descr}</Ltr>
                    </div>
                  </td>
                  <td className={td}>{i.alias}</td>
                  <td className={`${td} whitespace-nowrap`}>
                    <Value text={formatValue(i.speed_bps, 'bit/s')} />
                  </td>
                  <td className={`${td} whitespace-nowrap text-xs`}>
                    <span className={i.oper_status === 'up' ? 'text-green-600' : 'text-slate-500'}>
                      {i.admin_status}/{i.oper_status}
                    </span>
                    {exists && <span className="ms-2 text-slate-500">({t('iface.exists')})</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      </div>
      <ErrorBox error={add.error} />
      <div className="flex items-center justify-end gap-2">
        <span className="text-sm text-slate-500">
          {t('iface.selected', { count: selected.length })}
        </span>
        <Button onClick={onDone}>{t('common.cancel')}</Button>
        <Button
          variant="primary"
          disabled={!selected.length || interval === 'invalid'}
          busy={add.isPending}
          onClick={() => add.mutate(all.filter((i) => selected.includes(i.if_index)))}
        >
          {t('iface.add', { count: selected.length })}
        </Button>
      </div>
    </div>
  );
}

export function EditSensorModal({
  open,
  onClose,
  sensor,
}: {
  open: boolean;
  onClose: () => void;
  sensor: Sensor;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const types = useSensorTypes();
  const def = types.data?.find((x) => x.type === sensor.type);
  const [name, setName] = useState(sensor.name);
  const [interval, setInterval] = useState(String(sensor.interval_s));
  const [sortOrder, setSortOrder] = useState(String(sensor.sort_order));
  const [raw, setRaw] = useState<RawConfig>({});
  const [errors, setErrors] = useState<ConfigErrors>({});

  useEffect(() => {
    if (!open) return;
    setName(sensor.name);
    setInterval(String(sensor.interval_s));
    setSortOrder(String(sensor.sort_order));
    if (def) setRaw(rawFromConfig(def.config_fields, sensor.config));
    setErrors({});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, def]);

  const m = useMutation({
    mutationFn: (body: SensorInput) => endpoints.updateSensor(sensor.id, body),
    onSuccess: (s) => {
      qc.setQueryData(qk.sensor(s.id), s);
      void qc.invalidateQueries({ queryKey: ['sensors'] });
      void qc.invalidateQueries({ queryKey: qk.tree });
      toast.push('success', t('common.saved'));
      onClose();
    },
  });
  const intervalVal = parseInterval(interval);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!def || intervalVal === 'invalid' || intervalVal === undefined) return;
    const { config, errors: errs } = buildSensorConfig(sensor.type, def.config_fields, raw);
    setErrors(errs);
    if (Object.keys(errs).length) return;
    m.mutate({
      name: name.trim(),
      interval_s: intervalVal,
      sort_order: Number(sortOrder) || 0,
      config,
    });
  };

  return (
    <Modal open={open} onClose={onClose} title={t('sensors.edit')} wide>
      {!def ? (
        <Loading />
      ) : (
        <form onSubmit={submit} className="space-y-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Field label={t('common.name')} required>
              <Input
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
                maxLength={128}
              />
            </Field>
            <Field
              label={t('sensors.interval')}
              hint={
                intervalVal === 'invalid' || intervalVal === undefined
                  ? t('validation.interval')
                  : undefined
              }
              required
            >
              <Input
                type="number"
                min={10}
                max={86400}
                value={interval}
                onChange={(e) => setInterval(e.target.value)}
                dir="ltr"
              />
            </Field>
            <Field label={t('common.sortOrder')}>
              <Input
                type="number"
                value={sortOrder}
                onChange={(e) => setSortOrder(e.target.value)}
                dir="ltr"
              />
            </Field>
          </div>
          {sensor.type === 'snmp_disk' && (
            <StoragePicker
              deviceId={sensor.device_id}
              mode="single"
              onPick={(descr) => setRaw((r) => ({ ...r, storage_descr: descr }))}
            />
          )}
          <SensorConfigFields
            type={sensor.type}
            fields={visibleConfigFields(sensor.type, def.config_fields, raw)}
            raw={raw}
            onChange={setRaw}
            errors={errors}
          />
          <ErrorBox error={m.error} />
          <div className="flex justify-end gap-2">
            <Button onClick={onClose}>{t('common.cancel')}</Button>
            <Button type="submit" variant="primary" busy={m.isPending}>
              {t('common.save')}
            </Button>
          </div>
        </form>
      )}
    </Modal>
  );
}

export function useSensorActions() {
  const qc = useQueryClient();
  const toast = useToast();
  const { t } = useTranslation();
  const toggle = useMutation({
    mutationFn: (s: Sensor) => endpoints.updateSensor(s.id, { enabled: !s.enabled }),
    onSuccess: (s) => {
      qc.setQueryData(qk.sensor(s.id), s);
      void qc.invalidateQueries({ queryKey: ['sensors'] });
      void qc.invalidateQueries({ queryKey: qk.tree });
      toast.push('success', s.enabled ? t('sensors.resumed') : t('sensors.paused'));
    },
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  return { toggle };
}

/** Localized default name, like the backend's defaults (label / "Disk <descr>"). */
export function defaultSensorName(
  t: (k: string, o?: Record<string, unknown>) => string,
  type: SensorType,
  name: string,
  nameTouched: boolean,
  config: Record<string, unknown>,
): string {
  const typed = name.trim();
  if (typed && nameTouched) return typed;
  if (type === 'snmp_custom') {
    const label = String(config.label ?? '').trim();
    if (label) return label.slice(0, 128);
    if (config.oid) return `SNMP ${String(config.oid)}`.slice(0, 128);
  }
  if (type === 'snmp_disk' && config.storage_descr)
    return t('sensors.diskName', { descr: String(config.storage_descr) }).slice(0, 128);
  return typed || t(`sensorType.${type}`);
}

const HIDDEN_STORAGE = ['ram', 'virtual_memory'];

/** Picks hrStorage rows for `snmp_disk` (GET /devices/{id}/snmp/storage). */
function StoragePicker({
  deviceId,
  mode,
  interval,
  onPick,
  onDone,
}: {
  deviceId: number;
  mode: 'multi' | 'single';
  interval?: number | undefined | 'invalid';
  onPick: (descr: string) => void;
  onDone?: () => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const q = useQuery({
    queryKey: qk.snmpStorage(deviceId),
    queryFn: () => endpoints.snmpStorage(deviceId),
    enabled: open,
    retry: false,
    staleTime: 0,
  });
  const rows = useMemo(
    () => (q.data?.items ?? []).filter((r) => !HIDDEN_STORAGE.includes(r.type)),
    [q.data],
  );
  const [selected, setSelected] = useState<number[] | null>(null);
  // Preselect fixed disks that are not monitored yet (api.md).
  const sel =
    selected ??
    rows
      .filter((r) => r.type === 'fixed_disk' && r.existing_sensor_id === null)
      .map((r) => r.index);

  const add = useMutation({
    mutationFn: async (items: SnmpStorage[]) => {
      const results = await Promise.allSettled(
        items.map((r) =>
          endpoints.createSensor({
            device_id: deviceId,
            type: 'snmp_disk',
            name: t('sensors.diskName', { descr: r.descr }).slice(0, 128),
            config: { storage_descr: r.descr },
            interval_s: interval === 'invalid' ? undefined : interval,
            enabled: true,
          }),
        ),
      );
      const failed = results.filter((r) => r.status === 'rejected');
      if (failed.length === results.length && failed[0])
        throw (failed[0] as PromiseRejectedResult).reason;
      return { ok: results.length - failed.length, failed: failed.length };
    },
    onSuccess: ({ ok, failed }) => {
      void qc.invalidateQueries({ queryKey: ['sensors'] });
      void qc.invalidateQueries({ queryKey: qk.tree });
      void qc.invalidateQueries({ queryKey: qk.snmpStorage(deviceId) });
      toast.push(failed ? 'error' : 'success', t('storage.added', { ok, failed }));
      onDone?.();
    },
  });

  if (!open)
    return (
      <Button size="sm" onClick={() => setOpen(true)}>
        {t('storage.pick')}
      </Button>
    );

  return (
    <div className="space-y-2 rounded-md p-3 ring-1 ring-slate-200 dark:ring-slate-700">
      <div className="text-xs font-medium text-slate-600 dark:text-slate-400">
        {t('storage.title')}
      </div>
      {q.isLoading && <Loading />}
      {q.error && (
        <div className="space-y-2">
          <ErrorBox error={q.error} />
          <Button size="sm" onClick={() => void q.refetch()}>
            {t('common.retry')}
          </Button>
        </div>
      )}
      {q.data && !rows.length && <Empty>{t('storage.none')}</Empty>}
      {rows.length > 0 && (
        <div className="max-h-64 overflow-y-auto">
          <Table>
            <thead>
              <tr>
                {mode === 'multi' && <th className={th} />}
                <th className={th}>{t('storage.descr')}</th>
                <th className={th}>{t('storage.type')}</th>
                <th className={th}>{t('storage.size')}</th>
                <th className={th}>{t('storage.used')}</th>
                {mode === 'single' && <th className={th} />}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const exists = r.existing_sensor_id !== null;
                const pct = storageUsedPct(r);
                return (
                  <tr key={r.index} className={cx(exists && mode === 'multi' && 'opacity-50')}>
                    {mode === 'multi' && (
                      <td className={td}>
                        <input
                          type="checkbox"
                          aria-label={r.descr}
                          disabled={exists}
                          checked={exists || sel.includes(r.index)}
                          onChange={(e) =>
                            setSelected(
                              e.target.checked
                                ? [...sel, r.index]
                                : sel.filter((x) => x !== r.index),
                            )
                          }
                        />
                      </td>
                    )}
                    <td className={`${td} max-w-[16rem] break-all`}>
                      <Ltr>{r.descr}</Ltr>
                      {exists && (
                        <span className="ms-2 text-xs text-slate-500">({t('iface.exists')})</span>
                      )}
                    </td>
                    <td className={`${td} whitespace-nowrap text-xs text-slate-500`}>
                      {t(`storage.types.${r.type}`)}
                    </td>
                    <td className={`${td} whitespace-nowrap tabular-nums`}>
                      <Value text={formatValue(r.size_bytes, 'B')} />
                    </td>
                    <td className={`${td} whitespace-nowrap tabular-nums`}>
                      <Value text={formatValue(pct, '%')} />
                    </td>
                    {mode === 'single' && (
                      <td className={`${td} text-end`}>
                        <Button
                          size="sm"
                          onClick={() => {
                            onPick(r.descr);
                            setOpen(false);
                          }}
                        >
                          {t('storage.use')}
                        </Button>
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </div>
      )}
      {mode === 'multi' && rows.length > 0 && (
        <>
          <ErrorBox error={add.error} />
          <div className="flex flex-wrap items-center justify-end gap-2">
            <span className="me-auto text-xs text-slate-500">{t('storage.manualHint')}</span>
            <Button
              variant="primary"
              disabled={!sel.length || interval === 'invalid'}
              busy={add.isPending}
              onClick={() => add.mutate(rows.filter((r) => sel.includes(r.index)))}
            >
              {t('iface.add', { count: sel.length })}
            </Button>
          </div>
        </>
      )}
      {mode === 'multi' && (q.error || (q.data && !rows.length)) && (
        <p className="text-xs text-slate-500">{t('storage.manualHint')}</p>
      )}
      {mode === 'single' && (
        <div className="flex justify-end">
          <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
            {t('common.close')}
          </Button>
        </div>
      )}
    </div>
  );
}
