import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { FeatureOffNotice, useFeatures } from '../hooks/features';
import { ingestOn } from '../lib/features';
import { RulesView } from '../components/logs/RulesView';
import { SyslogView } from '../components/logs/SyslogView';
import { TrapsView } from '../components/logs/TrapsView';
import { UnknownSourcesView } from '../components/logs/UnknownSourcesView';
import { IngestStatusLine } from '../components/features/IngestStatus';
import { Loading, PageHeader } from '../components/ui';
import { TabBar } from '../components/TabBar';

type LogsTab = 'syslog' | 'traps' | 'unknown' | 'rules';

/** [2b-3] Traps & Syslog (/logs): Syslog | Traps | Unknown sources | Rules. */
export function LogsPage() {
  const { t } = useTranslation();
  const { features, loaded } = useFeatures();
  const [params, setParams] = useSearchParams();
  const tabs: LogsTab[] = [
    ...(features.syslog ? (['syslog'] as const) : []),
    ...(features.traps ? (['traps'] as const) : []),
    'unknown',
    'rules',
  ];
  const p = params.get('tab') as LogsTab | null;
  const tab: LogsTab = p && tabs.includes(p) ? p : tabs[0];
  if (!loaded) return <Loading />;
  return (
    <div className="space-y-4">
      <PageHeader title={t('logs.title')} subtitle={<IngestStatusLine />} />
      {!ingestOn(features) ? (
        <FeatureOffNotice feature={['traps', 'syslog']} />
      ) : (
        <>
          <TabBar
            tabs={tabs}
            value={tab}
            label={t('logs.title')}
            onChange={(k) => setParams({ tab: k }, { replace: true })}
            render={(k) => t(`logs.tab.${k}`)}
          />
          {tab === 'syslog' && <SyslogView />}
          {tab === 'traps' && <TrapsView />}
          {tab === 'unknown' && <UnknownSourcesView />}
          {tab === 'rules' && <RulesView />}
        </>
      )}
    </div>
  );
}
