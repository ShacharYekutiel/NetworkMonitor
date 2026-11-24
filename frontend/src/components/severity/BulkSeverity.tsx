// [severity] Bulk "Set severity…" of the sensor bulk bar: POST /api/sensors/bulk-severity with the
// two overrides, each "Leave unchanged", "Automatic" (clears it) or a level.
import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { qk } from '../../api/endpoints';
import { severityApi, type BulkSeverityInput } from '../../api/severity';
import { useToast } from '../toast';
import { Button, ErrorBox, Field, Modal } from '../ui';
import { SeveritySelect, type SeverityChoice } from './Severity';

export function BulkSeverityModal({
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
  const [down, setDown] = useState<SeverityChoice>('keep');
  const [warning, setWarning] = useState<SeverityChoice>('keep');
  const count = sensorIds.length;
  const body: BulkSeverityInput = { sensor_ids: sensorIds };
  if (down !== 'keep') body.severity_down = down || null;
  if (warning !== 'keep') body.severity_warning = warning || null;
  const nothing = down === 'keep' && warning === 'keep';
  const m = useMutation({
    mutationFn: () => severityApi.bulkSeverity(body),
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ['sensors'] });
      void qc.invalidateQueries({ queryKey: ['sensor'] });
      void qc.invalidateQueries({ queryKey: qk.dashboard });
      if (deviceId !== undefined) void qc.invalidateQueries({ queryKey: qk.device(deviceId) });
      toast.push('success', t('severity.bulk.done', { count: r.updated }));
      onDone?.();
      onClose();
    },
  });
  useEffect(() => {
    if (!open) return;
    setDown('keep');
    setWarning('keep');
    m.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t('severity.bulk.title', { count })}
      testId="bulk-severity"
      footer={
        <>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button
            variant="primary"
            busy={m.isPending}
            disabled={nothing || !count}
            onClick={() => m.mutate()}
          >
            {t('impact.apply')}
          </Button>
        </>
      }
    >
      <div className="grid gap-3">
        <Field label={t('severity.whenDown')}>
          <SeveritySelect
            testId="bulk-severity-down"
            value={down}
            onChange={setDown}
            automatic={t('severity.automatic')}
            allowKeep
          />
        </Field>
        <Field label={t('severity.whenWarning')}>
          <SeveritySelect
            testId="bulk-severity-warning"
            value={warning}
            onChange={setWarning}
            automatic={t('severity.automatic')}
            allowKeep
          />
        </Field>
        <p className="m-0 text-xs text-ink-subtle">
          {nothing ? t('severity.bulk.pickOne') : t('severity.overrideHint')}
        </p>
        <ErrorBox error={m.error} />
      </div>
    </Modal>
  );
}
