import { useMemo, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link, useParams } from 'react-router';
import { endpoints3b, qk3b } from '../api/endpoints3b';
import type { IncidentMember, Status } from '../api/types';
import { AiNotice, useAiFeature } from '../components/assistant/common';
import { ExplainButton } from '../components/assistant/ExplainButton';
import {
  IncidentCounts,
  IncidentStateBadge,
  useIncidentTitle,
} from '../components/assistant/Incidents';
import { IncidentIcon, RefreshIcon } from '../components/icons';
import {
  AiTag,
  Banner,
  EmptyState,
  MemberChip,
  SeverityTag,
  Timeline,
  TimelineEntry,
} from '../components/kit';
import {
  BackLink,
  Button,
  buttonClass,
  Card,
  cx,
  ErrorBox,
  GroupList,
  GroupTitle,
  ListRow,
  Loading,
  PageHeader,
  TAG,
  TAG_TONE,
} from '../components/ui';
import { FeatureOffNotice, useFeature } from '../hooks/features';
import { useLiveMessages } from '../hooks/live';
import { errorMessage } from '../lib/errors';
import { formatDateTime, formatShortTime } from '../lib/format';
import { STATUS_DOT, statusClass } from '../lib/status';

const STATUS_SET = new Set(['up', 'warning', 'down', 'paused', 'unknown', 'unreachable']);
const asStatus = (s: string): Status => (STATUS_SET.has(s) ? (s as Status) : 'unknown');

type TlItem = { key: string; at: string; tone: string; title: ReactNode; sub: ReactNode };

/** /incidents/:id — incident detail (feature `ai_correlation`). */
export function IncidentPage() {
  const { t, i18n } = useTranslation();
  const { id } = useParams();
  const iid = Number(id);
  const qc = useQueryClient();
  const on = useAiFeature('ai_correlation');
  const severityOn = useFeature('alert_severity'); // [severity]
  const title = useIncidentTitle();
  const q = useQuery({
    queryKey: qk3b.incident(iid),
    queryFn: () => endpoints3b.incident(iid),
    enabled: on && Number.isFinite(iid),
  });
  useLiveMessages(['incident.update'], (m) => {
    if (m.type === 'incident.update' && m.data.incident.id === iid)
      void qc.invalidateQueries({ queryKey: qk3b.incident(iid) });
  });
  const summarize = useMutation({
    mutationFn: () => endpoints3b.summarizeIncident(iid),
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk3b.incident(iid) }),
  });
  const locale = i18n.language === 'he' ? 'he-IL' : 'en-GB';
  // Problems and recoveries in time order (prototype incident timeline).
  const items = useMemo(() => {
    const out: TlItem[] = [];
    const name = (m: IncidentMember) =>
      [m.device_name, m.sensor_name].filter(Boolean).join(' · ') || '—';
    for (const m of q.data?.members ?? []) {
      out.push({
        key: `p${m.event_id}`,
        at: m.time,
        tone: asStatus(m.status),
        title: (
          <bdi dir="auto" className="font-display">
            {name(m)}
          </bdi>
        ),
        sub: [t(`status.${asStatus(m.status)}`), m.is_root ? t('incident.root') : null]
          .filter(Boolean)
          .join(' · '),
      });
      if (m.recovered_at)
        out.push({
          key: `r${m.event_id}`,
          at: m.recovered_at,
          tone: 'up',
          title: (
            <bdi dir="auto" className="font-display">
              {name(m)}
            </bdi>
          ),
          sub: t('incident.recoveredAt'),
        });
    }
    return out.sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
  }, [q.data, t]);
  if (!on) return <FeatureOffNotice feature="ai_correlation" />;
  if (q.isLoading) return <Loading />;
  const back = <BackLink to="/events?tab=incidents" label={t('incident.all')} />;
  if (q.error || !q.data)
    return (
      <div>
        <PageHeader breadcrumbs={back} title={t('incident.title')} />
        <Card>
          <EmptyState
            error
            title={t('incident.pageError')}
            actions={
              <Link to="/events?tab=incidents" className={buttonClass('primary')}>
                {t('incident.all')}
              </Link>
            }
          >
            {q.error ? errorMessage(t, q.error) : t('incident.pageErrorHint')}
          </EmptyState>
        </Card>
      </div>
    );
  const i = q.data;
  const root = i.members.find((m) => m.is_root && m.sensor_id !== null);
  const guess = i.ai?.root_guess;
  const guessName = guess
    ? (i.root_candidates.find((c) => c.id === guess.device_id)?.name ??
      i.members.find((m) => m.device_id === guess.device_id)?.device_name ??
      `#${guess.device_id}`)
    : null;
  const timeFmt = new Intl.DateTimeFormat(locale, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  });
  const subtitle = [
    t('incident.kindLine'),
    i.state === 'resolved' && i.resolved_at
      ? t('incident.resolvedAt', { time: formatShortTime(i.resolved_at) })
      : t('incident.since', { time: formatShortTime(i.opened_at) }),
    t('incident.grouped', { count: i.member_count }),
    i.recovered ? t('incident.recoveredN', { count: i.recovered }) : null,
  ]
    .filter(Boolean)
    .join(' · ');
  return (
    <div>
      <PageHeader
        breadcrumbs={back}
        title={<span>{title(i)}</span>}
        subtitle={<span title={formatDateTime(i.opened_at)}>{subtitle}</span>}
        actions={
          <>
            {severityOn && (
              <SeverityTag severity={i.severity} size="md" testId="incident-severity" />
            )}
            <IncidentStateBadge state={i.state} />
            {root?.sensor_id != null && (
              <ExplainButton sensorId={root.sensor_id} eventId={root.event_id} size="md" />
            )}
            <Button onClick={() => summarize.mutate()} busy={summarize.isPending}>
              <RefreshIcon size={14} /> {t('incident.summarize')}
            </Button>
          </>
        }
      />
      <ErrorBox error={summarize.error} className="mb-4" />
      <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
        <div className="grid min-w-0 gap-4">
          <Card
            title={
              <span className="inline-flex items-center gap-2">
                {t('incident.summary')}
                {i.ai && <AiTag />}
              </span>
            }
          >
            <div className="grid gap-3">
              <IncidentCounts i={i} />
              {i.ai ? (
                <div className="grid gap-3" data-testid="incident-ai">
                  {guessName && (
                    <Banner
                      tone="down"
                      icon={IncidentIcon}
                      title={t('incident.rootGuess', {
                        name: guessName,
                        confidence: t(`confidence3.${guess!.confidence}`),
                      })}
                    />
                  )}
                  <b className="font-semibold" dir="auto">
                    {i.ai.title}
                  </b>
                  <p className="m-0 leading-[1.65]" dir="auto">
                    {i.ai.summary}
                  </p>
                  <AiNotice className="m-0" />
                </div>
              ) : (
                <p className="m-0 text-ink-muted">{t('incident.noAi')}</p>
              )}
              {i.root_candidates.length > 0 && (
                <div className="grid gap-1.5">
                  <span className="text-[13px] text-ink-muted">{t('incident.rootCandidates')}</span>
                  <span className="flex flex-wrap gap-1.5">
                    {i.root_candidates.map((c) => (
                      <Link key={c.id} to={`/devices/${c.id}`} className="no-underline">
                        <MemberChip tone="down">{c.name}</MemberChip>
                      </Link>
                    ))}
                  </span>
                </div>
              )}
            </div>
          </Card>
          <Card bodyClassName="px-[22px] pb-1.5 pt-0 max-sm:px-3">
            <Timeline label={t('incident.timeline')} testId="incident-timeline">
              {items.map((x) => (
                <TimelineEntry
                  key={x.key}
                  time={timeFmt.format(new Date(x.at))}
                  timeTitle={formatDateTime(x.at)}
                  tone={x.tone}
                  title={x.title}
                >
                  {x.sub}
                </TimelineEntry>
              ))}
            </Timeline>
          </Card>
        </div>
        <section aria-labelledby="incident-members" className="min-w-0">
          <GroupTitle id="incident-members">
            {t('incident.members', { count: i.member_count })}
          </GroupTitle>
          <GroupList className="[&>li+li]:before:start-[46px]">
            {i.members.map((m) => {
              const recovered = !!m.recovered_at;
              return (
                <ListRow
                  key={m.event_id}
                  testId="incident-member"
                  to={
                    m.sensor_id
                      ? `/sensors/${m.sensor_id}`
                      : m.device_id
                        ? `/devices/${m.device_id}`
                        : undefined
                  }
                  dot={statusClass(recovered ? 'up' : asStatus(m.status), STATUS_DOT)}
                  title={
                    <bdi dir="ltr" className="font-display">
                      {m.device_name ?? '—'}
                    </bdi>
                  }
                  subtitle={
                    <>
                      {m.sensor_name && <bdi dir="auto">{m.sensor_name}</bdi>}
                      {m.sensor_name && ' · '}
                      {t(`status.${asStatus(m.status)}`)} ·{' '}
                      <time dateTime={m.time} title={formatDateTime(m.time)}>
                        {timeFmt.format(new Date(m.time))}
                      </time>
                      {recovered && (
                        <>
                          {' → '}
                          <time dateTime={m.recovered_at!} title={formatDateTime(m.recovered_at)}>
                            {timeFmt.format(new Date(m.recovered_at!))}
                          </time>
                        </>
                      )}
                    </>
                  }
                  end={
                    <span className="flex shrink-0 gap-1.5">
                      {severityOn && <SeverityTag severity={m.severity} />}
                      {m.is_root && (
                        <span className={cx(TAG, TAG_TONE.down)}>{t('incident.root')}</span>
                      )}
                      {recovered && (
                        <span className={cx(TAG, TAG_TONE.up)}>{t('incident.recoveredAt')}</span>
                      )}
                    </span>
                  }
                />
              );
            })}
          </GroupList>
          <p className="mx-1.5 mt-3 text-[13px] text-ink-muted">{t('incident.noAckAll')}</p>
        </section>
      </div>
    </div>
  );
}
