// [2b-1] Acknowledge: badge, acknowledge dialog, unacknowledge (admin only).
import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { Acknowledgement, Dashboard, Sensor } from '../api/types';
import { useAuth } from '../hooks/auth';
import { errorMessage } from '../lib/errors';
import { formatDateTime } from '../lib/format';
import { applyAckToDashboard } from '../lib/liveCache';
import { canAcknowledge } from '../lib/status';
import { CheckIcon, XIcon } from './icons';
import { useToast } from './toast';
import { Button, ErrorBox, Field, Modal, Textarea } from './ui';

export function ackTooltip(
  t: (k: string, o?: Record<string, unknown>) => string,
  a: Acknowledgement,
): string {
  const base = t('ack.tooltip', { by: a.by ?? t('ack.unknownUser'), at: formatDateTime(a.at) });
  return a.comment ? `${base}: ${a.comment}` : base;
}

/** Check badge of an acknowledged sensor, with "by admin at 09:12: comment" as tooltip. */
export function AckBadge({ ack, compact }: { ack: Acknowledgement; compact?: boolean }) {
  const { t } = useTranslation();
  const title = ackTooltip(t, ack);
  if (compact)
    return (
      <span
        title={title}
        aria-label={title}
        className="inline-flex shrink-0 text-emerald-600 dark:text-emerald-400"
      >
        <CheckIcon size={13} />
      </span>
    );
  return (
    <span
      title={title}
      className="inline-flex items-center gap-1 whitespace-nowrap rounded bg-emerald-50 px-1.5 py-0.5 text-xs font-medium text-emerald-800 ring-1 ring-inset ring-emerald-200 dark:bg-emerald-950 dark:text-emerald-200 dark:ring-emerald-800"
    >
      <CheckIcon size={12} />
      {t('ack.acknowledged')}
    </span>
  );
}

/** Who/when/comment of an acknowledgement, for detail views. */
export function AckDetails({ ack }: { ack: Acknowledgement }) {
  const { t } = useTranslation();
  return (
    <span className="text-xs text-slate-600 dark:text-slate-300">
      {t('ack.by', { by: ack.by ?? t('ack.unknownUser'), at: formatDateTime(ack.at) })}
      {ack.comment && (
        <>
          {': '}
          <bdi dir="auto" className="italic">
            {ack.comment}
          </bdi>
        </>
      )}
    </span>
  );
}

function useAckMutations() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const apply = (s: Sensor) => {
    qc.setQueryData(qk.sensor(s.id), s);
    qc.setQueryData<Dashboard>(qk.dashboard, (old) =>
      old ? applyAckToDashboard(old, s.id, s.acknowledgement) : old,
    );
    void qc.invalidateQueries({ queryKey: ['sensors'] });
    void qc.invalidateQueries({ queryKey: qk.dashboard });
    void qc.invalidateQueries({ queryKey: qk.tree });
  };
  const ack = useMutation({
    mutationFn: ({ id, comment }: { id: number; comment: string }) =>
      endpoints.acknowledge(id, comment),
    onSuccess: (s) => {
      apply(s);
      toast.push('success', t('ack.done'));
    },
  });
  const unack = useMutation({
    mutationFn: (id: number) => endpoints.unacknowledge(id),
    onSuccess: apply,
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  return { ack, unack };
}

/**
 * Admin action for a problem sensor: "Acknowledge" (dialog with optional comment), or
 * "Unacknowledge" when it is already acknowledged. Renders nothing otherwise.
 */
export function AckAction({ sensor, size = 'sm' }: { sensor: Sensor; size?: 'sm' | 'md' }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const { ack, unack } = useAckMutations();
  const [open, setOpen] = useState(false);
  const [comment, setComment] = useState('');
  if (!isAdmin) return null;
  if (sensor.acknowledgement)
    return (
      <Button
        size={size}
        variant="ghost"
        busy={unack.isPending}
        onClick={() => unack.mutate(sensor.id)}
        title={t('ack.unack')}
      >
        <XIcon size={12} /> {t('ack.unack')}
      </Button>
    );
  if (!canAcknowledge(sensor)) return null;
  return (
    <>
      <Button
        size={size}
        variant={size === 'md' ? 'secondary' : 'ghost'}
        onClick={() => {
          setComment('');
          ack.reset();
          setOpen(true);
        }}
      >
        <CheckIcon size={12} /> {t('ack.action')}
      </Button>
      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title={t('ack.title', { name: `${sensor.device_name} / ${sensor.name}` })}
        footer={
          <>
            <Button onClick={() => setOpen(false)}>{t('common.cancel')}</Button>
            <Button
              variant="primary"
              busy={ack.isPending}
              onClick={() =>
                ack.mutate(
                  { id: sensor.id, comment: comment.trim() },
                  { onSuccess: () => setOpen(false) },
                )
              }
            >
              {t('ack.action')}
            </Button>
          </>
        }
      >
        <p className="mb-3 text-sm text-slate-600 dark:text-slate-300">{t('ack.explain')}</p>
        <Field label={t('ack.comment')} hint={t('ack.commentHint')}>
          <Textarea
            rows={3}
            maxLength={1000}
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            dir="auto"
            autoFocus
          />
        </Field>
        <ErrorBox className="mt-3" error={ack.error} />
      </Modal>
    </>
  );
}
