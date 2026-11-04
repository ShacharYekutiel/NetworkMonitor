import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import type { Sensor } from '../api/types';
import { formatRelative, sensorValueText } from '../lib/format';
import { StatusBadge } from './Status';
import { Empty, Table, td, th, Value } from './ui';

export function primaryChannel(s: Sensor) {
  return s.channels.find((c) => c.key === s.primary_channel) ?? s.channels[0];
}

export function SensorTable({
  sensors,
  showDevice,
  actions,
}: {
  sensors: Sensor[];
  showDevice?: boolean;
  actions?: (s: Sensor) => React.ReactNode;
}) {
  const { t } = useTranslation();
  if (!sensors.length) return <Empty>{t('sensors.none')}</Empty>;
  return (
    <Table>
      <thead>
        <tr>
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
            <tr key={s.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
              <td className={td}>
                <StatusBadge status={s.status} />
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
                title={s.status_message}
              >
                {s.status_message}
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
