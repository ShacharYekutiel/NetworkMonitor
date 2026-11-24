import { useMemo } from 'react';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import type { NmEvent } from '../api/types';
import { formatDateTime, formatRelative } from '../lib/format';
import { StatusBadge } from './Status';
import { ChevronIcon, WrenchIcon } from './icons';
import { StatusMessage } from './Dependency';
import { useLocalizeMessage } from '../hooks/localize';
import { cx, Empty, GroupList, td, th } from './ui';
import {
  CELL_PINNED_END,
  CELL_TRUNCATE,
  ColumnHeader,
  ColumnTable,
  useColumnLayout,
  type ColumnSpec,
} from './kitTable';
import { useAiFeature } from './assistant/common';
import { STATUS_DOT, statusClass } from '../lib/status';
import { ExplainButton } from './assistant/ExplainButton';
import { useFeature } from '../hooks/features';
import { SeverityTag } from './kit';
import { useEntityMenu, type EntityRef } from './treemenu/TreeContextMenu';

export function EventKindLabel({ kind }: { kind: NmEvent['kind'] }) {
  const { t } = useTranslation();
  return (
    <span className="inline-flex h-[22px] items-center whitespace-nowrap rounded-full bg-chip px-2 text-xs font-medium text-ink-muted">
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

type EventCol = ColumnSpec & { label: string };

/** Problem transitions the AI can explain (the Explain button). */
function explainableEvent(ev: NmEvent): boolean {
  return (
    ev.kind === 'status_change' &&
    ev.sensor_id !== null &&
    (ev.new_status === 'down' || ev.new_status === 'warning' || ev.new_status === 'unreachable')
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
  const message = useEventMessage();
  const severityOn = useFeature('alert_severity'); // [severity]
  const menu = useEntityMenu();
  const aiOn = useAiFeature('ai_explain_alert');
  const withActions = aiOn && events.some(explainableEvent);
  const specs = useMemo<EventCol[]>(
    () =>
      (
        [
          { id: 'time', width: 206, label: t('events.time') },
          showKind && { id: 'kind', width: 140, label: t('events.kind') },
          showSource && { id: 'source', width: 230, label: t('events.source') },
          { id: 'status', width: 200, label: t('events.status') },
          severityOn && { id: 'severity', width: 128, label: t('severity.label') },
          { id: 'message', minWidth: 160, label: t('events.message') },
          withActions && {
            id: 'actions',
            pin: 'end' as const,
            fixed: true,
            width: 132,
            label: t('tableCols.actions'),
          },
        ] as (EventCol | false)[]
      ).filter((c): c is EventCol => !!c),
    [t, showKind, showSource, withActions, severityOn],
  );
  const layout = useColumnLayout('events', specs);
  if (!events.length) return <Empty>{t('events.none')}</Empty>;
  const cols = layout.columns;
  const cell = (c: EventCol, ev: NmEvent) => {
    switch (c.id) {
      case 'time':
        return (
          <td
            key={c.id}
            className={cx(td, CELL_TRUNCATE, 'font-mono text-[13px] tabular-nums text-ink-subtle')}
            title={formatRelative(ev.time)}
          >
            {formatDateTime(ev.time)}
          </td>
        );
      case 'kind':
        return (
          <td key={c.id} className={cx(td, 'overflow-hidden')}>
            <EventKindLabel kind={ev.kind} />
          </td>
        );
      case 'source':
        return (
          <td
            key={c.id}
            className={cx(td, CELL_TRUNCATE)}
            title={[ev.device_name, ev.sensor_name].filter(Boolean).join(' / ') || undefined}
          >
            <EventSource ev={ev} />
          </td>
        );
      case 'status':
        return (
          <td key={c.id} className={cx(td, 'overflow-hidden')}>
            <StatusTransition ev={ev} />
          </td>
        );
      case 'severity':
        return (
          <td key={c.id} className={cx(td, 'overflow-hidden')}>
            <SeverityTag severity={ev.severity} />
          </td>
        );
      case 'message':
        return (
          <td
            key={c.id}
            dir="auto"
            className={cx(td, CELL_TRUNCATE, 'text-ink-muted')}
            title={message(ev)}
          >
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
              message(ev)
            )}
          </td>
        );
      case 'actions':
        return (
          <td key={c.id} className={cx(td, CELL_PINNED_END)}>
            {/* [3b] Explain (AI) on problem transitions */}
            {explainableEvent(ev) && <ExplainButton sensorId={ev.sensor_id!} eventId={ev.id} />}
          </td>
        );
    }
    return null;
  };
  return (
    <ColumnTable layout={layout} columns={cols}>
      <thead>
        <tr>
          {cols.map((c) =>
            c.id === 'actions' ? (
              <th key={c.id} data-col={c.id} className={cx(th, CELL_PINNED_END)}>
                <span className="sr-only">{c.label}</span>
              </th>
            ) : (
              <ColumnHeader key={c.id} layout={layout} col={c} label={c.label} className={th} />
            ),
          )}
        </tr>
      </thead>
      <tbody>
        {events.map((ev) => (
          <tr key={ev.id} {...menu(eventEntity(ev))}>
            {cols.map((c) => cell(c, ev))}
          </tr>
        ))}
      </tbody>
    </ColumnTable>
  );
}

/** The sensor (else the device) an event is about, for the context menu of its row. */
export function eventEntity(ev: NmEvent): EntityRef | null {
  if (ev.sensor_id) return { kind: 'sensor', id: ev.sensor_id };
  if (ev.device_id) return { kind: 'device', id: ev.device_id };
  return null;
}

export function EventSource({ ev }: { ev: NmEvent }) {
  const { t } = useTranslation();
  const menu = useEntityMenu();
  if (!ev.device_name && !ev.sensor_name)
    return <span className="text-ink-subtle">{t('events.system')}</span>;
  return (
    <span className="whitespace-nowrap">
      {ev.device_id && ev.device_name ? (
        <Link
          {...menu({ kind: 'device', id: ev.device_id })}
          className="text-ink hover:underline"
          to={`/devices/${ev.device_id}`}
        >
          {ev.device_name}
        </Link>
      ) : (
        ev.device_name
      )}
      {ev.sensor_name && (
        <>
          <span className="mx-1 text-ink-subtle">/</span>
          {ev.sensor_id ? (
            <Link
              {...menu({ kind: 'sensor', id: ev.sensor_id })}
              className="text-accent-ink hover:underline"
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

/** [if-noise] The message of an event; a flapping row is described in the UI language. */
export function useEventMessage() {
  const { t } = useTranslation();
  const localize = useLocalizeMessage();
  return (ev: NmEvent): string => {
    if (ev.kind !== 'sensor_flapping') return localize(ev.message);
    const status = ev.new_status ? t(`status.${ev.new_status}`) : '';
    return t(ev.flap_end ? 'noise.flapEnded' : 'noise.flapOngoing', {
      count: ev.flap_count ?? 0,
      status,
    });
  };
}

/** "Up → Down" in words (he: "תקין ← תקלה"); the kind for events without a status change. */
export function useEventTitle() {
  const { t } = useTranslation();
  return (ev: NmEvent): string => {
    if (ev.kind === 'sensor_flapping') return t('noise.flapTitle', { count: ev.flap_count ?? 0 });
    if (!ev.new_status) return t(`eventKind.${ev.kind}`, { defaultValue: ev.kind });
    if (!ev.old_status || ev.old_status === ev.new_status) return t(`status.${ev.new_status}`);
    return t('events.transition', {
      from: t(`status.${ev.old_status}`),
      to: t(`status.${ev.new_status}`),
    });
  };
}

/**
 * The events of one sensor as a grouped list (prototype sensor page "אירועים"): a status dot, the
 * transition in words, the message and when.
 */
export function EventRows({ events, label }: { events: NmEvent[]; label?: string }) {
  const { t } = useTranslation();
  const menu = useEntityMenu();
  const message = useEventMessage();
  const title = useEventTitle();
  if (!events.length)
    return (
      <GroupList label={label}>
        <li>
          <Empty>{t('events.none')}</Empty>
        </li>
      </GroupList>
    );
  return (
    <GroupList label={label} className="[&>li+li]:before:start-[46px]">
      {events.map((ev) => (
        <li
          key={ev.id}
          {...menu(eventEntity(ev))}
          data-testid="event-row"
          className="flex items-start gap-3.5 px-5 py-3.5"
        >
          <span
            aria-hidden="true"
            className={cx(
              'mt-[7px] h-2.5 w-2.5 shrink-0 rounded-full',
              ev.new_status ? statusClass(ev.new_status, STATUS_DOT) : 'bg-ink-subtle',
            )}
          />
          <div className="grid min-w-0 flex-1 gap-0.5">
            <b className="font-medium">{title(ev)}</b>
            <span dir="auto" className="min-w-0 break-words text-[13.5px] text-ink-muted">
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
                message(ev)
              )}
              {' · '}
              <time dateTime={ev.time} title={formatDateTime(ev.time)}>
                {formatRelative(ev.time)}
              </time>
            </span>
          </div>
          {explainableEvent(ev) && <ExplainButton sensorId={ev.sensor_id!} eventId={ev.id} />}
        </li>
      ))}
    </GroupList>
  );
}
