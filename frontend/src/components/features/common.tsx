import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../../api/endpoints';
import type { FeatureName, Settings, SettingsInput } from '../../api/types';
import { useFeatures, FeatureOffNotice } from '../../hooks/features';
import { useToast } from '../toast';
import { cx } from '../ui';

/** PUT /api/settings (partial merge) + cache update. */
export function useSaveSettings(opts: { toast?: boolean; onSaved?: (s: Settings) => void } = {}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: (body: SettingsInput) => endpoints.updateSettings(body),
    onSuccess: (s) => {
      qc.setQueryData(qk.settings, s);
      void qc.invalidateQueries({ queryKey: qk.publicSettings });
      if (opts.toast !== false) toast.push('success', t('common.saved'));
      opts.onSaved?.(s);
    },
  });
}

/** An accessible on/off switch. */
export function Toggle({
  checked,
  onChange,
  label,
  disabled,
  busy,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  disabled?: boolean;
  busy?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled || busy}
      onClick={() => onChange(!checked)}
      className={cx(
        'relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 disabled:cursor-not-allowed disabled:opacity-60',
        checked ? 'bg-green-600' : 'bg-slate-300 dark:bg-slate-600',
        busy && 'animate-pulse',
      )}
    >
      <span
        className={cx(
          'inline-block h-4 w-4 rounded-full bg-white shadow transition-transform',
          checked
            ? 'translate-x-[18px] rtl:-translate-x-[18px]'
            : 'translate-x-0.5 rtl:-translate-x-0.5',
        )}
      />
    </button>
  );
}

/**
 * A settings section of a feature: while the feature is OFF it is shown greyed with the one-line
 * explanation; it stays editable so an admin can prepare it before turning the feature on.
 */
export function FeatureSection({
  feature,
  children,
}: {
  feature: FeatureName | FeatureName[];
  children: React.ReactNode;
}) {
  const { features } = useFeatures();
  const names = Array.isArray(feature) ? feature : [feature];
  const on = names.some((n) => features[n]);
  return (
    <div className="space-y-3">
      {!on && <FeatureOffNotice feature={feature} />}
      <div className={cx(!on && 'opacity-70')} data-feature-off={!on || undefined}>
        {children}
      </div>
    </div>
  );
}
