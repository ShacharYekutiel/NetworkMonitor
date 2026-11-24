// [if-noise] Events page: the sensors with the most status changes (shown and hidden), so the
// noisy access ports can be found and excluded or re-applied without them.
import { useState } from 'react';
import { Link } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints } from '../api/endpoints';
import { StatusBadge } from './Status';
import { Card, Empty, ErrorBox, Loading, SegmentedControl, TAG, TAG_TONE, cx, td, th } from './ui';

const RANGES = [24, 168, 720] as const;

export function NoisySensorsCard() {
  const { t } = useTranslation();
  const [hours, setHours] = useState<number>(24);
  const q = useQuery({
    queryKey: ['noisy-sensors', hours],
    queryFn: () => endpoints.noisySensors(hours),
    staleTime: 30_000,
  });
  return (
    <Card
      title={t('noisy.title')}
      actions={
        <SegmentedControl
          size="sm"
          label={t('noisy.range')}
          value={String(hours)}
          onChange={(v) => setHours(Number(v))}
          options={RANGES.map((h) => ({ value: String(h), label: t(`noisy.hours.${h}`) }))}
        />
      }
    >
      <div data-testid="noisy-sensors" className="space-y-2">
        <p className="m-0 text-xs text-ink-muted">{t('noisy.intro')}</p>
        {q.isLoading && <Loading />}
        <ErrorBox error={q.error} />
        {q.data && !q.data.items.length && <Empty>{t('noisy.none')}</Empty>}
        {q.data && q.data.items.length > 0 && (
          <>
            <p className="m-0 text-xs text-ink-muted" data-testid="noisy-total">
              {t('noisy.total', { count: q.data.total_changes })}
            </p>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr>
                    <th className={th}>{t('noisy.sensor')}</th>
                    <th className={th}>{t('noisy.changes')}</th>
                    <th className={th}>{t('noisy.hidden')}</th>
                    <th className={th}>{t('noisy.share')}</th>
                    <th className={th}>{t('noisy.status')}</th>
                  </tr>
                </thead>
                <tbody>
                  {q.data.items.map((s) => (
                    <tr key={s.sensor_id} data-testid="noisy-row">
                      <td className={td}>
                        <Link
                          to={`/sensors/${s.sensor_id}`}
                          className="font-medium text-accent-ink hover:underline"
                        >
                          <bdi dir="auto">{s.sensor_name}</bdi>
                        </Link>
                        <span className="text-ink-muted"> · </span>
                        <Link
                          to={`/devices/${s.device_id}`}
                          className="text-ink-muted hover:underline"
                        >
                          <bdi dir="auto">{s.device_name}</bdi>
                        </Link>
                        {s.flapping && (
                          <span className={cx(TAG, TAG_TONE.warning, 'ms-2')}>
                            {t('noisy.flapping')}
                          </span>
                        )}
                      </td>
                      <td className={cx(td, 'tabular-nums')}>{s.changes}</td>
                      <td className={cx(td, 'tabular-nums text-ink-muted')}>{s.hidden}</td>
                      <td className={cx(td, 'tabular-nums text-ink-muted')}>
                        {q.data.total_changes
                          ? `${Math.round((100 * s.changes) / q.data.total_changes)}%`
                          : '–'}
                      </td>
                      <td className={td}>
                        <StatusBadge status={s.status} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </Card>
  );
}
