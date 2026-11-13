import { useSearchParams } from 'react-router-dom';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk, type EventQuery } from '../api/endpoints';
import { EVENT_KINDS } from '../api/types';
import { EventsTable } from '../components/EventList';
import { Pagination } from '../components/Pagination';
import {
  Button,
  Card,
  ErrorBox,
  Field,
  Input,
  Loading,
  PageHeader,
  Select,
} from '../components/ui';
import { useTree } from '../hooks/queries';
import { flattenGroups } from '../lib/tree';
import { STATUSES } from '../lib/status';
import { useState } from 'react';
import { TabBar } from '../components/TabBar';
import { useAiFeature } from '../components/assistant/common';
import { IncidentList } from '../components/assistant/Incidents';
import { INCIDENT_STATES, type IncidentState } from '../api/types';

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

function IncidentsTab() {
  const { t } = useTranslation();
  const [state, setState] = useState<IncidentState | ''>('');
  const [page, setPage] = useState(1);
  return (
    <Card
      bodyClassName="p-0"
      actions={
        <Select
          value={state}
          onChange={(e) => {
            setState(e.target.value as IncidentState | '');
            setPage(1);
          }}
          className="w-auto"
          aria-label={t('incident.stateFilter')}
        >
          <option value="">{t('common.all')}</option>
          {INCIDENT_STATES.map((s) => (
            <option key={s} value={s}>
              {t(`incident.state.${s}`)}
            </option>
          ))}
        </Select>
      }
      title={t('incident.title')}
    >
      <IncidentList
        query={{ state: state ? [state] : undefined, page, page_size: 50 }}
        onPage={setPage}
      />
    </Card>
  );
}

const PAGE_SIZE = 50;

/** datetime-local value (local time) <-> ISO UTC */
function localToIso(v: string): string | undefined {
  if (!v) return undefined;
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

export function EventsPage() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const tree = useTree();
  const get = (k: string) => params.get(k) ?? '';
  const num = (k: string) => (params.get(k) ? Number(params.get(k)) : undefined);
  const page = Math.max(1, Number(params.get('page') ?? 1) || 1);
  const incidentsOn = useAiFeature('ai_correlation');

  const query: EventQuery = {
    kind: get('kind') || undefined,
    status: get('status') || undefined,
    group_id: num('group_id'),
    device_id: num('device_id'),
    sensor_id: num('sensor_id'),
    since: localToIso(get('since')),
    until: localToIso(get('until')),
    in_maintenance: get('in_maintenance') ? get('in_maintenance') === 'true' : undefined,
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

  if (incidentsOn && params.get('tab') === 'incidents')
    return (
      <div className="space-y-4">
        <PageHeader title={t('events.title')} />
        <EventsTabs />
        <IncidentsTab />
      </div>
    );

  return (
    <div className="space-y-4">
      <PageHeader title={t('events.title')} />
      {incidentsOn && <EventsTabs />}
      <Card bodyClassName="p-0">
        {/* Filter toolbar above the table (design system: DataTable toolbar). */}
        <div className="grid grid-cols-1 items-end gap-x-2 gap-y-2.5 border-b border-line px-3.5 py-2.5 sm:grid-cols-3 lg:grid-cols-[repeat(6,minmax(0,1fr))_auto] [&_input]:h-[30px] [&_input]:py-1 [&_input]:text-[13px] [&_select]:h-[30px] [&_select]:py-1 [&_select]:text-[13px] [&_label>span]:mb-1">
          <Field label={t('events.kind')}>
            <Select value={get('kind')} onChange={(e) => set('kind', e.target.value)}>
              <option value="">{t('common.all')}</option>
              {EVENT_KINDS.map((k) => (
                <option key={k} value={k}>
                  {t(`eventKind.${k}`)}
                </option>
              ))}
            </Select>
          </Field>
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
                  {'  '.repeat(depth)}
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
        {(query.device_id || query.sensor_id) && (
          <p className="border-b border-line bg-accent-soft px-3.5 py-2 text-xs text-ink">
            {query.device_id && t('events.filteredDevice', { id: query.device_id })}{' '}
            {query.sensor_id && t('events.filteredSensor', { id: query.sensor_id })}
          </p>
        )}
        {q.isLoading && <Loading />}
        <ErrorBox error={q.error} className="m-3" />
        {q.data && (
          <>
            <EventsTable events={q.data.items} />
            <Pagination
              page={q.data.page}
              pageSize={q.data.page_size}
              total={q.data.total}
              onPage={(p) => set('page', String(p))}
            />
          </>
        )}
      </Card>
    </div>
  );
}
