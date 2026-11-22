import { CHECKBOX, SeverityTag } from './kit';
import { useFeature } from '../hooks/features';
import { useMemo, type ReactNode } from 'react';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import type { Sensor } from '../api/types';
import { formatRelative, sensorValueText } from '../lib/format';
import { StatusBadge } from './Status';
import { AckBadge } from './Acknowledge';
import { StatusMessage } from './Dependency';
import { useLocalizeMessage } from '../hooks/localize';
import { MaintenanceBadge } from './Maintenance';
import { ImpactTag } from './SensorImpact';
import { cx, Empty, td, th, Value } from './ui';
import {
  CELL_PINNED_END,
  CELL_TRUNCATE,
  ColumnHeader,
  ColumnTable,
  useColumnLayout,
  type ColumnLayout,
  type ColumnSpec,
} from './kitTable';
import { useEntityMenu } from './treemenu/TreeContextMenu';

const boxCls = CHECKBOX;

/** [1.3] Row selection (checkboxes + select all) for bulk actions. */
export type SensorSelection = {
  selected: ReadonlySet<number>;
  onChange: (next: Set<number>) => void;
};

export function primaryChannel(s: Sensor) {
  return s.channels.find((c) => c.key === s.primary_channel) ?? s.channels[0];
}

type SensorCol = ColumnSpec & { label: string };

/** Width of the actions column: room for "Acknowledge" when a row offers it. */
export function sensorActionsWidth(sensors: Sensor[]): number {
  return sensors.some((s) => s.acknowledgement || s.status === 'down' || s.status === 'warning')
    ? 246
    : 112;
}

/** Columns of the sensor table (order and widths remembered per browser: `netmon.table.sensors`). */
export function useSensorColumns({
  sensors,
  showDevice,
  selectable,
  withActions,
}: {
  sensors: Sensor[];
  showDevice?: boolean;
  selectable?: boolean;
  withActions?: boolean;
}): ColumnLayout<SensorCol> {
  const { t } = useTranslation();
  const actionsW = withActions ? sensorActionsWidth(sensors) : 0;
  const severityOn = useFeature('alert_severity');
  const specs = useMemo<SensorCol[]>(
    () =>
      (
        [
          selectable && {
            id: 'select',
            pin: 'start' as const,
            fixed: true,
            width: 52,
            label: t('tableCols.select'),
          },
          { id: 'status', width: 122, minWidth: 90, label: t('sensors.status') },
          // [severity] the current alert's severity (only while the feature is on)
          severityOn && { id: 'severity', width: 120, minWidth: 90, label: t('severity.label') },
          showDevice && { id: 'device', width: 170, label: t('sensors.device') },
          { id: 'name', width: 210, minWidth: 120, label: t('sensors.name') },
          { id: 'type', width: 124, label: t('sensors.type') },
          { id: 'value', width: 110, label: t('sensors.value') },
          { id: 'message', minWidth: 120, label: t('sensors.message') },
          { id: 'lastCheck', width: 140, label: t('sensors.lastCheck') },
          withActions && {
            id: 'actions',
            pin: 'end' as const,
            fixed: true,
            width: actionsW,
            label: t('tableCols.actions'),
          },
        ] as (SensorCol | false | undefined)[]
      ).filter((c): c is SensorCol => !!c),
    [t, selectable, showDevice, withActions, actionsW, severityOn],
  );
  return useColumnLayout('sensors', specs);
}

export function SensorTable({
  sensors,
  showDevice,
  actions,
  selection,
  layout: given,
}: {
  sensors: Sensor[];
  showDevice?: boolean;
  actions?: (s: Sensor) => React.ReactNode;
  selection?: SensorSelection;
  /** the column layout shared with a "Columns" popover outside the card */
  layout?: ColumnLayout<SensorCol>;
}) {
  const { t } = useTranslation();
  const localize = useLocalizeMessage();
  const menu = useEntityMenu();
  const own = useSensorColumns({
    sensors,
    showDevice,
    selectable: !!selection,
    withActions: !!actions,
  });
  const layout = given ?? own;
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
  const cols = layout.columns;
  const head = (c: SensorCol): ReactNode => {
    if (c.id === 'select')
      return (
        <th key={c.id} data-col={c.id} className={`${th} w-8`}>
          <input
            type="checkbox"
            className={boxCls}
            checked={all}
            ref={(el) => {
              if (el) el.indeterminate = nSel > 0 && !all;
            }}
            onChange={(e) =>
              selection?.onChange(
                e.target.checked ? new Set(sensors.map((s) => s.id)) : new Set<number>(),
              )
            }
            aria-label={t('common.selectAll')}
            data-testid="sensor-select-all"
          />
        </th>
      );
    if (c.id === 'actions')
      return (
        <th key={c.id} data-col={c.id} className={cx(th, CELL_PINNED_END)}>
          <span className="sr-only">{c.label}</span>
        </th>
      );
    return <ColumnHeader key={c.id} layout={layout} col={c} label={c.label} className={th} />;
  };
  const cell = (c: SensorCol, s: Sensor): ReactNode => {
    const pc = primaryChannel(s);
    switch (c.id) {
      case 'select':
        return (
          <td key={c.id} className={`${td} w-8`}>
            <input
              type="checkbox"
              className={boxCls}
              checked={!!sel?.has(s.id)}
              onChange={(e) => toggle(s.id, e.target.checked)}
              aria-label={t('impact.selectSensor', { name: s.name })}
              data-testid="sensor-select"
            />
          </td>
        );
      case 'status':
        return (
          <td key={c.id} className={cx(td, 'overflow-hidden')}>
            <span className="inline-flex items-center gap-1">
              <StatusBadge status={s.status} />
              {s.acknowledgement && <AckBadge ack={s.acknowledgement} compact />}
              {s.in_maintenance && <MaintenanceBadge compact />}
            </span>
          </td>
        );
      case 'severity':
        return (
          <td key={c.id} className={cx(td, 'overflow-hidden')} data-testid="sensor-severity-cell">
            <SeverityTag severity={s.severity} />
          </td>
        );
      case 'device':
        return (
          <td key={c.id} className={cx(td, CELL_TRUNCATE)} title={s.device_name}>
            <Link
              {...menu({ kind: 'device', id: s.device_id })}
              className="hover:underline"
              to={`/devices/${s.device_id}`}
            >
              {s.device_name}
            </Link>
          </td>
        );
      case 'name':
        return (
          <td key={c.id} className={cx(td, CELL_TRUNCATE)} title={s.name}>
            <Link className="font-medium text-accent-ink hover:underline" to={`/sensors/${s.id}`}>
              {s.name}
            </Link>
            <ImpactTag impact={s.impact} />
          </td>
        );
      case 'type':
        return (
          <td key={c.id} className={cx(td, CELL_TRUNCATE, 'text-ink-muted')}>
            {t(`sensorType.${s.type}`)}
          </td>
        );
      case 'value':
        return (
          <td key={c.id} className={cx(td, CELL_TRUNCATE, 'tabular-nums')}>
            {pc ? <Value text={sensorValueText(s, pc.key, pc.last_value, pc.unit)} /> : '—'}
          </td>
        );
      case 'message':
        return (
          <td
            key={c.id}
            dir="auto"
            className={cx(td, CELL_TRUNCATE, 'text-ink-muted')}
            title={localize(s.status_message)}
            data-testid="sensor-message"
          >
            <StatusMessage message={s.status_message} status={s.status} deviceId={s.device_id} />
          </td>
        );
      case 'lastCheck':
        return (
          <td key={c.id} className={cx(td, CELL_TRUNCATE, 'text-ink-muted')}>
            {formatRelative(s.last_check_at)}
          </td>
        );
      case 'actions':
        return (
          <td key={c.id} className={cx(td, CELL_PINNED_END)} data-testid="sensor-actions">
            {actions?.(s)}
          </td>
        );
    }
    return null;
  };
  return (
    <ColumnTable layout={layout} columns={cols} testId="sensor-table">
      <thead>
        <tr>{cols.map(head)}</tr>
      </thead>
      <tbody>
        {sensors.map((s) => (
          <tr
            key={s.id}
            {...menu({ kind: 'sensor', id: s.id })}
            className={
              sel?.has(s.id)
                ? '[&>td]:bg-[color-mix(in_srgb,var(--accent)_8%,var(--surface))]'
                : undefined
            }
          >
            {cols.map((c) => cell(c, s))}
          </tr>
        ))}
      </tbody>
    </ColumnTable>
  );
}
