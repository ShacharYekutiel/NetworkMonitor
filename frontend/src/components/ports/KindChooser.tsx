import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { qk } from '../../api/endpoints';
import { premium } from '../../api/premium';
import { CONNECTED_KINDS, type ConnectedKind, type Port } from '../../api/types';
import { useAuth } from '../../hooks/auth';
import { errorMessage } from '../../lib/errors';
import { portDeviceKind } from '../../lib/ports';
import { useToast } from '../toast';
import { cx } from '../ui';
import { KIND_ICONS } from './Faceplate';

/** The 8 kinds offered by the chooser (prototype KOPTS); "unknown" is only ever detected. */
const CHOICES = CONNECTED_KINDS.filter((k) => k !== 'unknown');

/**
 * [premium] "Connected device type" at the end of the port card (BUILD.md § 6b): Automatic (with
 * what was detected) or one of 8 kinds; PUT …/ports/{if_index}/connected-kind. Admins only; the
 * server must know the field (premium), else nothing is shown.
 */
export function KindChooser({ deviceId, port }: { deviceId: number; port: Port }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const toast = useToast();
  const set = useMutation({
    mutationFn: (kind: ConnectedKind | null) => premium.setPortKind(deviceId, port.if_index, kind),
    onSuccess: (_r, kind) => {
      void qc.invalidateQueries({ queryKey: qk.devicePorts(deviceId) });
      void qc.invalidateQueries({ queryKey: ['port-detail', deviceId] });
      toast.push(
        'success',
        kind
          ? t('ports.kindChooser.saved', { kind: t(`ports.kindShort.${kind}`) })
          : t('ports.kindChooser.savedAuto'),
      );
    },
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  if (!isAdmin || port.connected_kind_override === undefined) return null;
  const manual = port.connected_kind_override ?? null;
  const detected = manual ? null : portDeviceKind(port);
  const src = port.connected_kind_source;
  return (
    <section className="grid gap-2 border-t border-line pt-3.5" data-testid="kind-chooser">
      <h4 className="m-0 text-[13.5px] font-normal text-ink-muted">
        {t('ports.kindChooser.title')}
      </h4>
      <div
        role="radiogroup"
        aria-label={t('ports.kindChooser.title')}
        className="grid grid-cols-3 gap-1.5"
      >
        <button
          type="button"
          role="radio"
          aria-checked={!manual}
          disabled={set.isPending}
          onClick={() => manual && set.mutate(null)}
          className={cx(
            'col-span-3 flex h-10 items-center justify-between gap-2 rounded-md px-3 text-[13px] font-medium transition-colors',
            !manual ? 'bg-accent text-accent-on' : 'bg-chip text-ink hover:bg-surface-hover',
          )}
        >
          {t('ports.kindChooser.auto')}
          <small className="text-xs opacity-70">
            {detected
              ? `${t('ports.kindChooser.detected', { kind: t(`ports.kindShort.${detected}`) })}${src && src !== 'manual' ? ` · ${t(`ports.kindSource.${src}`)}` : ''}`
              : manual
                ? ''
                : t('ports.kindChooser.nothing')}
          </small>
        </button>
        {CHOICES.map((k) => {
          const Icon = KIND_ICONS[k];
          const on = manual === k;
          return (
            <button
              key={k}
              type="button"
              role="radio"
              aria-checked={on}
              disabled={set.isPending}
              onClick={() => !on && set.mutate(k)}
              className={cx(
                'flex h-10 items-center gap-2 rounded-md px-2.5 text-[13px] font-medium transition-colors',
                on ? 'bg-accent text-accent-on' : 'bg-chip text-ink hover:bg-surface-hover',
              )}
            >
              <Icon size={16} strokeWidth={2} className="shrink-0" />
              <span className="truncate">{t(`ports.kindShort.${k}`)}</span>
            </button>
          );
        })}
      </div>
      <span className="text-[12.5px] text-ink-subtle">{t('ports.kindChooser.hint')}</span>
    </section>
  );
}
