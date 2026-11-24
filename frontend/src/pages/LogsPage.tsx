import { useSearchParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { FeatureOffNotice, useFeatures } from '../hooks/features';
import { ingestOn } from '../lib/features';
import { RulesView } from '../components/logs/RulesView';
import { SyslogProtectionView } from '../components/logs/SyslogProtectionView';
import { useAuth } from '../hooks/auth';
import { SyslogView } from '../components/logs/SyslogView';
import { TrapsView } from '../components/logs/TrapsView';
import { UnknownSourcesView } from '../components/logs/UnknownSourcesView';
import { IngestStatusLine } from '../components/features/IngestStatus';
import { Loading, PageHeader } from '../components/ui';
import { TabBar } from '../components/TabBar';
import { useAiFeature } from '../components/assistant/common';
import { DigestDay } from '../components/assistant/Digest';

type LogsTab = 'syslog' | 'traps' | 'unknown' | 'rules' | 'protection' | 'digest';

/** [2b-3] Traps & Syslog (/logs): Syslog | Traps | Unknown sources | Rules. */
export function LogsPage() {
  const { t } = useTranslation();
  const { features, loaded } = useFeatures();
  const [params, setParams] = useSearchParams();
  const digestOn = useAiFeature('ai_syslog_digest');
  const { isAdmin } = useAuth();
  const tabs: LogsTab[] = [
    ...(features.syslog ? (['syslog'] as const) : []),
    ...(features.traps ? (['traps'] as const) : []),
    'unknown',
    'rules',
    // [syslog-protection] filter rules, counters, top talkers (admins)
    ...(features.syslog && isAdmin ? (['protection'] as const) : []),
    // [3b] daily syslog digest (admins, feature ai_syslog_digest)
    ...(digestOn ? (['digest'] as const) : []),
  ];
  const p = params.get('tab') as LogsTab | null;
  const tab: LogsTab = p && tabs.includes(p) ? p : tabs[0];
  if (!loaded) return <Loading />;
  return (
    <div className="space-y-4">
      <PageHeader title={t('logs.title')} actions={<IngestStatusLine />} />
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
          {tab === 'protection' && <SyslogProtectionView />}
          {tab === 'digest' && <DigestDay />}
        </>
      )}
    </div>
  );
}
