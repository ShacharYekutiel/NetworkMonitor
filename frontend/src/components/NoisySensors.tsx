// [if-noise] Events page: the sensors with the most status changes (shown and hidden), so the
// noisy access ports can be found and excluded or re-applied without them.
import { useState } from 'react';
import { useAuth } from '../hooks/auth';
import { isOverridden } from '../lib/noiseOverride';
import { NoiseOverrideTag, SensorNoiseModal } from './noise/NoiseOverride';
import type { NoisySensor, SensorType } from '../api/types';
import { isEventSensor } from './EventConditions';
import { Link } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints } from '../api/endpoints';
import { StatusBadge } from './Status';
import {
  Button,
  Card,
  Empty,
  ErrorBox,
  Loading,
  SegmentedControl,
  TAG,
  TAG_TONE,
  cx,
  td,
  th,
} from './ui';

const RANGES = [24, 168, 720] as const;

export function NoisySensorsCard() {
  const { t } = useTranslation();
  const [hours, setHours] = useState<number>(24);
  const { isAdmin } = useAuth();
  const [editing, setEditing] = useState<NoisySensor | null>(null); // [if-noise] quick action
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
                    <th className={th}>{t('noiseOv.column')}</th>
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
                      <td className={td}>
                        <span className="inline-flex flex-wrap items-center gap-1.5">
                          {isOverridden(s.event_noise) && (
                            <NoiseOverrideTag
                              title={
                                s.effective_event_noise
                                  ? t('noiseOv.customTitle', {
                                      debounce: t(
                                        s.effective_event_noise.debounce.value
                                          ? 'noiseOv.value.on'
                                          : 'noiseOv.value.off',
                                      ),
                                      flap: t(
                                        s.effective_event_noise.flap.value
                                          ? 'noiseOv.value.on'
                                          : 'noiseOv.value.off',
                                      ),
                                    })
                                  : undefined
                              }
                            />
                          )}
                          {isAdmin && !isEventSensor({ type: s.sensor_type as SensorType }) && (
                            <Button
                              size="sm"
                              variant="ghost"
                              data-testid="noisy-protection"
                              aria-label={t('noiseOv.quickLabel', { name: s.sensor_name })}
                              onClick={() => setEditing(s)}
                            >
                              {t('noiseOv.quick')}
                            </Button>
                          )}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
      {editing && (
        <SensorNoiseModal
          open
          onClose={() => setEditing(null)}
          sensor={{
            id: editing.sensor_id,
            name: editing.sensor_name,
            device_id: editing.device_id,
            event_noise: editing.event_noise,
            effective_event_noise: editing.effective_event_noise,
          }}
        />
      )}
    </Card>
  );
}
