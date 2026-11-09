import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import type { FeatureName, Settings } from '../../api/types';
import { featuresFromSettings, featuresOfTab, featureTab, movedFeatures } from '../../lib/features';
import { Card, cx } from '../ui';
import { FeatureToggleList } from './FeatureToggle';

/** Read-only row of a feature toggled in its own tab: name, on/off and a link to that tab. */
function OverviewRow({ name, on }: { name: FeatureName; on: boolean }) {
  const { t } = useTranslation();
  const tab = featureTab(name);
  return (
    <li
      className="flex flex-wrap items-center gap-x-3 gap-y-0.5 px-4 py-2 text-sm"
      data-feature-overview={name}
    >
      <span
        aria-hidden
        className={cx('h-2 w-2 shrink-0 rounded-full', on ? 'bg-green-600' : 'bg-slate-400')}
      />
      <span className="font-medium">{t(`features.name.${name}`)}</span>
      <span className={cx('text-xs', on ? 'text-green-700 dark:text-green-400' : 'text-slate-500')}>
        {on ? t('features.on') : t('features.off')}
      </span>
      <Link
        to={`/settings?tab=${tab}`}
        className="ms-auto text-xs font-medium text-sky-700 hover:underline dark:text-sky-400"
      >
        {t('features.configuredIn', { tab: t(`settings.tab.${tab}`) })}
      </Link>
    </li>
  );
}

/**
 * [2b-3] Settings → Features: switches of the features without a settings tab of their own, plus a
 * read-only overview of the others (each is switched at the top of its own tab).
 */
export function FeaturesTab({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const f = featuresFromSettings(settings);
  return (
    <div className="space-y-6">
      <Card title={t('features.title')} bodyClassName="p-0">
        <p className="border-b border-slate-200 px-4 py-2 text-xs text-slate-500 dark:border-slate-800">
          {t('features.intro')}
        </p>
        <div data-testid="features-list">
          <FeatureToggleList names={featuresOfTab('features')} settings={settings} />
        </div>
      </Card>
      <Card title={t('features.ownTabTitle')} bodyClassName="p-0">
        <p className="border-b border-slate-200 px-4 py-2 text-xs text-slate-500 dark:border-slate-800">
          {t('features.ownTabIntro')}
        </p>
        <ul
          className="divide-y divide-slate-100 dark:divide-slate-800"
          data-testid="features-overview"
        >
          {movedFeatures().map((n) => (
            <OverviewRow key={n} name={n} on={f[n]} />
          ))}
        </ul>
      </Card>
    </div>
  );
}
