import { Fragment, useState, type FormEvent } from 'react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { endpoints, qk, type AuditQuery } from '../api/endpoints';
import type { AuditEntry } from '../api/types';
import { ChevronIcon } from '../components/icons';
import { Pagination } from '../components/Pagination';
import {
  buttonClass,
  Button,
  Card,
  cx,
  Empty,
  ErrorBox,
  Input,
  Loading,
  PageHeader,
  Select,
  td,
  th,
} from '../components/ui';
import { FeatureOffNotice, useFeatures } from '../hooks/features';
import { AUDIT_ACTION_PREFIXES, AUDIT_ENTITY_TYPES, auditTone, changeRows } from '../lib/audit';
import { formatDateTime } from '../lib/format';
import { isoToLocal, localToIso } from '../lib/logs';

const PAGE_SIZE = 50;

type Form = {
  since: string;
  until: string;
  actor: string;
  action: string;
  entity_type: string;
  ip: string;
};

function Changes({ e }: { e: AuditEntry }) {
  const { t } = useTranslation();
  const rows = changeRows(e.changes);
  if (!rows.length) return <p className="text-xs text-ink-muted">{t('audit.noChanges')}</p>;
  return (
    <table className="w-full text-xs" data-testid="audit-diff">
      <thead>
        <tr>
          <th className="px-2 py-1 text-start font-medium text-ink-muted">{t('audit.field')}</th>
          <th className="px-2 py-1 text-start font-medium text-ink-muted">{t('audit.old')}</th>
          <th className="px-2 py-1 text-start font-medium text-ink-muted">{t('audit.new')}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.field} className="border-t border-line">
            <td className="px-2 py-1 font-mono">
              <bdi dir="ltr">{r.field}</bdi>
            </td>
            <td className="px-2 py-1 font-mono text-status-down">
              <bdi dir="ltr" className="break-all">
                {r.secret ? t('audit.secret') : r.old}
              </bdi>
            </td>
            <td className="px-2 py-1 font-mono text-status-up">
              <bdi dir="ltr" className="break-all">
                {r.secret ? t('audit.secret') : r.new}
              </bdi>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** [2b-3] Audit log (/audit, admin): filters, table with diff rows, CSV export with the same filters. */
export function AuditPage() {
  const { t } = useTranslation();
  const { features, loaded } = useFeatures();
  const [params] = useSearchParams();
  const deviceId = params.get('device_id') ? Number(params.get('device_id')) : undefined;
  const [form, setForm] = useState<Form>(() => ({
    since: isoToLocal(new Date(Date.now() - 7 * 86_400_000).toISOString()),
    until: '',
    actor: '',
    action: '',
    entity_type: '',
    ip: '',
  }));
  const [applied, setApplied] = useState<Form>(form);
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState<number | null>(null);
  const query: AuditQuery = {
    since: localToIso(applied.since) ?? undefined,
    until: localToIso(applied.until) ?? undefined,
    actor: applied.actor.trim() || undefined,
    action: applied.action || undefined,
    entity_type: applied.entity_type || undefined,
    ip: applied.ip.trim() || undefined,
    device_id: deviceId,
    page,
    page_size: PAGE_SIZE,
  };
  const q = useQuery({
    queryKey: qk.audit(query),
    queryFn: () => endpoints.audit(query),
    enabled: features.audit,
    placeholderData: keepPreviousData,
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setApplied(form);
    setPage(1);
  };
  const set = (p: Partial<Form>) => setForm((f) => ({ ...f, ...p }));
  if (!loaded) return <Loading />;
  return (
    <div className="space-y-4">
      <PageHeader
        title={t('audit.title')}
        subtitle={deviceId !== undefined ? t('audit.filteredDevice', { id: deviceId }) : undefined}
        actions={
          features.audit && (
            <a
              className={buttonClass()}
              href={endpoints.auditExportUrl(query)}
              download
              data-testid="audit-export"
            >
              {t('audit.export')}
            </a>
          )
        }
      />
      {!features.audit ? (
        <FeatureOffNotice feature="audit" />
      ) : (
        <>
          <Card bodyClassName="p-3">
            <form
              onSubmit={submit}
              className="flex flex-wrap items-end gap-2"
              data-testid="audit-filters"
            >
              <label className="text-xs text-ink-muted">
                {t('logs.from')}
                <Input
                  type="datetime-local"
                  value={form.since}
                  onChange={(e) => set({ since: e.target.value })}
                  dir="ltr"
                  className="w-auto"
                />
              </label>
              <label className="text-xs text-ink-muted">
                {t('logs.to')}
                <Input
                  type="datetime-local"
                  value={form.until}
                  onChange={(e) => set({ until: e.target.value })}
                  dir="ltr"
                  className="w-auto"
                />
              </label>
              <Input
                value={form.actor}
                onChange={(e) => set({ actor: e.target.value })}
                placeholder={t('audit.actor')}
                aria-label={t('audit.actor')}
                className="w-40"
                dir="auto"
              />
              <Select
                value={form.action}
                onChange={(e) => set({ action: e.target.value })}
                aria-label={t('audit.action')}
                className="w-48"
              >
                <option value="">{t('audit.anyAction')}</option>
                {AUDIT_ACTION_PREFIXES.map((a) => (
                  <option key={a} value={a}>
                    {t(`audit.actionGroup.${a.slice(0, -1)}`)}
                  </option>
                ))}
              </Select>
              <Select
                value={form.entity_type}
                onChange={(e) => set({ entity_type: e.target.value })}
                aria-label={t('audit.entity')}
                className="w-44"
              >
                <option value="">{t('audit.anyEntity')}</option>
                {AUDIT_ENTITY_TYPES.map((x) => (
                  <option key={x} value={x}>
                    {t(`audit.entityType.${x}`)}
                  </option>
                ))}
              </Select>
              <Input
                value={form.ip}
                onChange={(e) => set({ ip: e.target.value })}
                placeholder={t('audit.ip')}
                aria-label={t('audit.ip')}
                className="w-36 font-mono"
                dir="ltr"
              />
              <Button type="submit" variant="primary" size="sm">
                {t('logs.apply')}
              </Button>
            </form>
          </Card>
          <Card bodyClassName="p-0">
            {q.isLoading && <Loading />}
            <ErrorBox error={q.error} className="m-3" />
            {q.data && !q.data.items.length && <Empty>{t('audit.none')}</Empty>}
            {q.data && q.data.items.length > 0 && (
              <>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr>
                        <th className={th} />
                        <th className={th}>{t('logs.col.time')}</th>
                        <th className={th}>{t('audit.actor')}</th>
                        <th className={th}>{t('audit.action')}</th>
                        <th className={th}>{t('audit.entity')}</th>
                        <th className={th}>{t('audit.ip')}</th>
                        <th className={th}>{t('audit.details')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {q.data.items.map((e) => {
                        const tone = auditTone(e.action);
                        const expanded = open === e.id;
                        return (
                          <Fragment key={e.id}>
                            <tr
                              className="cursor-pointer hover:bg-surface-hover"
                              onClick={() => setOpen(expanded ? null : e.id)}
                              aria-expanded={expanded}
                            >
                              <td className={td}>
                                <ChevronIcon
                                  size={12}
                                  className={cx(
                                    'transition-transform',
                                    expanded ? 'rotate-90' : 'rtl:rotate-180',
                                  )}
                                />
                              </td>
                              <td className={cx(td, 'whitespace-nowrap text-xs text-ink-muted')}>
                                {formatDateTime(e.time)}
                              </td>
                              <td className={cx(td, 'whitespace-nowrap')}>
                                <bdi dir="auto">{e.actor_name || '—'}</bdi>
                                <span className="ms-1 text-xs text-ink-muted">
                                  ({t(`audit.actorType.${e.actor_type}`)}
                                  {e.source && ` · ${t(`audit.source.${e.source}`)}`})
                                </span>
                              </td>
                              <td
                                className={cx(
                                  td,
                                  'whitespace-nowrap font-mono text-xs',
                                  tone === 'bad' && 'text-status-down',
                                  tone === 'warn' && 'text-status-warning',
                                )}
                              >
                                <bdi dir="ltr">{e.action}</bdi>
                              </td>
                              <td className={td}>
                                {e.entity_type ? (
                                  <span>
                                    {t(`audit.entityType.${e.entity_type}`, {
                                      defaultValue: e.entity_type,
                                    })}
                                    {e.entity_name && (
                                      <>
                                        : <bdi dir="auto">{e.entity_name}</bdi>
                                      </>
                                    )}
                                  </span>
                                ) : (
                                  '—'
                                )}
                              </td>
                              <td className={cx(td, 'font-mono text-xs')}>
                                <bdi dir="ltr">{e.ip ?? '—'}</bdi>
                              </td>
                              <td className={cx(td, 'text-xs text-ink-muted')}>
                                {e.reason
                                  ? t(`ldap.reason.${e.reason}`, { defaultValue: e.reason })
                                  : t('audit.changesCount', {
                                      n: Object.keys(e.changes ?? {}).length,
                                    })}
                              </td>
                            </tr>
                            {expanded && (
                              <tr>
                                <td />
                                <td colSpan={6} className="bg-surface-sunken px-3 py-2">
                                  <Changes e={e} />
                                </td>
                              </tr>
                            )}
                          </Fragment>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                <Pagination
                  page={page}
                  pageSize={PAGE_SIZE}
                  total={q.data.total}
                  onPage={setPage}
                />
              </>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
