import { useState } from 'react';
import { Link, useSearchParams } from 'react-router';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk, type EventQuery } from '../api/endpoints';
import {
  EVENT_KINDS,
  INCIDENT_STATES,
  type EventKind,
  type IncidentState,
  type NmEvent,
} from '../api/types';
import { EventSource, StatusTransition, useEventTitle } from '../components/EventList';
import { Pagination } from '../components/Pagination';
import { ExplainButton } from '../components/assistant/ExplainButton';
import { useAiFeature } from '../components/assistant/common';
import { IncidentList } from '../components/assistant/Incidents';
import { StatusMessage } from '../components/Dependency';
import { ChevronIcon, WrenchIcon } from '../components/icons';
import { FilterChips, SeverityTag, Timeline, TimelineEntry } from '../components/kit';
import { SeverityChips, SeverityMappingInfo } from '../components/severity/Severity';
import { useFeature } from '../hooks/features';
import { parseSeverities, severityCsv } from '../lib/severity';
import { TabBar } from '../components/TabBar';
import {
  Button,
  Card,
  Empty,
  ErrorBox,
  Field,
  Input,
  Loading,
  PageHeader,
  SegmentedControl,
  Select,
  cx,
} from '../components/ui';
import { useTree } from '../hooks/queries';
import { useLocalizeMessage } from '../hooks/localize';
import { groupByDay } from '../lib/timeline';
import { flattenGroups } from '../lib/tree';
import { STATUSES } from '../lib/status';

/** [3b] Events | Incidents (feature ai_correlation). */
function EventsTabs() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const tab = params.get('tab') === 'incidents' ? 'incidents' : 'events';
  return (
    <TabBar
      tabs={['events', 'incidents']}
      value={tab}
      label={t('events.title')}
      onChange={(k) => setParams(k === 'events' ? {} : { tab: k }, { replace: true })}
      render={(k) => t(`incident.tab.${k}`)}
    />
  );
}

/** Events tab "Incidents" (prototype "אירועים מקובצים"): state chips and the incident cards. */
function IncidentsTab() {
  const { t } = useTranslation();
  const [state, setState] = useState<IncidentState | ''>('');
  const [page, setPage] = useState(1);
  return (
    <div className="grid gap-[18px]">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <EventsTabs />
        <FilterChips
          label={t('incident.stateFilter')}
          testId="incident-state-filter"
          value={state}
          onChange={(v) => {
            setState(v);
            setPage(1);
          }}
          options={[
            { value: '' as const, label: t('common.all') },
            ...INCIDENT_STATES.map((s) => ({ value: s, label: t(`incident.state.${s}`) })),
          ]}
        />
      </div>
      <IncidentList
        query={{ state: state ? [state] : undefined, page, page_size: 50 }}
        onPage={setPage}
      />
    </div>
  );
}

const PAGE_SIZE = 50;

/** datetime-local value (local time) <-> ISO UTC */
function localToIso(v: string): string | undefined {
  if (!v) return undefined;
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

/** The filters beyond the kind chips (status, group, time range, maintenance). */
const MORE = ['status', 'group_id', 'since', 'until', 'in_maintenance'] as const;

/** One event on the timeline: the source and what happened, the message under it. */
function EventEntry({
  ev,
  time,
  timeTitle,
  severity,
}: {
  ev: NmEvent;
  time: string;
  timeTitle: string;
  severity?: boolean;
}) {
  const { t } = useTranslation();
  const localize = useLocalizeMessage();
  const title = useEventTitle();
  const hasSource = !!(ev.device_name || ev.sensor_name);
  const explain =
    ev.kind === 'status_change' &&
    ev.sensor_id !== null &&
    (ev.new_status === 'down' || ev.new_status === 'warning' || ev.new_status === 'unreachable');
  return (
    <TimelineEntry
      testId="event-entry"
      time={time}
      timeTitle={timeTitle}
      tone={ev.new_status ?? 'neutral'}
      title={
        <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1">
          {hasSource ? <EventSource ev={ev} /> : <span>{title(ev)}</span>}
          {severity && <SeverityTag severity={ev.severity} />}
          {ev.new_status ? (
            <StatusTransition ev={ev} />
          ) : (
            hasSource && (
              <span className="text-[13px] font-medium text-ink-muted">
                {t(`eventKind.${ev.kind}`, { defaultValue: ev.kind })}
              </span>
            )
          )}
        </span>
      }
      end={explain ? <ExplainButton sensorId={ev.sensor_id!} eventId={ev.id} /> : undefined}
    >
      <span dir="auto">
        {ev.in_maintenance && (
          <span
            className="me-1 inline-flex align-[-2px] text-accent-ink"
            title={t('events.inMaintenance')}
            aria-label={t('events.inMaintenance')}
          >
            <WrenchIcon size={13} />
          </span>
        )}
        {ev.device_id !== null && ev.new_status === 'unreachable' ? (
          <StatusMessage message={ev.message} status="unreachable" deviceId={ev.device_id} />
        ) : (
          localize(ev.message)
        )}
      </span>
    </TimelineEntry>
  );
}

/** The events as a timeline (prototype .tl), one heading per day. */
function EventsTimeline({
  events,
  severity,
  bySeverity,
}: {
  events: NmEvent[];
  severity?: boolean;
  /** [severity] sorted by severity: one list (no day headings), the date on every row */
  bySeverity?: boolean;
}) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language === 'he' ? 'he-IL' : 'en-GB';
  if (!events.length) return <Empty>{t('events.none')}</Empty>;
  const timeFmt = new Intl.DateTimeFormat(locale, {
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  });
  const dayFmt = new Intl.DateTimeFormat(locale, {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
  });
  const full = new Intl.DateTimeFormat(locale, { dateStyle: 'short', timeStyle: 'medium' });
  if (bySeverity) {
    const dayTime = new Intl.DateTimeFormat(locale, {
      day: 'numeric',
      month: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    });
    return (
      <Timeline label={t('severity.sort.severity')}>
        {events.map((ev) => (
          <EventEntry
            key={ev.id}
            ev={ev}
            time={dayTime.format(new Date(ev.time))}
            timeTitle={full.format(new Date(ev.time))}
            severity={severity}
          />
        ))}
      </Timeline>
    );
  }
  return (
    <div className="grid">
      {groupByDay(events).map((d) => (
        <section key={d.day} aria-label={dayFmt.format(new Date(d.items[0].time))}>
          <h3 className="m-0 ps-[100px] pt-4 text-[13px] font-semibold text-ink-muted max-sm:ps-[84px]">
            {dayFmt.format(new Date(d.items[0].time))}
          </h3>
          <Timeline>
            {d.items.map((ev) => (
              <EventEntry
                key={ev.id}
                ev={ev}
                time={timeFmt.format(new Date(ev.time))}
                timeTitle={full.format(new Date(ev.time))}
                severity={severity}
              />
            ))}
          </Timeline>
        </section>
      ))}
    </div>
  );
}

export function EventsPage() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const tree = useTree();
  const get = (k: string) => params.get(k) ?? '';
  const num = (k: string) => (params.get(k) ? Number(params.get(k)) : undefined);
  const page = Math.max(1, Number(params.get('page') ?? 1) || 1);
  const incidentsOn = useAiFeature('ai_correlation');
  const [more, setMore] = useState(() => MORE.some((k) => !!params.get(k)));
  // [severity] filter (?severity=critical,major) and order (?sort=severity), only while ON
  const severityOn = useFeature('alert_severity');
  const sevFilter = severityOn ? parseSeverities(params.get('severity')) : [];
  const sort = severityOn && params.get('sort') === 'severity' ? 'severity' : 'time';

  const query: EventQuery = {
    kind: get('kind') || undefined,
    status: get('status') || undefined,
    group_id: num('group_id'),
    device_id: num('device_id'),
    sensor_id: num('sensor_id'),
    since: localToIso(get('since')),
    until: localToIso(get('until')),
    in_maintenance: get('in_maintenance') ? get('in_maintenance') === 'true' : undefined,
    ...(sevFilter.length ? { severity: severityCsv(sevFilter) } : {}),
    ...(sort === 'severity' ? { sort } : {}),
    page,
    page_size: PAGE_SIZE,
  };
  const q = useQuery({
    queryKey: qk.events(query),
    queryFn: () => endpoints.events(query),
    placeholderData: keepPreviousData,
  });

  const set = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v);
    else next.delete(k);
    if (k !== 'page') next.delete('page');
    setParams(next, { replace: true });
  };
  const moreCount = MORE.filter((k) => !!params.get(k)).length;

  if (incidentsOn && params.get('tab') === 'incidents')
    return (
      <div>
        <PageHeader title={t('events.title')} subtitle={t('incident.subtitle')} />
        <IncidentsTab />
      </div>
    );

  return (
    <div>
      <PageHeader title={t('events.title')} subtitle={t('events.subtitle')} />
      <div className="mb-[18px] grid gap-3.5">
        {incidentsOn && (
          <div className="flex">
            <EventsTabs />
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <FilterChips
            label={t('events.kind')}
            testId="event-kind-chips"
            value={get('kind') as EventKind | ''}
            onChange={(v) => set('kind', v)}
            options={[
              { value: '' as const, label: t('common.all') },
              ...EVENT_KINDS.map((k) => ({ value: k, label: t(`eventKind.${k}`) })),
            ]}
          />
          <button
            type="button"
            aria-expanded={more}
            aria-controls="events-more"
            onClick={() => setMore((m) => !m)}
            className={cx(
              'inline-flex h-8 items-center gap-1.5 rounded-full px-3.5 text-[13.5px] font-medium transition-colors',
              moreCount ? 'bg-accent-soft text-accent-ink' : 'text-ink-muted hover:text-ink',
            )}
          >
            {t('events.moreFilters')}
            {moreCount > 0 && <span className="num">({moreCount})</span>}
            <ChevronIcon
              size={14}
              className={cx('transition-transform', more ? '-rotate-90' : 'rotate-90')}
            />
          </button>
        </div>
        {severityOn && (
          <div
            className="flex flex-wrap items-center gap-x-3 gap-y-2"
            data-testid="event-severity-bar"
          >
            <span className="text-[13.5px] font-medium text-ink-muted">{t('severity.filter')}</span>
            <SeverityChips
              label={t('severity.filter')}
              testId="event-severity-filter"
              value={sevFilter}
              onChange={(v) => set('severity', severityCsv(v))}
            />
            <span className="ms-auto inline-flex flex-wrap items-center gap-2">
              <SegmentedControl
                size="sm"
                label={t('severity.sort.label')}
                value={sort}
                onChange={(v) => set('sort', v === 'severity' ? v : '')}
                options={[
                  { value: 'time', label: t('severity.sort.time') },
                  { value: 'severity', label: t('severity.sort.severity') },
                ]}
              />
              <SeverityMappingInfo />
            </span>
          </div>
        )}
        {more && (
          <div
            id="events-more"
            className="grid grid-cols-1 items-end gap-x-3 gap-y-2.5 rounded-lg bg-surface px-4 py-3.5 shadow-card sm:grid-cols-3 lg:grid-cols-[repeat(5,minmax(0,1fr))_auto] [&_input]:h-9 [&_input]:text-[13.5px] [&_select]:h-9 [&_select]:text-[13.5px]"
          >
            <Field label={t('events.newStatus')}>
              <Select value={get('status')} onChange={(e) => set('status', e.target.value)}>
                <option value="">{t('common.all')}</option>
                {STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {t(`status.${s}`)}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label={t('events.group')}>
              <Select value={get('group_id')} onChange={(e) => set('group_id', e.target.value)}>
                <option value="">{t('common.all')}</option>
                {flattenGroups(tree.data?.groups ?? []).map(({ group, depth }) => (
                  <option key={group.id} value={group.id}>
                    {'  '.repeat(depth)}
                    {group.name}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label={t('events.since')}>
              <Input
                type="datetime-local"
                value={get('since')}
                onChange={(e) => set('since', e.target.value)}
                dir="ltr"
              />
            </Field>
            <Field label={t('events.until')}>
              <Input
                type="datetime-local"
                value={get('until')}
                onChange={(e) => set('until', e.target.value)}
                dir="ltr"
              />
            </Field>
            <Field label={t('events.maintenance')}>
              <Select
                value={get('in_maintenance')}
                onChange={(e) => set('in_maintenance', e.target.value)}
              >
                <option value="">{t('common.all')}</option>
                <option value="true">{t('events.inMaintenance')}</option>
                <option value="false">{t('events.notInMaintenance')}</option>
              </Select>
            </Field>
            <div className="flex items-end">
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setParams(new URLSearchParams(), { replace: true })}
              >
                {t('common.clearFilters')}
              </Button>
            </div>
          </div>
        )}
        {(query.device_id || query.sensor_id) && (
          <p className="m-0 flex flex-wrap items-center gap-2 rounded-lg bg-accent-soft px-4 py-2.5 text-[13.5px] text-ink">
            {query.device_id && t('events.filteredDevice', { id: query.device_id })}{' '}
            {query.sensor_id && t('events.filteredSensor', { id: query.sensor_id })}
            <Link to="/events" className="ms-auto font-medium text-accent-ink hover:underline">
              {t('common.clearFilters')}
            </Link>
          </p>
        )}
      </div>
      <Card bodyClassName="px-[22px] pb-1.5 pt-0 max-sm:px-3">
        {q.isLoading && <Loading />}
        <ErrorBox error={q.error} className="my-3" />
        {q.data && (
          <EventsTimeline
            events={q.data.items}
            severity={severityOn}
            bySeverity={sort === 'severity'}
          />
        )}
      </Card>
      {q.data && (
        <Pagination
          page={q.data.page}
          pageSize={q.data.page_size}
          total={q.data.total}
          onPage={(p) => set('page', String(p))}
        />
      )}
    </div>
  );
}
