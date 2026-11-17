import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { endpoints3b, qk3b, type IncidentQuery } from '../../api/endpoints3b';
import type { Incident, IncidentState } from '../../api/types';
import { useLiveMessages } from '../../hooks/live';
import { errorMessage } from '../../lib/errors';
import { formatDateTime, formatShortTime } from '../../lib/format';
import { ChevronIcon, IncidentIcon, RefreshIcon } from '../icons';
import { EmptyState, MemberChip } from '../kit';
import { Pagination } from '../Pagination';
import { Button, Card, cx, GroupTitle, Loading, TAG, TAG_TONE } from '../ui';
import { AiBadge, useAiFeature } from './common';

/** Deterministic title from its fields ("core-sw down — 14 devices unreachable"), he/en. */
export function useIncidentTitle() {
  const { t, i18n } = useTranslation();
  // status words sit mid-sentence: "rtr-hfa down — 4 devices unreachable" (no-op in Hebrew)
  const word = (s: string) =>
    t(`status.${s}`, { defaultValue: s }).toLocaleLowerCase(i18n.language);
  return (i: Incident) => {
    const f = i.title_fields;
    if (!f?.root) return i.title;
    return t('incident.titleLine', {
      root: f.root,
      rootStatus: word(f.root_status),
      members: t(`incident.unit.${f.unit}`, { count: f.count }),
      status: word(f.status),
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

/** The counts as member chips (prototype .mchip), coloured by state. */
export function IncidentCountChips({ i }: { i: Incident }) {
  const { t } = useTranslation();
  const parts: [string, number, string][] = [
    ['down', i.down, 'down'],
    ['unreachable', i.unreachable, 'unreachable'],
    ['warning', i.warning, 'warning'],
    ['recovered', i.recovered, 'up'],
  ];
  return (
    <span className="mt-1 flex flex-wrap gap-1.5">
      {parts
        .filter(([, n]) => n > 0)
        .map(([k, n, tone]) => (
          <MemberChip key={k} tone={tone}>
            {n} {t(k === 'recovered' ? 'incident.recovered' : `status.${k}`)}
          </MemberChip>
        ))}
    </span>
  );
}

/** The state colour of an incident: its worst member while open, green resolved, grey stale. */
export function incidentTone(i: Incident): 'down' | 'unreachable' | 'warning' | 'up' | 'paused' {
  if (i.state === 'resolved') return 'up';
  if (i.state === 'stale') return 'paused';
  if (i.down > 0) return 'down';
  if (i.unreachable > 0) return 'unreachable';
  if (i.warning > 0) return 'warning';
  return 'down';
}

const TONE_TILE: Record<ReturnType<typeof incidentTone>, string> = {
  down: 'bg-status-down-soft text-status-down',
  unreachable: 'bg-status-unreachable-soft text-status-unreachable',
  warning: 'bg-status-warning-soft text-status-warning',
  up: 'bg-status-up-soft text-status-up',
  paused: 'bg-chip text-ink-muted',
};

/** "Open since 10:40 · 12 problems grouped · root: rtr-hfa (confidence: high)". */
export function useIncidentMeta() {
  const { t } = useTranslation();
  return (i: Incident): string => {
    const parts = [
      i.state === 'resolved' && i.resolved_at
        ? t('incident.resolvedAt', { time: formatShortTime(i.resolved_at) })
        : t('incident.since', { time: formatShortTime(i.opened_at) }),
      t('incident.grouped', { count: i.member_count }),
    ];
    const guess = i.ai?.root_guess;
    const rootName = guess
      ? (i.root_candidates.find((c) => c.id === guess.device_id)?.name ??
        (i.root_device?.id === guess.device_id ? i.root_device.name : null))
      : (i.root_device?.name ?? null);
    if (rootName)
      parts.push(
        guess
          ? t('incident.rootConf', {
              name: rootName,
              confidence: t(`confidence3.${guess.confidence}`),
            })
          : t('incident.rootShort', { name: rootName }),
      );
    if (!i.ai) parts.push(t('incident.noAiShort'));
    return parts.join(' · ');
  };
}

/** One incident card (prototype .inc): status tile, title, meta line, member chips, chevron. */
export function IncidentCard({ i }: { i: Incident }) {
  const title = useIncidentTitle();
  const meta = useIncidentMeta();
  const tone = incidentTone(i);
  return (
    <li data-testid="incident-card" data-incident={i.id}>
      <Link
        to={`/incidents/${i.id}`}
        className="grid w-full grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-4 rounded-xl bg-surface px-5 py-[18px] text-start text-ink no-underline shadow-card transition-transform duration-200 hover:-translate-y-0.5"
      >
        <span
          aria-hidden="true"
          className={cx(
            'grid h-[46px] w-[46px] place-items-center rounded-[14px]',
            TONE_TILE[tone],
          )}
        >
          <IncidentIcon size={22} strokeWidth={1.8} />
        </span>
        <span className="grid min-w-0 gap-1">
          <span className="flex flex-wrap items-center gap-2">
            <b className="min-w-0 text-[17px] font-semibold">{title(i)}</b>
            <IncidentStateBadge state={i.state} />
          </span>
          <span className="text-[13.5px] text-ink-muted" title={formatDateTime(i.opened_at)}>
            {meta(i)}
          </span>
          {i.ai && (
            <span
              className="line-clamp-2 text-[13.5px] text-ink-muted"
              dir="auto"
              data-testid="incident-ai"
            >
              <AiBadge className="me-1.5" />
              {i.ai.summary}
            </span>
          )}
          <IncidentCountChips i={i} />
        </span>
        <ChevronIcon size={16} className="shrink-0 text-ink-subtle rtl:rotate-180" />
      </Link>
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
  if (q.error)
    return (
      <Card>
        <EmptyState
          error
          title={t('incident.loadError')}
          actions={
            <Button variant="primary" onClick={() => void q.refetch()} busy={q.isFetching}>
              <RefreshIcon size={14} /> {t('common.retry')}
            </Button>
          }
        >
          {errorMessage(t, q.error)}
        </EmptyState>
      </Card>
    );
  if (!q.data?.items.length)
    return (
      <Card>
        <EmptyState icon={IncidentIcon} title={emptyText ?? t('incident.none')}>
          {t('incident.noneHint')}
        </EmptyState>
      </Card>
    );
  return (
    <>
      <ul className="m-0 grid list-none gap-3 p-0" data-testid="incident-list">
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

/** Home widget: open incidents (only while `ai_correlation` is ON and there are some). */
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
    <section aria-labelledby="home-incidents">
      <GroupTitle
        id="home-incidents"
        action={
          <Link to="/events?tab=incidents" className="text-ink-muted no-underline hover:text-ink">
            {t('incident.all')}
          </Link>
        }
      >
        {t('incident.openTitle')}{' '}
        <span className="num ms-1 inline-flex h-[22px] min-w-[22px] items-center justify-center rounded-full bg-status-down px-[7px] align-middle text-xs font-semibold text-on-status">
          {q.data.total}
        </span>
      </GroupTitle>
      <IncidentList query={{ state: ['open'], page_size: 10 }} />
    </section>
  );
}
