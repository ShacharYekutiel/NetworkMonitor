import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import type { Sensor } from '../api/types';
import { formatRelative, sensorValueText } from '../lib/format';
import { StatusBadge } from './Status';
import { AckBadge } from './Acknowledge';
import { StatusMessage } from './Dependency';
import { useLocalizeMessage } from '../hooks/localize';
import { MaintenanceBadge } from './Maintenance';
import { ImpactTag } from './SensorImpact';
import { Empty, Table, td, th, Value } from './ui';

const boxCls =
  'h-4 w-4 rounded border-slate-300 text-sky-600 focus:ring-sky-500 dark:border-slate-600 dark:bg-slate-900';

/** [1.3] Row selection (checkboxes + select all) for bulk actions. */
export type SensorSelection = {
  selected: ReadonlySet<number>;
  onChange: (next: Set<number>) => void;
};

export function primaryChannel(s: Sensor) {
  return s.channels.find((c) => c.key === s.primary_channel) ?? s.channels[0];
}

export function SensorTable({
  sensors,
  showDevice,
  actions,
  selection,
}: {
  sensors: Sensor[];
  showDevice?: boolean;
  actions?: (s: Sensor) => React.ReactNode;
  selection?: SensorSelection;
}) {
  const { t } = useTranslation();
  const localize = useLocalizeMessage();
  if (!sensors.length) return <Empty>{t('sensors.none')}</Empty>;
  const sel = selection?.selected;
  const nSel = sel ? sensors.filter((s) => sel.has(s.id)).length : 0;
  const all = nSel > 0 && nSel === sensors.length;
  const toggle = (id: number, on: boolean) => {
    if (!selection) return;
    const next = new Set(selection.selected);
    if (on) next.add(id);
    else next.delete(id);
    selection.onChange(next);
  };
  return (
    <Table>
      <thead>
        <tr>
          {selection && (
            <th className={`${th} w-8`}>
              <input
                type="checkbox"
                className={boxCls}
                checked={all}
                ref={(el) => {
                  if (el) el.indeterminate = nSel > 0 && !all;
                }}
                onChange={(e) =>
                  selection.onChange(
                    e.target.checked ? new Set(sensors.map((s) => s.id)) : new Set<number>(),
                  )
                }
                aria-label={t('common.selectAll')}
                data-testid="sensor-select-all"
              />
            </th>
          )}
          <th className={th}>{t('sensors.status')}</th>
          {showDevice && <th className={th}>{t('sensors.device')}</th>}
          <th className={th}>{t('sensors.name')}</th>
          <th className={th}>{t('sensors.type')}</th>
          <th className={th}>{t('sensors.value')}</th>
          <th className={th}>{t('sensors.message')}</th>
          <th className={th}>{t('sensors.lastCheck')}</th>
          {actions && <th className={th} />}
        </tr>
      </thead>
      <tbody>
        {sensors.map((s) => {
          const pc = primaryChannel(s);
          return (
            <tr
              key={s.id}
              className={
                sel?.has(s.id)
                  ? 'bg-sky-50 dark:bg-sky-950/40'
                  : 'hover:bg-slate-50 dark:hover:bg-slate-800/40'
              }
            >
              {selection && (
                <td className={`${td} w-8`}>
                  <input
                    type="checkbox"
                    className={boxCls}
                    checked={!!sel?.has(s.id)}
                    onChange={(e) => toggle(s.id, e.target.checked)}
                    aria-label={t('impact.selectSensor', { name: s.name })}
                    data-testid="sensor-select"
                  />
                </td>
              )}
              <td className={td}>
                <span className="inline-flex items-center gap-1">
                  <StatusBadge status={s.status} />
                  {s.acknowledgement && <AckBadge ack={s.acknowledgement} compact />}
                  {s.in_maintenance && <MaintenanceBadge compact />}
                </span>
              </td>
              {showDevice && (
                <td className={`${td} whitespace-nowrap`}>
                  <Link className="hover:underline" to={`/devices/${s.device_id}`}>
                    {s.device_name}
                  </Link>
                </td>
              )}
              <td className={td}>
                <Link
                  className="font-medium text-sky-700 hover:underline dark:text-sky-400"
                  to={`/sensors/${s.id}`}
                >
                  {s.name}
                </Link>
                <ImpactTag impact={s.impact} />
              </td>
              <td className={`${td} whitespace-nowrap text-slate-500`}>
                {t(`sensorType.${s.type}`)}
              </td>
              <td className={`${td} whitespace-nowrap tabular-nums`}>
                {pc ? <Value text={sensorValueText(s, pc.key, pc.last_value, pc.unit)} /> : '—'}
              </td>
              <td
                dir="auto"
                className={`${td} max-w-xs truncate text-slate-500`}
                title={localize(s.status_message)}
              >
                <StatusMessage
                  message={s.status_message}
                  status={s.status}
                  deviceId={s.device_id}
                />
              </td>
              <td className={`${td} whitespace-nowrap text-slate-500`}>
                {formatRelative(s.last_check_at)}
              </td>
              {actions && <td className={`${td} whitespace-nowrap text-end`}>{actions(s)}</td>}
            </tr>
          );
        })}
      </tbody>
    </Table>
  );
}
