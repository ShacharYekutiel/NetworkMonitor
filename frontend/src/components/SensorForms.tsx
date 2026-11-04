import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk, type SensorInput } from '../api/endpoints';
import type { CheckResult, Device, Sensor, SensorType, SnmpInterface } from '../api/types';
import { SENSOR_TYPES } from '../api/types';
import { useSensorTypes } from '../hooks/queries';
import {
  configFromRaw,
  rawFromConfig,
  type ConfigErrors,
  type RawConfig,
} from '../lib/sensorConfig';
import { formatValue } from '../lib/format';
import { errorMessage } from '../lib/errors';
import { CheckResultView } from './CheckResultView';
import { SensorConfigFields } from './SensorConfigFields';
import { useToast } from './toast';
import {
  Button,
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
    const { config, errors: errs } = configFromRaw(def.config_fields, raw);
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
      name: name.trim() || t(`sensorType.${type}`),
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
          <div className="grid gap-3 sm:grid-cols-3">
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
              <>
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
                <Field
                  label={t('sensors.interval')}
                  hint={
                    intervalVal === 'invalid'
                      ? t('validation.interval')
                      : t('sensors.intervalDefault')
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
              </>
            )}
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
              <SensorConfigFields
                type={type}
                fields={def.config_fields}
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
                    <Ltr>{formatValue(i.speed_bps, 'bit/s')}</Ltr>
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
    const { config, errors: errs } = configFromRaw(def.config_fields, raw);
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
          <div className="grid gap-3 sm:grid-cols-3">
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
          <SensorConfigFields
            type={sensor.type}
            fields={def.config_fields}
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
