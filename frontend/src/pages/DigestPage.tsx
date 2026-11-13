import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link, useParams } from 'react-router-dom';
import { endpoints3b, qk3b } from '../api/endpoints3b';
import { useAiFeature } from '../components/assistant/common';
import { DigestList, DigestView } from '../components/assistant/Digest';
import { ErrorBox, Loading, PageHeader } from '../components/ui';
import { FeatureOffNotice } from '../hooks/features';
import { useLiveMessages } from '../hooks/live';

/** /assistant/digests and /assistant/digests/:day (feature `ai_syslog_digest`, admins). */
export function DigestPage() {
  const { t } = useTranslation();
  const { day } = useParams();
  const qc = useQueryClient();
  const on = useAiFeature('ai_syslog_digest');
  const valid = !!day && /^\d{4}-\d{2}-\d{2}$/.test(day);
  const q = useQuery({
    queryKey: qk3b.digest(day ?? ''),
    queryFn: () => endpoints3b.digest(day!),
    enabled: on && valid,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 3000 : false),
  });
  useLiveMessages(['digest.ready'], (m) => {
    if (m.type === 'digest.ready' && m.data.day === day)
      void qc.invalidateQueries({ queryKey: qk3b.digest(day) });
  });
  if (!on) return <FeatureOffNotice feature="ai_syslog_digest" />;
  return (
    <div className="space-y-4">
      <PageHeader
        breadcrumbs={
          day && (
            <Link
              to="/assistant/digests"
              className="text-sky-700 hover:underline dark:text-sky-400"
            >
              {t('digest.title')}
            </Link>
          )
        }
        title={day ? t('digest.dayTitle', { day }) : t('digest.title')}
      />
      {!day && <DigestList />}
      {day && q.isLoading && <Loading />}
      {day && <ErrorBox error={q.error} />}
      {q.data && <DigestView d={q.data} />}
    </div>
  );
}
