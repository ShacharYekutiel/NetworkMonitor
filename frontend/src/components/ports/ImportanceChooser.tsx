import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../../api/endpoints';
import type { Port } from '../../api/types';
import { useAuth } from '../../hooks/auth';
import { errorMessage } from '../../lib/errors';
import { useToast } from '../toast';
import { cx } from '../ui';

type Choice = 'auto' | 'important' | 'not';

/**
 * [if-noise] "Important port" in the port card: automatic (the Settings criteria, with why) or
 * the user's explicit choice; PUT …/ports/{if_index}/importance. Admins only; nothing is shown on
 * servers without the field.
 */
export function ImportanceChooser({ deviceId, port }: { deviceId: number; port: Port }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const toast = useToast();
  const set = useMutation({
    mutationFn: (v: boolean | null) => endpoints.setPortImportance(deviceId, port.if_index, v),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.devicePorts(deviceId) });
      void qc.invalidateQueries({ queryKey: ['port-detail', deviceId] });
      toast.push('success', t('portImportance.saved'));
    },
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  if (!isAdmin || port.importance_override === undefined) return null;
  const value: Choice =
    port.importance_override === true
      ? 'important'
      : port.importance_override === false
        ? 'not'
        : 'auto';
  const reasons = (port.important_reasons ?? []).filter((r) => r !== 'marked');
  const auto =
    value === 'auto'
      ? port.important
        ? t('portImportance.autoImportant', {
            reasons: reasons.map((r) => t(`applyPorts.reason.${r}`)).join(', '),
          })
        : t('portImportance.autoNot')
      : '';
  const options: { v: Choice; label: string }[] = [
    { v: 'auto', label: t('portImportance.auto') },
    { v: 'important', label: t('portImportance.important') },
    { v: 'not', label: t('portImportance.not') },
  ];
  return (
    <section className="grid gap-2 border-t border-line pt-3.5" data-testid="importance-chooser">
      <h4 className="m-0 text-[13.5px] font-normal text-ink-muted">{t('portImportance.title')}</h4>
      <div
        role="radiogroup"
        aria-label={t('portImportance.title')}
        className="grid grid-cols-3 gap-1.5"
      >
        {options.map((o) => {
          const on = value === o.v;
          return (
            <button
              key={o.v}
              type="button"
              role="radio"
              aria-checked={on}
              disabled={set.isPending}
              onClick={() =>
                !on && set.mutate(o.v === 'auto' ? null : o.v === 'important' ? true : false)
              }
              className={cx(
                'flex h-10 items-center justify-center gap-2 rounded-md px-2.5 text-[13px] font-medium transition-colors',
                on ? 'bg-accent text-accent-on' : 'bg-chip text-ink hover:bg-surface-hover',
              )}
            >
              {o.label}
            </button>
          );
        })}
      </div>
      {auto && <span className="text-[12.5px] text-ink-muted">{auto}</span>}
      <span className="text-[12.5px] text-ink-subtle">{t('portImportance.hint')}</span>
    </section>
  );
}
