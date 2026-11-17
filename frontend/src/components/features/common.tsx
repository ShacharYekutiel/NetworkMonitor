import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../../api/endpoints';
import type { FeatureName, Settings, SettingsInput } from '../../api/types';
import { featuresFromSettings } from '../../lib/features';
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
        // premium switch (prototype .sw, as ui.tsx Switch): 51×31, green when on
        'relative h-[31px] w-[51px] shrink-0 rounded-full transition-colors duration-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-60',
        checked ? 'bg-status-up' : 'bg-chip',
        busy && 'opacity-60',
      )}
    >
      <span
        aria-hidden="true"
        className={cx(
          'absolute top-[2px] h-[27px] w-[27px] rounded-full bg-white shadow-[0_2px_6px_rgba(0,0,0,0.2)] transition-[inset-inline-start] duration-300 ease-premium',
          checked ? 'start-[22px]' : 'start-[2px]',
        )}
      />
    </button>
  );
}

/**
 * A settings section of a feature: shown only while the feature is ON (owner decision: an OFF
 * feature's tab shows just its description and switch; the stored settings are kept).
 */
export function FeatureSection({
  feature,
  settings,
  children,
}: {
  feature: FeatureName | FeatureName[];
  settings: Settings;
  children: React.ReactNode;
}) {
  const f = featuresFromSettings(settings);
  const names = Array.isArray(feature) ? feature : [feature];
  return names.some((n) => f[n]) ? <>{children}</> : null;
}
