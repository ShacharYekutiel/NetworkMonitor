import { useState, type FormEvent } from 'react';
import type { TFunction } from 'i18next';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { endpoints, qk, type AuditQuery } from '../api/endpoints';
import type { AuditEntry } from '../api/types';
import { AuditIcon, CogIcon, KeyIcon } from '../components/icons';
import { ActionRow, Avatar, EmptyState } from '../components/kit';
import { Pagination } from '../components/Pagination';
import {
  buttonClass,
  Button,
  Card,
  cx,
  ErrorBox,
  GroupList,
  Input,
  KeyValue,
  Loading,
  PageHeader,
  Select,
  TAG,
  TAG_TONE,
} from '../components/ui';
import { FeatureOffNotice, useFeatures } from '../hooks/features';
import { AUDIT_ACTION_PREFIXES, AUDIT_ENTITY_TYPES, auditTone, changeRows } from '../lib/audit';
import { formatDateTime, formatShortTime } from '../lib/format';
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
  if (!rows.length) return <p className="m-0 text-sm text-ink-muted">{t('audit.noChanges')}</p>;
  return (
    <dl className="m-0 grid gap-0 text-[14px]" data-testid="audit-diff">
      {rows.map((r) => (
        <div
          key={r.field}
          className="grid gap-1 border-b border-line py-2.5 last:border-b-0 sm:grid-cols-[minmax(0,140px)_minmax(0,1fr)] sm:gap-4"
        >
          <dt className="min-w-0 break-words font-mono text-[12.5px] text-ink-muted">
            <bdi dir="ltr">{r.field}</bdi>
          </dt>
          <dd className="m-0 flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1 font-mono text-[12.5px]">
            <bdi
              dir="ltr"
              className="break-all rounded-md bg-status-down-soft px-1.5 text-status-down"
            >
              {r.secret ? t('audit.secret') : r.old}
            </bdi>
            <span aria-hidden="true" className="text-ink-subtle rtl:rotate-180">
              →
            </span>
            <span className="sr-only">{t('audit.new')}</span>
            <bdi dir="ltr" className="break-all rounded-md bg-status-up-soft px-1.5 text-status-up">
              {r.secret ? t('audit.secret') : r.new}
            </bdi>
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** "Alert rules · updated" from "alert_rule.update" (unknown verbs as words). */
function actionTitle(t: TFunction, action: string): string {
  const dot = action.indexOf('.');
  const group = dot > 0 ? action.slice(0, dot) : action;
  const verb = dot > 0 ? action.slice(dot + 1) : '';
  // sign-ins read as one word ("Signed in"), the rest as "Alert rules · changed"
  if (group === 'auth' && verb) return t(`audit.authVerb.${verb}`, { defaultValue: action });
  const g = t(`audit.actionGroup.${group}`, { defaultValue: group });
  if (!verb) return g;
  return `${g} · ${t(`audit.verb.${verb}`, { defaultValue: verb.replace(/_/g, ' ') })}`;
}

function AuditDetail({ e }: { e: AuditEntry }) {
  const { t } = useTranslation();
  const tone = auditTone(e.action);
  return (
    <Card
      title={
        <span className="grid gap-1">
          <span>
            {actionTitle(t, e.action)}
            {e.entity_name && e.entity_name !== e.actor_name && (
              <>
                {' · '}
                <bdi dir="auto">{e.entity_name}</bdi>
              </>
            )}
          </span>
          <bdi dir="ltr" className="font-mono text-[12.5px] font-normal text-ink-muted">
            {e.action}
          </bdi>
        </span>
      }
      actions={
        tone === 'bad' ? (
          <span className={cx(TAG, TAG_TONE.down)}>{t('audit.failed')}</span>
        ) : tone === 'warn' ? (
          <span className={cx(TAG, TAG_TONE.warning)}>{t('audit.removal')}</span>
        ) : undefined
      }
    >
      <div className="grid gap-4" data-testid="audit-detail">
        {e.reason && (
          <p className="m-0 text-sm">{t(`ldap.reason.${e.reason}`, { defaultValue: e.reason })}</p>
        )}
        <Changes e={e} />
        <KeyValue
          className="border-t border-line pt-3.5"
          items={[
            {
              k: t('audit.actor'),
              v: (
                <>
                  <bdi dir="auto">{e.actor_name || '—'}</bdi>
                  <span className="text-ink-muted">
                    {' '}
                    ({t(`audit.actorType.${e.actor_type}`)}
                    {e.source && ` · ${t(`audit.source.${e.source}`)}`})
                  </span>
                </>
              ),
            },
            ...(e.entity_type
              ? [
                  {
                    k: t('audit.entity'),
                    v: t(`audit.entityType.${e.entity_type}`, { defaultValue: e.entity_type }),
                  },
                ]
              : []),
            { k: t('logs.col.time'), v: formatDateTime(e.time) },
            { k: t('audit.ip'), v: <bdi dir="ltr">{e.ip ?? '—'}</bdi> },
          ]}
        />
      </div>
    </Card>
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
  const settings = useQuery({ queryKey: qk.settings, queryFn: endpoints.settings });
  const shown = q.data?.items.find((e) => e.id === open) ?? q.data?.items[0];
  if (!loaded) return <Loading />;
  return (
    <div>
      <PageHeader
        title={t('audit.title')}
        subtitle={
          deviceId !== undefined
            ? t('audit.filteredDevice', { id: deviceId })
            : settings.data && t('audit.subtitle', { days: settings.data.audit_retention_days })
        }
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
          <Card bodyClassName="px-5 py-4" className="mb-6">
            <form
              onSubmit={submit}
              className="grid grid-cols-1 items-end gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-[minmax(230px,1.3fr)_minmax(230px,1.3fr)_repeat(4,minmax(0,1fr))_auto]"
              data-testid="audit-filters"
            >
              <label className="grid min-w-0 gap-1.5 text-[13px] text-ink-muted">
                {t('logs.from')}
                <Input
                  type="datetime-local"
                  value={form.since}
                  onChange={(e) => set({ since: e.target.value })}
                  dir="ltr"
                />
              </label>
              <label className="grid min-w-0 gap-1.5 text-[13px] text-ink-muted">
                {t('logs.to')}
                <Input
                  type="datetime-local"
                  value={form.until}
                  onChange={(e) => set({ until: e.target.value })}
                  dir="ltr"
                />
              </label>
              <div className="grid min-w-0 gap-1.5 text-[13px] text-ink-muted">
                <span aria-hidden="true">{t('audit.actor')}</span>
                <Input
                  value={form.actor}
                  onChange={(e) => set({ actor: e.target.value })}
                  aria-label={t('audit.actor')}
                  dir="auto"
                />
              </div>
              <div className="grid min-w-0 gap-1.5 text-[13px] text-ink-muted">
                <span aria-hidden="true">{t('audit.action')}</span>
                <Select
                  value={form.action}
                  onChange={(e) => set({ action: e.target.value })}
                  aria-label={t('audit.action')}
                >
                  <option value="">{t('audit.anyAction')}</option>
                  {AUDIT_ACTION_PREFIXES.map((a) => (
                    <option key={a} value={a}>
                      {t(`audit.actionGroup.${a.slice(0, -1)}`)}
                    </option>
                  ))}
                </Select>
              </div>
              <div className="grid min-w-0 gap-1.5 text-[13px] text-ink-muted">
                <span aria-hidden="true">{t('audit.entity')}</span>
                <Select
                  value={form.entity_type}
                  onChange={(e) => set({ entity_type: e.target.value })}
                  aria-label={t('audit.entity')}
                >
                  <option value="">{t('audit.anyEntity')}</option>
                  {AUDIT_ENTITY_TYPES.map((x) => (
                    <option key={x} value={x}>
                      {t(`audit.entityType.${x}`)}
                    </option>
                  ))}
                </Select>
              </div>
              <div className="grid min-w-0 gap-1.5 text-[13px] text-ink-muted">
                <span aria-hidden="true">{t('audit.ip')}</span>
                <Input
                  value={form.ip}
                  onChange={(e) => set({ ip: e.target.value })}
                  aria-label={t('audit.ip')}
                  className="font-mono"
                  dir="ltr"
                />
              </div>
              <Button type="submit" variant="primary">
                {t('logs.apply')}
              </Button>
            </form>
          </Card>
          {q.isLoading && <Loading />}
          <ErrorBox error={q.error} className="mb-3" />
          {q.data && !q.data.items.length && (
            <div className="rounded-xl bg-surface shadow-card">
              <EmptyState icon={AuditIcon} title={t('audit.none')}>
                {t('audit.noneHint')}
              </EmptyState>
            </div>
          )}
          {q.data && q.data.items.length > 0 && (
            // prototype .two: the entries and the selected entry's changes
            <div className="grid grid-cols-1 items-start gap-6 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
              <div className="min-w-0">
                <GroupList label={t('audit.title')}>
                  {q.data.items.map((e) => {
                    const tone = auditTone(e.action);
                    return (
                      <ActionRow
                        key={e.id}
                        testId="audit-row"
                        wrap
                        selected={shown?.id === e.id}
                        lead={
                          e.actor_type === 'user' ? (
                            <Avatar name={e.actor_name || '?'} size="sm" />
                          ) : (
                            <span className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-chip text-ink-muted">
                              {e.actor_type === 'token' ? (
                                <KeyIcon size={16} />
                              ) : (
                                <CogIcon size={16} />
                              )}
                            </span>
                          )
                        }
                        onClick={() => setOpen(e.id)}
                        title={
                          <span
                            className={cx(
                              tone === 'bad' && 'text-status-down',
                              tone === 'warn' && 'text-status-warning',
                            )}
                          >
                            {actionTitle(t, e.action)}
                            {e.entity_name && e.entity_name !== e.actor_name && (
                              <>
                                {' · '}
                                <bdi dir="auto">{e.entity_name}</bdi>
                              </>
                            )}
                          </span>
                        }
                        subtitle={
                          <>
                            <bdi dir="auto" className="font-display">
                              {e.actor_name || '—'}
                            </bdi>
                            {e.actor_type !== 'user' &&
                              ` · ${t(`audit.actorType.${e.actor_type}`)}`}
                            {e.ip && (
                              <>
                                {' · '}
                                <bdi dir="ltr" className="font-mono text-[12.5px]">
                                  {e.ip}
                                </bdi>
                              </>
                            )}
                            {e.reason
                              ? ` · ${t(`ldap.reason.${e.reason}`, { defaultValue: e.reason })}`
                              : Object.keys(e.changes ?? {}).length > 0 &&
                                ` · ${t('audit.changesCount', { n: Object.keys(e.changes ?? {}).length })}`}
                          </>
                        }
                        end={
                          <span
                            className="num whitespace-nowrap text-sm font-medium text-ink-muted"
                            title={formatDateTime(e.time)}
                          >
                            {formatShortTime(e.time)}
                          </span>
                        }
                      />
                    );
                  })}
                </GroupList>
                <Pagination
                  page={page}
                  pageSize={PAGE_SIZE}
                  total={q.data.total}
                  onPage={setPage}
                />
              </div>
              <div className="min-w-0 lg:sticky lg:top-[84px]">
                {shown && <AuditDetail e={shown} />}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
