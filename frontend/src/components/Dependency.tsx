// [2b-1] Device dependencies: device picker, dependency editor, effective parent, parent banner.
import { useId, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Trans, useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { Dependency, DependencyInput, EffectiveDependency } from '../api/types';
import { useTree } from '../hooks/queries';
import { allDevices, findGroup } from '../lib/tree';
import { Field, FieldGroup, Input, Select } from './ui';
import { useLocalizeMessage } from '../hooks/localize';

/** Device select from the tree; shows a search box when the list is long. */
export function DevicePicker({
  value,
  onChange,
  exclude = [],
  allowNone,
  label,
}: {
  value: number | null;
  onChange: (v: number | null) => void;
  exclude?: number[];
  allowNone?: boolean;
  /** Accessible name of the select (the search box has its own), usually the field label. */
  label: string;
}) {
  const { t } = useTranslation();
  const { data } = useTree();
  const [q, setQ] = useState('');
  const searchId = useId();
  const all = useMemo(
    () =>
      allDevices(data?.groups ?? [])
        .filter((x) => !exclude.includes(x.device.id))
        .sort((a, b) => a.device.name.localeCompare(b.device.name)),
    [data, exclude],
  );
  const query = q.trim().toLowerCase();
  const opts = query
    ? all.filter(
        (x) =>
          x.device.id === value ||
          x.device.name.toLowerCase().includes(query) ||
          x.device.host.toLowerCase().includes(query),
      )
    : all;
  return (
    <div className="flex flex-col gap-1 sm:flex-row">
      {all.length > 8 && (
        <Input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t('dependency.searchDevice')}
          aria-label={`${label}: ${t('dependency.searchDevice')}`}
          id={searchId}
          className="sm:w-40"
        />
      )}
      <Select
        value={value === null ? '' : String(value)}
        onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}
        aria-label={label}
      >
        {(allowNone || value === null) && <option value="">{t('dependency.chooseDevice')}</option>}
        {opts.map(({ device, group }) => (
          <option key={device.id} value={device.id}>
            {device.name} ({device.host}) — {group.name}
          </option>
        ))}
      </Select>
    </div>
  );
}

export function dependencyInputFrom(d: Dependency | null | undefined): DependencyInput {
  if (!d) return { mode: 'inherit', device_id: null, sensor_id: null };
  return {
    mode: d.mode,
    device_id: d.mode === 'device' ? d.device_id : null,
    sensor_id: d.mode === 'device' ? d.sensor_id : null,
  };
}

/** Normalises the editor value for the API (ids null unless mode = device). */
export function dependencyToInput(d: DependencyInput): DependencyInput {
  return d.mode === 'device'
    ? { mode: 'device', device_id: d.device_id, sensor_id: d.device_id ? d.sensor_id : null }
    : { mode: d.mode, device_id: null, sensor_id: null };
}

/**
 * Radio inherit / none / device, then a device picker (excluding `selfDeviceId`) and an
 * optional dependency sensor of that device.
 */
export function DependencyEditor({
  value,
  onChange,
  selfDeviceId,
  forGroup,
  effective,
}: {
  value: DependencyInput;
  onChange: (v: DependencyInput) => void;
  selfDeviceId?: number;
  forGroup?: boolean;
  /** device forms: the stored effective dependency (read-only line) */
  effective?: EffectiveDependency | null;
}) {
  const { t } = useTranslation();
  const sensors = useQuery({
    queryKey: qk.sensors({ device_id: value.device_id }),
    queryFn: () => endpoints.sensors({ device_id: value.device_id ?? undefined }),
    enabled: value.mode === 'device' && value.device_id !== null,
  });
  const exclude = useMemo(() => (selfDeviceId ? [selfDeviceId] : []), [selfDeviceId]);
  const modes: DependencyInput['mode'][] = ['inherit', 'none', 'device'];
  return (
    <fieldset className="min-w-0 rounded-md border border-line p-3">
      <legend className="px-1 text-xs font-semibold text-ink-muted">{t('dependency.title')}</legend>
      <p className="mb-2 text-xs text-ink-muted">
        {forGroup ? t('dependency.groupHint') : t('dependency.deviceHint')}
      </p>
      <FieldGroup label={t('dependency.mode')}>
        <div className="flex flex-col gap-1 sm:flex-row sm:flex-wrap sm:gap-x-5">
          {modes.map((m) => (
            <label key={m} className="inline-flex items-center gap-2 text-sm">
              <input
                type="radio"
                name={forGroup ? 'dep-mode-group' : 'dep-mode-device'}
                checked={value.mode === m}
                onChange={() =>
                  onChange({
                    mode: m,
                    device_id: m === 'device' ? value.device_id : null,
                    sensor_id: null,
                  })
                }
              />
              {m === 'inherit'
                ? t(forGroup ? 'dependency.modes.inheritParent' : 'dependency.modes.inherit')
                : t(`dependency.modes.${m}`)}
            </label>
          ))}
        </div>
      </FieldGroup>
      {value.mode === 'device' && (
        <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label={t('dependency.parentDevice')} required>
            <DevicePicker
              label={t('dependency.parentDevice')}
              value={value.device_id}
              exclude={exclude}
              onChange={(id) => onChange({ ...value, device_id: id, sensor_id: null })}
            />
          </Field>
          <Field label={t('dependency.sensor')} hint={t('dependency.sensorHint')}>
            <Select
              value={value.sensor_id === null ? '' : String(value.sensor_id)}
              disabled={value.device_id === null}
              onChange={(e) =>
                onChange({ ...value, sensor_id: e.target.value ? Number(e.target.value) : null })
              }
            >
              <option value="">{t('dependency.sensorAuto')}</option>
              {sensors.data?.items.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({t(`sensorType.${s.type}`)})
                </option>
              ))}
            </Select>
          </Field>
        </div>
      )}
      {effective !== undefined && (
        <p className="mt-2 text-xs text-ink-muted">
          <EffectiveDependencyText effective={effective} />
        </p>
      )}
    </fieldset>
  );
}

/** "Effective: depends on branch-gw (from group Branch)" / "Effective: no parent". */
export function EffectiveDependencyText({
  effective,
  bare,
}: {
  effective: EffectiveDependency | null;
  /** without the "Effective:" wording (e.g. after a "Parent:" label) */
  bare?: boolean;
}) {
  const { t } = useTranslation();
  const { data: tree } = useTree();
  if (!effective) return <>{bare ? t('dependency.modes.none') : t('dependency.effectiveNone')}</>;
  const groupName =
    effective.source === 'group' && effective.group_id !== null && tree
      ? (findGroup(tree.groups, effective.group_id)?.name ?? `#${effective.group_id}`)
      : null;
  return (
    <>
      {!bare && <>{t('dependency.effective')} </>}
      <Link
        className="font-medium text-accent-ink hover:underline"
        to={`/devices/${effective.device_id}`}
      >
        {effective.device_name}
      </Link>
      {effective.sensor_name && <span className="text-ink-muted"> / {effective.sensor_name}</span>}{' '}
      {groupName ? (
        <span className="text-ink-muted">({t('dependency.fromGroup', { group: groupName })})</span>
      ) : (
        <span className="text-ink-muted">({t('dependency.fromDevice')})</span>
      )}
    </>
  );
}

/** Violet banner on the device page while the effective parent is down. */
export function ParentDownBanner({ effective }: { effective: EffectiveDependency }) {
  const { t } = useTranslation();
  return (
    <div
      role="status"
      className="rounded-md bg-status-unreachable-soft px-3 py-2 text-sm text-status-unreachable"
    >
      <span className="me-1 inline-block h-2.5 w-2.5 rounded-full bg-status-unreachable align-middle" />
      {t('dependency.parentDownPrefix')}{' '}
      <Link className="font-semibold underline" to={`/devices/${effective.device_id}`}>
        {effective.device_name}
      </Link>{' '}
      {t('dependency.parentDownSuffix')}
    </div>
  );
}

/** Resolves device ids to names (for dependency_cycle paths). */
export function useDeviceNames(): (id: number) => string | undefined {
  const { data } = useTree();
  return useMemo(() => {
    const m = new Map(allDevices(data?.groups ?? []).map((x) => [x.device.id, x.device.name]));
    return (id: number) => m.get(id);
  }, [data]);
}

const UNREACHABLE_RE = /^Unreachable \(parent (.+?) down\)(?::\s*([\s\S]*))?$/;

/** Splits "Unreachable (parent X down): msg" into the parent name and the original message. */
export function parseUnreachable(message: string): { parent: string; rest: string } | null {
  const m = UNREACHABLE_RE.exec(message);
  return m ? { parent: m[1], rest: m[2] ?? '' } : null;
}

/**
 * A sensor status message; for `unreachable` the "Unreachable (parent X down)" prefix is
 * translated and X links to the parent device (from the tree's effective parent).
 */
export function StatusMessage({
  message,
  status,
  deviceId,
  className,
}: {
  message: string;
  status: string;
  deviceId: number;
  className?: string;
}) {
  const { data } = useTree();
  const localize = useLocalizeMessage(); // [1.3.2] named-map texts in the viewer's language
  const parsed = status === 'unreachable' ? parseUnreachable(message) : null;
  if (!parsed)
    return (
      <bdi dir="auto" className={className}>
        {localize(message)}
      </bdi>
    );
  const parentId = allDevices(data?.groups ?? []).find((x) => x.device.id === deviceId)?.device
    .parent_device_id;
  return (
    <bdi dir="auto" className={className}>
      <Trans
        i18nKey="dependency.unreachableMsg"
        values={{ name: parsed.parent }}
        components={{
          p: parentId ? (
            <Link
              className="font-medium text-status-unreachable underline"
              to={`/devices/${parentId}`}
            />
          ) : (
            <span className="font-medium" />
          ),
        }}
      />
      {parsed.rest && <>: {localize(parsed.rest)}</>}
    </bdi>
  );
}
