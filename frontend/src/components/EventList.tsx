import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import type { NmEvent } from '../api/types';
import { formatDateTime, formatRelative } from '../lib/format';
import { StatusBadge } from './Status';
import { ChevronIcon, WrenchIcon } from './icons';
import { StatusMessage } from './Dependency';
import { useLocalizeMessage } from '../hooks/localize';
import { Empty, Table, td, th } from './ui';
import { ExplainButton } from './assistant/ExplainButton';

export function EventKindLabel({ kind }: { kind: NmEvent['kind'] }) {
  const { t } = useTranslation();
  return (
    <span className="inline-flex h-[22px] items-center whitespace-nowrap rounded-sm border border-line bg-surface-sunken px-2 text-xs font-medium text-ink-muted">
      {t(`eventKind.${kind}`, { defaultValue: kind })}
    </span>
  );
}

export function StatusTransition({ ev }: { ev: NmEvent }) {
  if (!ev.new_status) return null;
  return (
    <span className="inline-flex items-center gap-1">
      {ev.old_status && ev.old_status !== ev.new_status && <StatusBadge status={ev.old_status} />}
      {ev.old_status && ev.old_status !== ev.new_status && (
        <ChevronIcon size={12} className="text-ink-subtle rtl:rotate-180" />
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
  const localize = useLocalizeMessage();
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
          <tr key={ev.id} className="hover:bg-surface-hover">
            <td
              className={`${td} whitespace-nowrap font-mono text-[12.5px] tabular-nums text-ink-subtle`}
              title={formatRelative(ev.time)}
            >
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
            <td dir="auto" className={`${td} min-w-48 text-ink-muted`}>
              {ev.in_maintenance && (
                <span
                  className="me-1 inline-flex align-[-2px] text-accent-ink"
                  title={t('events.inMaintenance')}
                  aria-label={t('events.inMaintenance')}
                >
                  <WrenchIcon size={13} />
                </span>
              )}
              {ev.device_id !== null && ev.new_status === 'unreachable' ? (
                <StatusMessage message={ev.message} status="unreachable" deviceId={ev.device_id} />
              ) : (
                localize(ev.message)
              )}
              {/* [3b] Explain (AI) on problem transitions */}
              {ev.kind === 'status_change' &&
                ev.sensor_id !== null &&
                (ev.new_status === 'down' ||
                  ev.new_status === 'warning' ||
                  ev.new_status === 'unreachable') && (
                  <span className="ms-2 inline-block align-middle">
                    <ExplainButton sensorId={ev.sensor_id} eventId={ev.id} />
                  </span>
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
    return <span className="text-ink-subtle">{t('events.system')}</span>;
  return (
    <span className="whitespace-nowrap">
      {ev.device_id && ev.device_name ? (
        <Link className="text-ink hover:underline" to={`/devices/${ev.device_id}`}>
          {ev.device_name}
        </Link>
      ) : (
        ev.device_name
      )}
      {ev.sensor_name && (
        <>
          <span className="mx-1 text-ink-subtle">/</span>
          {ev.sensor_id ? (
            <Link className="text-accent-ink hover:underline" to={`/sensors/${ev.sensor_id}`}>
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
