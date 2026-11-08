import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { Sensor } from '../api/types';
import { errorMessage } from '../lib/errors';
import { canRun } from '../lib/status';
import { PauseIcon, PlayIcon, RefreshIcon } from './icons';
import { useSensorActions } from './SensorForms';
import { useToast } from './toast';
import { Button } from './ui';
import { AckAction } from './Acknowledge';
import { isEventSensor } from './EventConditions';

export function useRunNow() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: (s: Sensor) => endpoints.runSensor(s.id),
    onSuccess: (r, s) => {
      void qc.invalidateQueries({ queryKey: qk.sensor(s.id) });
      void qc.invalidateQueries({ queryKey: ['history', s.id] });
      toast.push(
        r.ok ? 'success' : 'error',
        `${s.name}: ${t(`status.${r.status}`)}${r.message ? ` — ${r.message}` : ''}`,
      );
    },
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
}

export function SensorRowActions({ sensor }: { sensor: Sensor }) {
  const { t } = useTranslation();
  const run = useRunNow();
  const { toggle } = useSensorActions();
  return (
    <span className="inline-flex gap-1">
      {(sensor.acknowledgement || sensor.status === 'down' || sensor.status === 'warning') && (
        <AckAction sensor={sensor} />
      )}
      {!isEventSensor(sensor) && (
        <Button
          size="sm"
          variant="ghost"
          title={canRun(sensor) ? t('sensors.runNow') : t('sensors.runPausedHint')}
          aria-label={t('sensors.runNow')}
          busy={run.isPending && run.variables?.id === sensor.id}
          disabled={!canRun(sensor)}
          onClick={() => run.mutate(sensor)}
        >
          <RefreshIcon size={14} />
        </Button>
      )}
      <Button
        size="sm"
        variant="ghost"
        title={sensor.enabled ? t('sensors.pause') : t('sensors.resume')}
        aria-label={sensor.enabled ? t('sensors.pause') : t('sensors.resume')}
        busy={toggle.isPending && toggle.variables?.id === sensor.id}
        onClick={() => toggle.mutate(sensor)}
      >
        {sensor.enabled ? (
          <PauseIcon size={14} />
        ) : (
          <PlayIcon size={14} className="rtl:rotate-180" />
        )}
      </Button>
    </span>
  );
}
