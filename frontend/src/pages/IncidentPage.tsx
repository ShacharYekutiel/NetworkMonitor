import { useMemo } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link, useParams } from 'react-router-dom';
import { endpoints3b, qk3b } from '../api/endpoints3b';
import type { IncidentMember, Status } from '../api/types';
import { AiBadge, AiNotice, useAiFeature } from '../components/assistant/common';
import { ExplainButton } from '../components/assistant/ExplainButton';
import {
  IncidentCounts,
  IncidentStateBadge,
  useIncidentTitle,
} from '../components/assistant/Incidents';
import { StatusBadge } from '../components/Status';
import { Button, Card, ErrorBox, Loading, PageHeader, Table, td, th } from '../components/ui';
import { FeatureOffNotice } from '../hooks/features';
import { useLiveMessages } from '../hooks/live';
import { formatDateTime } from '../lib/format';

const STATUS_SET = new Set(['up', 'warning', 'down', 'paused', 'unknown', 'unreachable']);
const asStatus = (s: string): Status => (STATUS_SET.has(s) ? (s as Status) : 'unknown');

/** Timeline strip: one tick per member problem between opened_at and now/resolved. */
function TimelineStrip({
  members,
  start,
  end,
}: {
  members: IncidentMember[];
  start: number;
  end: number;
}) {
  const span = Math.max(1, end - start);
  return (
    <div className="relative h-6 rounded bg-slate-100 dark:bg-slate-800" aria-hidden>
      {members.map((m) => {
        const pos = ((Date.parse(m.time) - start) / span) * 100;
        const cls =
          m.status === 'warning'
            ? 'bg-amber-500'
            : m.status === 'unreachable'
              ? 'bg-red-400'
              : 'bg-red-600';
        return (
          <span
            key={m.event_id}
            title={`${m.device_name ?? ''} ${m.sensor_name ?? ''} ${formatDateTime(m.time)}`}
            className={`absolute top-1 h-4 w-1 rounded ${cls} ${m.recovered_at ? 'opacity-40' : ''}`}
            style={{ insetInlineStart: `${Math.min(99, Math.max(0, pos))}%` }}
          />
        );
      })}
    </div>
  );
}

/** /incidents/:id — incident detail (feature `ai_correlation`). */
export function IncidentPage() {
  const { t } = useTranslation();
  const { id } = useParams();
  const iid = Number(id);
  const qc = useQueryClient();
  const on = useAiFeature('ai_correlation');
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
  const groups = useMemo(() => {
    const m = new Map<string, IncidentMember[]>();
    for (const x of q.data?.members ?? []) {
      const k = `${x.device_id ?? 0}|${x.device_name ?? ''}`;
      m.set(k, [...(m.get(k) ?? []), x]);
    }
    return [...m.entries()];
  }, [q.data]);
  if (!on) return <FeatureOffNotice feature="ai_correlation" />;
  if (q.isLoading) return <Loading />;
  if (q.error || !q.data) return <ErrorBox error={q.error} />;
  const i = q.data;
  const root = i.members.find((m) => m.is_root && m.sensor_id !== null);
  const start = Date.parse(i.opened_at);
  const end = i.resolved_at
    ? Date.parse(i.resolved_at)
    : Math.max(Date.now(), Date.parse(i.last_change_at));
  return (
    <div className="space-y-4">
      <PageHeader
        breadcrumbs={
          <Link
            to="/events?tab=incidents"
            className="text-xs text-sky-700 hover:underline dark:text-sky-400"
          >
            {t('incident.all')}
          </Link>
        }
        title={
          <span className="inline-flex flex-wrap items-center gap-2" dir="auto">
            {title(i)} <IncidentStateBadge state={i.state} />
          </span>
        }
        subtitle={`${formatDateTime(i.opened_at)}${i.resolved_at ? ` → ${formatDateTime(i.resolved_at)}` : ''}`}
        actions={
          <>
            {root?.sensor_id != null && (
              <ExplainButton sensorId={root.sensor_id} eventId={root.event_id} size="md" />
            )}
            <Button onClick={() => summarize.mutate()} busy={summarize.isPending}>
              {t('incident.summarize')}
            </Button>
          </>
        }
      />
      <ErrorBox error={summarize.error} />
      <Card title={t('incident.summary')}>
        <div className="space-y-2">
          <IncidentCounts i={i} />
          {i.ai ? (
            <div className="space-y-1" data-testid="incident-ai">
              <p className="font-medium" dir="auto">
                <AiBadge className="me-1" />
                {i.ai.title}
              </p>
              <p className="text-sm" dir="auto">
                {i.ai.summary}
              </p>
              {i.ai.root_guess && (
                <p className="text-xs text-slate-500">
                  {t('incident.rootGuess', {
                    name:
                      i.root_candidates.find((c) => c.id === i.ai!.root_guess!.device_id)?.name ??
                      i.members.find((m) => m.device_id === i.ai!.root_guess!.device_id)
                        ?.device_name ??
                      `#${i.ai.root_guess.device_id}`,
                    confidence: t(`confidence3.${i.ai.root_guess.confidence}`),
                  })}
                </p>
              )}
              <AiNotice />
            </div>
          ) : (
            <p className="text-sm text-slate-500">{t('incident.noAi')}</p>
          )}
          {i.root_candidates.length > 0 && (
            <p className="text-sm">
              {t('incident.rootCandidates')}:{' '}
              {i.root_candidates.map((c, n) => (
                <span key={c.id}>
                  {n > 0 && ', '}
                  <Link
                    to={`/devices/${c.id}`}
                    className="text-sky-700 hover:underline dark:text-sky-400"
                  >
                    {c.name}
                  </Link>
                </span>
              ))}
            </p>
          )}
          <TimelineStrip members={i.members} start={start} end={end} />
          <p className="text-xs text-slate-500">{t('incident.noAckAll')}</p>
        </div>
      </Card>
      <Card title={t('incident.members', { count: i.member_count })} bodyClassName="p-0">
        <Table>
          <thead>
            <tr>
              <th className={th}>{t('incident.device')}</th>
              <th className={th}>{t('incident.sensor')}</th>
              <th className={th}>{t('events.status')}</th>
              <th className={th}>{t('events.time')}</th>
              <th className={th}>{t('incident.recoveredAt')}</th>
            </tr>
          </thead>
          <tbody>
            {groups.map(([k, ms]) =>
              ms.map((m, n) => (
                <tr key={`${k}-${m.event_id}`} data-testid="incident-member">
                  <td className={td}>
                    {n === 0 &&
                      (m.device_id ? (
                        <Link
                          to={`/devices/${m.device_id}`}
                          className="font-medium hover:underline"
                        >
                          {m.device_name}
                        </Link>
                      ) : (
                        m.device_name
                      ))}
                    {n === 0 && m.is_root && (
                      <span className="ms-1 rounded bg-slate-200 px-1 text-[10px] dark:bg-slate-700">
                        {t('incident.root')}
                      </span>
                    )}
                  </td>
                  <td className={td}>
                    {m.sensor_id ? (
                      <Link
                        to={`/sensors/${m.sensor_id}`}
                        className="text-sky-700 hover:underline dark:text-sky-400"
                      >
                        {m.sensor_name}
                      </Link>
                    ) : (
                      m.sensor_name
                    )}
                  </td>
                  <td className={td}>
                    <StatusBadge status={asStatus(m.status)} />
                  </td>
                  <td className={`${td} whitespace-nowrap tabular-nums`}>
                    {formatDateTime(m.time)}
                  </td>
                  <td className={`${td} whitespace-nowrap tabular-nums`}>
                    {m.recovered_at ? formatDateTime(m.recovered_at) : '—'}
                  </td>
                </tr>
              )),
            )}
          </tbody>
        </Table>
      </Card>
    </div>
  );
}
