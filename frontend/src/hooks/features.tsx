import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { endpoints, qk } from '../api/endpoints';
import type { FeatureName, Features } from '../api/types';
import { normalizeFeatures } from '../lib/features';
import { useAuth } from './auth';

/** [2b-3] Feature toggles from /api/settings/public (also before login); refetched on `features.changed`. */
export function useFeatures(): {
  features: Features;
  ldap: { domain: string; netbios_domain: string } | null;
  loaded: boolean;
} {
  const q = useQuery({
    queryKey: qk.publicSettings,
    queryFn: endpoints.publicSettings,
    staleTime: 60_000,
  });
  return {
    features: normalizeFeatures(q.data?.features),
    ldap: q.data?.ldap ?? null,
    loaded: !!q.data,
  };
}

export function useFeature(name: FeatureName): boolean {
  return useFeatures().features[name];
}

/**
 * One-line explanation for an entry point of a disabled feature, with a link to
 * Settings → Features (admins) or "Ask an administrator" (viewers).
 */
export function FeatureOffNotice({
  feature,
  className,
}: {
  feature: FeatureName | FeatureName[];
  className?: string;
}) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const names = (Array.isArray(feature) ? feature : [feature])
    .map((f) => t(`features.name.${f}`))
    .join(' / ');
  return (
    <div
      role="note"
      data-testid="feature-off"
      className={`rounded-md bg-slate-100 px-3 py-2 text-sm text-slate-700 ring-1 ring-inset ring-slate-200 dark:bg-slate-800/60 dark:text-slate-300 dark:ring-slate-700 ${className ?? ''}`}
    >
      {t('features.offLine', { name: names })}{' '}
      {isAdmin ? (
        <Link
          to="/settings?tab=features"
          className="font-medium text-sky-700 hover:underline dark:text-sky-400"
        >
          {t('features.openSettings')}
        </Link>
      ) : (
        <span className="text-slate-500">{t('features.askAdmin')}</span>
      )}
    </div>
  );
}
