import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { EventCondition, Sensor } from '../api/types';
import { useAuth } from '../hooks/auth';
import { errorMessage } from '../lib/errors';
import { formatDateTime, formatRelative } from '../lib/format';
import { StatusBadge } from './Status';
import { useToast } from './toast';
import { Button, Card, cx, Empty, ErrorBox, Loading, Table, td, th } from './ui';

/** [2b-3] Passive event sensors (trap/syslog/port_flap) have no measurements or polling. */
export function isEventSensor(s: Pick<Sensor, 'type'>): boolean {
  return (
    s.type === 'trap' ||
    s.type === 'syslog' ||
    s.type === 'port_flap' ||
    s.type === 'config_backup' || // [3]
    s.type === 'new_mac'
  );
}

export function FlappingBadge() {
  const { t } = useTranslation();
  return (
    <span className="inline-flex items-center gap-1 rounded bg-status-warning-soft px-1.5 py-0.5 text-[11px] font-semibold text-status-warning">
      <span className="h-1.5 w-1.5 rounded-full bg-status-warning" />
      {t('flapping.badge')}
    </span>
  );
}

function ConditionRow({ c }: { c: EventCondition }) {
  const { t } = useTranslation();
  return (
    <tr>
      <td className={td}>
        <StatusBadge status={c.status} />
      </td>
      <td className={td}>
        {c.source === 'flap' ? (
          <FlappingBadge />
        ) : c.source !== 'rule' ? (
          <span className="font-medium">{t(`conditions.source.${c.source}`)}</span>
        ) : (
          <span className="font-medium">{c.rule_name ?? `#${c.rule_id}`}</span>
        )}
        {c.key && (
          <span className="ms-1.5 text-xs text-ink-muted">
            {c.source === 'flap'
              ? t('conditions.ifIndex', { key: c.key })
              : t('conditions.key', { key: c.key })}
          </span>
        )}
      </td>
      <td className={td}>
        <bdi dir="auto">{c.message}</bdi>
      </td>
      <td className={cx(td, 'tabular-nums')}>
        {c.source === 'flap' ? t('conditions.transitions', { n: c.count }) : c.count}
      </td>
      <td className={cx(td, 'whitespace-nowrap text-xs')} title={formatDateTime(c.first_at)}>
        {formatRelative(c.first_at)}
      </td>
      <td className={cx(td, 'whitespace-nowrap text-xs')} title={formatDateTime(c.last_at)}>
        {formatRelative(c.last_at)}
      </td>
      <td
        className={cx(td, 'whitespace-nowrap text-xs text-ink-muted')}
        title={formatDateTime(c.expires_at)}
      >
        {c.expires_at ? formatRelative(c.expires_at) : t('conditions.noExpiry')}
      </td>
    </tr>
  );
}

/** "Active conditions" of an event sensor, with the admin Clear button (instead of a chart). */
export function EventConditionsCard({ sensor }: { sensor: Sensor }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({
    queryKey: qk.conditions(sensor.id),
    queryFn: () => endpoints.conditions(sensor.id),
  });
  const clear = useMutation({
    mutationFn: () => endpoints.clearConditions(sensor.id),
    onSuccess: (s) => {
      qc.setQueryData(qk.sensor(s.id), s);
      void qc.invalidateQueries({ queryKey: qk.conditions(sensor.id) });
      void qc.invalidateQueries({ queryKey: ['sensors'] });
      toast.push('success', t('conditions.cleared'));
    },
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  const items = q.data?.items ?? [];
  return (
    <Card
      title={t('conditions.title')}
      bodyClassName="p-0"
      actions={
        isAdmin && (
          <Button
            size="sm"
            busy={clear.isPending}
            disabled={!items.length}
            onClick={() => clear.mutate()}
          >
            {t('conditions.clear')}
          </Button>
        )
      }
    >
      <p className="px-4 py-2 text-xs text-ink-muted">
        {t(`conditions.intro.${sensor.type}`, { defaultValue: '' }) || t('conditions.introGeneric')}
      </p>
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} className="m-3" />
      {q.data && !items.length && <Empty>{t('conditions.none')}</Empty>}
      {items.length > 0 && (
        <Table>
          <thead>
            <tr>
              <th className={th}>{t('common.status')}</th>
              <th className={th}>{t('conditions.rule')}</th>
              <th className={th}>{t('events.message')}</th>
              <th className={th}>{t('conditions.count')}</th>
              <th className={th}>{t('conditions.first')}</th>
              <th className={th}>{t('conditions.last')}</th>
              <th className={th}>{t('conditions.expires')}</th>
            </tr>
          </thead>
          <tbody>
            {items.map((c) => (
              <ConditionRow key={c.id} c={c} />
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}
