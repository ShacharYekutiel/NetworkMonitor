import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import type { NmEvent } from '../api/types';
import { formatDateTime, formatRelative } from '../lib/format';
import { StatusBadge } from './Status';
import { ChevronIcon, WrenchIcon } from './icons';
import { StatusMessage } from './Dependency';
import { Empty, Table, td, th } from './ui';

export function EventKindLabel({ kind }: { kind: NmEvent['kind'] }) {
  const { t } = useTranslation();
  return (
    <span className="text-xs text-slate-500">{t(`eventKind.${kind}`, { defaultValue: kind })}</span>
  );
}

export function StatusTransition({ ev }: { ev: NmEvent }) {
  if (!ev.new_status) return null;
  return (
    <span className="inline-flex items-center gap-1">
      {ev.old_status && ev.old_status !== ev.new_status && <StatusBadge status={ev.old_status} />}
      {ev.old_status && ev.old_status !== ev.new_status && (
        <ChevronIcon size={12} className="text-slate-400 rtl:rotate-180" />
      )}
      <StatusBadge status={ev.new_status} />
    </span>
  );
}

export function EventsTable({
  events,
  showSource = true,
  showKind = true,
}: {
  events: NmEvent[];
  showSource?: boolean;
  showKind?: boolean;
}) {
  const { t } = useTranslation();
  if (!events.length) return <Empty>{t('events.none')}</Empty>;
  return (
    <Table>
      <thead>
        <tr>
          <th className={th}>{t('events.time')}</th>
          {showKind && <th className={th}>{t('events.kind')}</th>}
          {showSource && <th className={th}>{t('events.source')}</th>}
          <th className={th}>{t('events.status')}</th>
          <th className={th}>{t('events.message')}</th>
        </tr>
      </thead>
      <tbody>
        {events.map((ev) => (
          <tr key={ev.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
            <td className={`${td} whitespace-nowrap tabular-nums`} title={formatRelative(ev.time)}>
              {formatDateTime(ev.time)}
            </td>
            {showKind && (
              <td className={td}>
                <EventKindLabel kind={ev.kind} />
              </td>
            )}
            {showSource && (
              <td className={td}>
                <EventSource ev={ev} />
              </td>
            )}
            <td className={td}>
              <StatusTransition ev={ev} />
            </td>
            <td dir="auto" className={`${td} min-w-48 text-slate-600 dark:text-slate-300`}>
              {ev.in_maintenance && (
                <span
                  className="me-1 inline-flex align-[-2px] text-sky-600 dark:text-sky-400"
                  title={t('events.inMaintenance')}
                  aria-label={t('events.inMaintenance')}
                >
                  <WrenchIcon size={13} />
                </span>
              )}
              {ev.device_id !== null && ev.new_status === 'unreachable' ? (
                <StatusMessage message={ev.message} status="unreachable" deviceId={ev.device_id} />
              ) : (
                ev.message
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

export function EventSource({ ev }: { ev: NmEvent }) {
  const { t } = useTranslation();
  if (!ev.device_name && !ev.sensor_name)
    return <span className="text-slate-400">{t('events.system')}</span>;
  return (
    <span className="whitespace-nowrap">
      {ev.device_id && ev.device_name ? (
        <Link
          className="text-sky-700 hover:underline dark:text-sky-400"
          to={`/devices/${ev.device_id}`}
        >
          {ev.device_name}
        </Link>
      ) : (
        ev.device_name
      )}
      {ev.sensor_name && (
        <>
          <span className="mx-1 text-slate-400">/</span>
          {ev.sensor_id ? (
            <Link
              className="text-sky-700 hover:underline dark:text-sky-400"
              to={`/sensors/${ev.sensor_id}`}
            >
              {ev.sensor_name}
            </Link>
          ) : (
            ev.sensor_name
          )}
        </>
      )}
    </span>
  );
}
