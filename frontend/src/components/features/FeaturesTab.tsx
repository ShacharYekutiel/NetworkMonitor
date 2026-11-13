import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import type { FeatureName, Settings } from '../../api/types';
import { FEATURE_GROUPS, featuresFromSettings, featureTab } from '../../lib/features';
import { Card, cx } from '../ui';
import { FeatureToggleList } from './FeatureToggle';
import { cardAnchor } from '../../lib/settingsIndex';
import { AI_FEATURES } from '../../api/types';
import { useAssistantStatus } from '../assistant/common';

/** [3b] state / idle reason / status line of an assistant feature (GET /api/assistant/status). */
function AiStatusLine({ name }: { name: FeatureName }) {
  const { t } = useTranslation();
  const isAi = name === 'assistant' || (AI_FEATURES as readonly string[]).includes(name);
  const st = useAssistantStatus(isAi);
  if (!isAi || !st.data) return null;
  if (name === 'assistant')
    return <span className="text-xs text-ink-muted">{t(`assistant.state.${st.data.state}`)}</span>;
  const f = st.data.features.find((x) => x.name === name);
  if (!f) return null;
  if (f.idle_reason)
    return <span className="text-xs text-status-warning">{t('assistant.idleShort')}</span>;
  return f.status_line ? <span className="text-xs text-ink-muted">{f.status_line}</span> : null;
}

/** Read-only row of a feature switched in its own tab: name, on/off and a link to that tab. */
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
        className={cx('h-2 w-2 shrink-0 rounded-full', on ? 'bg-status-up' : 'bg-status-paused')}
      />
      <span className="font-medium">{t(`features.name.${name}`)}</span>
      <span className={cx('text-xs', on ? 'text-status-up' : 'text-ink-muted')}>
        {on ? t('features.on') : t('features.off')}
      </span>
      {on && <AiStatusLine name={name} />}
      <Link
        to={`/settings?tab=${tab}`}
        className="ms-auto text-xs font-medium text-accent-ink hover:underline"
      >
        {t('features.configuredIn', { tab: t(`settings.tab.${tab}`) })}
      </Link>
    </li>
  );
}

function GroupHeading({ id, k }: { id: string; k: string }) {
  const { t } = useTranslation();
  return (
    <h3
      id={id}
      className="border-b border-line bg-surface-sunken px-4 py-1.5 text-xs font-semibold text-ink-muted"
    >
      {t(`features.group.${k}`)}
    </h3>
  );
}

/**
 * [2b-3] Settings → Features: switches of the features without a settings tab of their own, plus a
 * read-only overview of the others (each is switched in its own tab), both grouped.
 */
export function FeaturesTab({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const f = featuresFromSettings(settings);
  const here = FEATURE_GROUPS.map((g) => ({
    key: g.key,
    names: g.names.filter((n) => featureTab(n) === 'features'),
  })).filter((g) => g.names.length);
  const moved = FEATURE_GROUPS.map((g) => ({
    key: g.key,
    names: g.names.filter((n) => featureTab(n) !== 'features'),
  })).filter((g) => g.names.length);
  return (
    <div className="space-y-6">
      <Card title={t('features.title')} bodyClassName="p-0" anchor={cardAnchor('features')}>
        <p className="border-b border-line px-4 py-2 text-xs text-ink-muted">
          {t('features.intro')}
        </p>
        <div data-testid="features-list">
          {here.map((g) => (
            <section key={g.key} aria-labelledby={`fg-${g.key}`}>
              <GroupHeading id={`fg-${g.key}`} k={g.key} />
              <FeatureToggleList names={g.names} settings={settings} />
            </section>
          ))}
        </div>
      </Card>
      <Card title={t('features.ownTabTitle')} bodyClassName="p-0">
        <p className="border-b border-line px-4 py-2 text-xs text-ink-muted">
          {t('features.ownTabIntro')}
        </p>
        <div data-testid="features-overview">
          {moved.map((g) => (
            <section key={g.key} aria-labelledby={`fo-${g.key}`}>
              <GroupHeading id={`fo-${g.key}`} k={g.key} />
              <ul className="divide-y divide-line">
                {g.names.map((n) => (
                  <OverviewRow key={n} name={n} on={f[n]} />
                ))}
              </ul>
            </section>
          ))}
        </div>
      </Card>
    </div>
  );
}
