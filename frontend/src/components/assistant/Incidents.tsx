import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { endpoints3b, qk3b, type IncidentQuery } from '../../api/endpoints3b';
import type { Incident, IncidentState } from '../../api/types';
import { useLiveMessages } from '../../hooks/live';
import { formatDateTime, formatRelative } from '../../lib/format';
import { IncidentIcon } from '../icons';
import { Pagination } from '../Pagination';
import { Card, cx, Empty, ErrorBox, Loading, TAG, TAG_TONE } from '../ui';
import { AiBadge, useAiFeature } from './common';

/** Deterministic title from its fields ("core-sw down — 14 devices unreachable"), he/en. */
export function useIncidentTitle() {
  const { t } = useTranslation();
  return (i: Incident) => {
    const f = i.title_fields;
    if (!f?.root) return i.title;
    return t('incident.titleLine', {
      root: f.root,
      rootStatus: t(`status.${f.root_status}`, { defaultValue: f.root_status }),
      members: t(`incident.unit.${f.unit}`, { count: f.count }),
      status: t(`status.${f.status}`, { defaultValue: f.status }),
    });
  };
}

const STATE_CLS: Record<IncidentState, string> = {
  open: TAG_TONE.down,
  resolved: TAG_TONE.up,
  stale: TAG_TONE.paused,
};

export function IncidentStateBadge({ state }: { state: IncidentState }) {
  const { t } = useTranslation();
  return (
    <span className={cx(TAG, STATE_CLS[state])} data-state={state}>
      {t(`incident.state.${state}`)}
    </span>
  );
}

/** Counts strip: 1 down · 4 unreachable · 0 warning · 2 recovered. */
export function IncidentCounts({ i }: { i: Incident }) {
  const { t } = useTranslation();
  const parts: [string, number, string][] = [
    ['down', i.down, 'text-status-down'],
    ['unreachable', i.unreachable, 'text-status-unreachable'],
    ['warning', i.warning, 'text-status-warning'],
    ['recovered', i.recovered, 'text-status-up'],
  ];
  return (
    <span className="flex flex-wrap gap-x-3 text-xs tabular-nums">
      {parts
        .filter(([, n]) => n > 0)
        .map(([k, n, cls]) => (
          <span key={k} className={cls}>
            {n} {t(k === 'recovered' ? 'incident.recovered' : `status.${k}`)}
          </span>
        ))}
    </span>
  );
}

/** One incident card (dashboard, Events → Incidents). */
export function IncidentCard({ i }: { i: Incident }) {
  const { t } = useTranslation();
  const title = useIncidentTitle();
  return (
    <li className="space-y-1 px-4 py-3" data-testid="incident-card" data-incident={i.id}>
      <div className="flex flex-wrap items-center gap-2">
        <IncidentIcon size={14} className="text-ink-muted" />
        <Link
          to={`/incidents/${i.id}`}
          className="font-medium text-accent-ink hover:underline"
          dir="auto"
        >
          {title(i)}
        </Link>
        <IncidentStateBadge state={i.state} />
        <span className="ms-auto text-xs text-ink-muted" title={formatDateTime(i.opened_at)}>
          {formatRelative(i.opened_at)}
        </span>
      </div>
      <IncidentCounts i={i} />
      {i.ai && (
        <p className="text-sm text-ink-muted" dir="auto" data-testid="incident-ai">
          <AiBadge className="me-1" />
          {i.ai.summary}
        </p>
      )}
      {i.root_device && (
        <p className="text-xs text-ink-muted">
          {t('incident.rootCandidate')}:{' '}
          <Link to={`/devices/${i.root_device.id}`} className="hover:underline">
            {i.root_device.name}
          </Link>
        </p>
      )}
    </li>
  );
}

/** Live list of incidents (WS `incident.update` refreshes it). */
export function IncidentList({
  query,
  emptyText,
  onPage,
}: {
  query: IncidentQuery;
  emptyText?: string;
  onPage?: (p: number) => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: qk3b.incidents(query),
    queryFn: () => endpoints3b.incidents(query),
    placeholderData: keepPreviousData,
    refetchInterval: 60_000,
  });
  useLiveMessages(['incident.update'], () => {
    void qc.invalidateQueries({ queryKey: ['incidents'] });
  });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} className="m-3" />;
  if (!q.data?.items.length) return <Empty>{emptyText ?? t('incident.none')}</Empty>;
  return (
    <>
      <ul className="divide-y divide-line" data-testid="incident-list">
        {q.data.items.map((i) => (
          <IncidentCard key={i.id} i={i} />
        ))}
      </ul>
      {onPage && (
        <Pagination
          page={q.data.page}
          pageSize={q.data.page_size}
          total={q.data.total}
          onPage={onPage}
        />
      )}
    </>
  );
}

/** Dashboard card: open incidents (only while `ai_correlation` is ON and there are some). */
export function DashboardIncidents() {
  const { t } = useTranslation();
  const on = useAiFeature('ai_correlation');
  const q = useQuery({
    queryKey: qk3b.incidents({ state: ['open'], page_size: 10 }),
    queryFn: () => endpoints3b.incidents({ state: ['open'], page_size: 10 }),
    enabled: on,
    refetchInterval: 60_000,
  });
  if (!on || !q.data?.items.length) return null;
  return (
    <Card
      title={
        <span className="inline-flex items-center gap-2">
          <IncidentIcon size={14} /> {t('incident.openTitle')}
          <span className="inline-flex h-[18px] min-w-[20px] items-center justify-center rounded-sm bg-status-down px-[5px] font-mono text-[11px] font-bold tabular-nums text-on-status">
            {q.data.total}
          </span>
        </span>
      }
      actions={
        <Link to="/events?tab=incidents" className="text-xs text-accent-ink hover:underline">
          {t('incident.all')}
        </Link>
      }
      bodyClassName="p-0"
    >
      <IncidentList query={{ state: ['open'], page_size: 10 }} />
    </Card>
  );
}
