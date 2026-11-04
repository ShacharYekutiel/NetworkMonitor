import { useSearchParams } from 'react-router-dom';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk, type EventQuery } from '../api/endpoints';
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

  const query: EventQuery = {
    kind: get('kind') || undefined,
    status: get('status') || undefined,
    group_id: num('group_id'),
    device_id: num('device_id'),
    sensor_id: num('sensor_id'),
    since: localToIso(get('since')),
    until: localToIso(get('until')),
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

  return (
    <div className="space-y-4">
      <PageHeader title={t('events.title')} />
      <Card>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <Field label={t('events.kind')}>
            <Select value={get('kind')} onChange={(e) => set('kind', e.target.value)}>
              <option value="">{t('common.all')}</option>
              {(['status_change', 'notification', 'system'] as const).map((k) => (
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
          <div className="flex items-end">
            <Button onClick={() => setParams(new URLSearchParams(), { replace: true })}>
              {t('common.clearFilters')}
            </Button>
          </div>
        </div>
        {(query.device_id || query.sensor_id) && (
          <p className="mt-2 text-xs text-slate-500">
            {query.device_id && t('events.filteredDevice', { id: query.device_id })}{' '}
            {query.sensor_id && t('events.filteredSensor', { id: query.sensor_id })}
          </p>
        )}
      </Card>
      <Card bodyClassName="p-0">
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
