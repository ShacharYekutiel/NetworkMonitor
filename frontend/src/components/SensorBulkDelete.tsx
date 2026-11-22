// Bulk "Delete" of the sensor bulk bar: POST /api/sensors/bulk-delete after a confirmation that
// names every sensor and says their history goes with them (as the single delete does).
import { useEffect } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { Sensor } from '../api/types';
import { useToast } from './toast';
import { Button, ErrorBox, Modal } from './ui';

export function BulkDeleteModal({
  open,
  onClose,
  sensors,
  deviceId,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  sensors: Pick<Sensor, 'id' | 'name'>[];
  deviceId?: number;
  onDone?: () => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const count = sensors.length;
  const m = useMutation({
    mutationFn: () => endpoints.bulkDeleteSensors(sensors.map((s) => s.id)),
    onSuccess: (r) => {
      for (const s of sensors) qc.removeQueries({ queryKey: qk.sensor(s.id) });
      void qc.invalidateQueries({ queryKey: ['sensors'] });
      void qc.invalidateQueries({ queryKey: qk.tree });
      void qc.invalidateQueries({ queryKey: qk.dashboard });
      if (deviceId !== undefined) void qc.invalidateQueries({ queryKey: qk.device(deviceId) });
      toast.push('success', t('sensorBulk.deleted', { count: r.deleted }));
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
      title={t('sensorBulk.deleteTitle', { count })}
      testId="bulk-delete"
      footer={
        <>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button
            variant="danger"
            busy={m.isPending}
            disabled={!count}
            onClick={() => m.mutate()}
            data-testid="bulk-delete-confirm"
          >
            {t('sensorBulk.deleteButton', { count })}
          </Button>
        </>
      }
    >
      <div className="grid gap-3">
        <p className="m-0 text-[14.5px]">{t('sensorBulk.deleteConfirm', { count })}</p>
        <ul
          aria-label={t('sensorBulk.listLabel')}
          data-testid="bulk-delete-list"
          // a long selection scrolls inside the sheet
          className="m-0 grid max-h-[min(40vh,320px)] list-none gap-px overflow-y-auto rounded-lg bg-chip p-1.5"
        >
          {sensors.map((s) => (
            <li
              key={s.id}
              dir="auto"
              title={s.name}
              className="truncate rounded-[8px] px-2.5 py-1.5 text-[14px] font-medium"
            >
              {s.name}
            </li>
          ))}
        </ul>
        <ErrorBox error={m.error} />
      </div>
    </Modal>
  );
}
