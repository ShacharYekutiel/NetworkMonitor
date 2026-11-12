// [1.3] Sensor impact on the device status: tag, form field and bulk "Set impact…" dialog.
import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import { SENSOR_IMPACTS, type SensorImpact } from '../api/types';
import { useToast } from './toast';
import { Button, cx, ErrorBox, FieldGroup, Modal } from './ui';

const DOT: Record<SensorImpact, string> = {
  critical: 'bg-red-600',
  warning: 'bg-amber-500',
  none: 'bg-gray-400',
};

/** Small tag next to a sensor name; critical is the default and stays unmarked. */
export function ImpactTag({ impact }: { impact: SensorImpact | undefined }) {
  const { t } = useTranslation();
  if (!impact || impact === 'critical') return null;
  return (
    <span
      data-testid="impact-tag"
      title={t('impact.tagTitle', { impact: t(`impact.option.${impact}`) })}
      className={cx(
        'ms-1.5 inline-flex items-center whitespace-nowrap rounded px-1 py-px align-middle text-[10px] font-semibold ring-1 ring-inset',
        impact === 'warning'
          ? 'bg-amber-50 text-amber-800 ring-amber-200 dark:bg-amber-950/50 dark:text-amber-300 dark:ring-amber-900'
          : 'bg-gray-100 text-gray-600 ring-gray-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700',
      )}
    >
      {t(`impact.tag.${impact}`)}
    </span>
  );
}

/** Radio group Critical / Warning / None with a one-line hint. */
export function ImpactField({
  value,
  onChange,
  name = 'impact',
}: {
  value: SensorImpact;
  onChange: (v: SensorImpact) => void;
  name?: string;
}) {
  const { t } = useTranslation();
  return (
    <FieldGroup label={t('impact.label')} hint={t(`impact.hint.${value}`)}>
      <div className="flex flex-wrap gap-x-4 gap-y-1.5" data-testid="impact-field">
        {SENSOR_IMPACTS.map((v) => (
          <label key={v} className="inline-flex cursor-pointer items-center gap-1.5 text-sm">
            <input
              type="radio"
              name={name}
              value={v}
              checked={value === v}
              onChange={() => onChange(v)}
              className="h-4 w-4 border-slate-300 text-sky-600 dark:border-slate-600 dark:bg-slate-900"
            />
            <span className={cx('h-2.5 w-2.5 rounded-full', DOT[v])} aria-hidden="true" />
            <span>{t(`impact.option.${v}`)}</span>
          </label>
        ))}
      </div>
    </FieldGroup>
  );
}

/** Bulk dialog: POST /api/sensors/bulk-impact for the selected sensors. */
export function BulkImpactModal({
  open,
  onClose,
  sensorIds,
  deviceId,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  sensorIds: number[];
  deviceId?: number;
  onDone?: () => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const [impact, setImpact] = useState<SensorImpact>('warning');
  const m = useMutation({
    mutationFn: () => endpoints.bulkSensorImpact(sensorIds, impact),
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ['sensors'] });
      void qc.invalidateQueries({ queryKey: qk.tree });
      if (deviceId !== undefined) void qc.invalidateQueries({ queryKey: qk.device(deviceId) });
      toast.push('success', t('impact.bulkDone', { count: r.updated }));
      onDone?.();
      onClose();
    },
  });
  useEffect(() => {
    if (open) m.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t('impact.bulkTitle', { count: sensorIds.length })}
      testId="bulk-impact"
      footer={
        <>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button
            variant="primary"
            busy={m.isPending}
            disabled={!sensorIds.length}
            onClick={() => m.mutate()}
          >
            {t('impact.apply')}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <ImpactField value={impact} onChange={setImpact} name="bulk-impact" />
        <ErrorBox error={m.error} />
      </div>
    </Modal>
  );
}
