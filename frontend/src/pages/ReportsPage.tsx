import { useAiFeature } from '../components/assistant/common';
import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import { endpoints3, qk3 } from '../api/endpoints3';
import type { Language } from '../api/types';
import type { Report, ReportRun, ReportRunStatus, ReportSchedule } from '../api/types3';
import { REPORT_SCHEDULES, REPORT_SECTIONS } from '../api/types3';
import { DevicePicker, useDeviceNames } from '../components/Dependency';
import { ChevronIcon, EditIcon, PlayIcon, PlusIcon, TrashIcon } from '../components/icons';
import { Pagination } from '../components/Pagination';
import { useToast } from '../components/toast';
import {
  Button,
  Card,
  Checkbox,
  ConfirmDialog,
  cx,
  Empty,
  ErrorBox,
  Field,
  FieldGroup,
  Input,
  Loading,
  Modal,
  PageHeader,
  Select,
  Table,
  Textarea,
  td,
  th,
} from '../components/ui';
import { useAuth } from '../hooks/auth';
import { FeatureOffNotice, useFeatures } from '../hooks/features';
import { useLiveMessages } from '../hooks/live';
import { useTree } from '../hooks/queries';
import { formatDateTime } from '../lib/format';
import {
  dateToIso,
  defaultPeriod,
  deliveryLabel,
  moveItem,
  periodError,
  reportFormErrors,
  reportFormFrom,
  reportFormToInput,
  toggleSection,
  type ReportForm,
} from '../lib/reports';
import { flattenGroups } from '../lib/tree';

export function RunStatusBadge({ status }: { status: ReportRunStatus }) {
  const { t } = useTranslation();
  return (
    <span
      className={cx(
        'rounded px-1.5 py-0.5 text-xs font-medium',
        status === 'ok' && 'bg-status-up-soft text-status-up',
        status === 'partial' && 'bg-status-warning-soft text-status-warning',
        status === 'failed' && 'bg-status-down-soft text-status-down',
        status === 'running' && 'bg-accent-soft text-accent-ink',
      )}
    >
      {t(`reports.status.${status}`)}
    </span>
  );
}

function scheduleText(t: (k: string, o?: Record<string, unknown>) => string, r: Report): string {
  if (r.schedule === 'weekly')
    return t('reports.scheduleWeekly', {
      day: t(`reports.weekday.${r.weekday ?? 1}`),
      time: r.send_time ?? '',
    });
  if (r.schedule === 'monthly')
    return t('reports.scheduleMonthly', { day: r.month_day ?? 1, time: r.send_time ?? '' });
  return t('reports.scheduleNone');
}

function SectionsEditor({
  value,
  onChange,
}: {
  value: ReportForm['sections'];
  onChange: (v: ReportForm['sections']) => void;
}) {
  const { t } = useTranslation();
  const ordered = [...value, ...REPORT_SECTIONS.filter((s) => !value.includes(s))];
  return (
    <FieldGroup label={t('reports.sections')} hint={t('reports.sectionsHint')}>
      <ul className="space-y-1">
        {ordered.map((s) => {
          const i = value.indexOf(s);
          return (
            <li key={s} className="flex items-center gap-2">
              <Checkbox
                label={t(`reportSection.${s}`)}
                checked={i >= 0}
                onChange={(v) => onChange(toggleSection(value, s, v))}
              />
              {i >= 0 && (
                <span className="ms-auto inline-flex gap-1">
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={i === 0}
                    aria-label={t('reports.moveUp')}
                    onClick={() => onChange(moveItem(value, i, -1))}
                  >
                    <ChevronIcon size={12} className="-rotate-90" />
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={i === value.length - 1}
                    aria-label={t('reports.moveDown')}
                    onClick={() => onChange(moveItem(value, i, 1))}
                  >
                    <ChevronIcon size={12} className="rotate-90" />
                  </Button>
                </span>
              )}
            </li>
          );
        })}
      </ul>
    </FieldGroup>
  );
}

function ScopeEditor({ form, set }: { form: ReportForm; set: (p: Partial<ReportForm>) => void }) {
  const { t } = useTranslation();
  const { data } = useTree();
  const groups = useMemo(() => flattenGroups(data?.groups ?? []), [data]);
  const deviceName = useDeviceNames();
  const [pick, setPick] = useState<number | null>(null);
  return (
    <FieldGroup label={t('reports.scope')} hint={t('reports.scopeHint')}>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="max-h-40 space-y-1 overflow-y-auto rounded-md p-2 ring-1 ring-inset ring-line">
          {groups.map(({ group, depth }) => (
            <div key={group.id} style={{ paddingInlineStart: depth * 12 }}>
              <Checkbox
                label={group.name}
                checked={form.scope_group_ids.includes(group.id)}
                onChange={(v) =>
                  set({
                    scope_group_ids: v
                      ? [...form.scope_group_ids, group.id]
                      : form.scope_group_ids.filter((x) => x !== group.id),
                  })
                }
              />
            </div>
          ))}
        </div>
        <div className="space-y-2">
          <div className="flex gap-1">
            <DevicePicker
              value={pick}
              onChange={setPick}
              allowNone
              label={t('reports.addDevice')}
              exclude={form.scope_device_ids}
            />
            <Button
              size="sm"
              disabled={pick === null}
              onClick={() => {
                if (pick !== null) set({ scope_device_ids: [...form.scope_device_ids, pick] });
                setPick(null);
              }}
            >
              {t('reports.addDevice')}
            </Button>
          </div>
          <div className="flex flex-wrap gap-1">
            {form.scope_device_ids.map((id) => (
              <span
                key={id}
                className="inline-flex items-center gap-1 rounded bg-surface-sunken px-1.5 py-0.5 text-xs"
              >
                {deviceName(id) ?? `#${id}`}
                <button
                  type="button"
                  aria-label={t('common.delete')}
                  className="text-ink-muted hover:text-status-down"
                  onClick={() =>
                    set({ scope_device_ids: form.scope_device_ids.filter((x) => x !== id) })
                  }
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        </div>
      </div>
    </FieldGroup>
  );
}

function ReportModal({
  open,
  report,
  onClose,
}: {
  open: boolean;
  report: Report | null;
  onClose: () => void;
}) {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const lang: Language = i18n.language === 'he' ? 'he' : 'en';
  const [form, setForm] = useState<ReportForm>(() => reportFormFrom(null, lang));
  const [touched, setTouched] = useState(false);
  // [3b] AI summary opt-in (shown while ai_report_summary is ON)
  const aiOn = useAiFeature('ai_report_summary');
  const [aiSummary, setAiSummary] = useState(false);
  const channels = useQuery({ queryKey: qk.channels, queryFn: endpoints.channels, enabled: open });
  useEffect(() => {
    if (!open) return;
    setForm(reportFormFrom(report, lang));
    setAiSummary(report?.ai_summary ?? false);
    setTouched(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, report]);
  const set = (p: Partial<ReportForm>) => setForm((f) => ({ ...f, ...p }));
  const errs = reportFormErrors(form);
  const m = useMutation({
    mutationFn: () =>
      report
        ? endpoints3.updateReport(report.id, {
            ...reportFormToInput(form),
            ...(aiOn ? { ai_summary: aiSummary } : {}),
          })
        : endpoints3.createReport({
            ...reportFormToInput(form),
            ...(aiOn ? { ai_summary: aiSummary } : {}),
          }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk3.reports });
      toast.push('success', t('common.saved'));
      onClose();
    },
  });
  const preview = useMutation({
    mutationFn: () => {
      const p = defaultPeriod(form.schedule, new Date());
      return endpoints3.previewReport(
        reportFormToInput(form),
        dateToIso(p.start) ?? undefined,
        dateToIso(p.end) ?? undefined,
      );
    },
    onSuccess: (run) => {
      onClose();
      navigate(`/reports/runs/${run.id}`);
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (!errs.length) m.mutate();
  };
  return (
    <Modal open={open} onClose={onClose} title={report ? t('reports.edit') : t('reports.add')} wide>
      <form onSubmit={submit} className="space-y-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label={t('common.name')} required className="sm:col-span-2">
            <Input
              value={form.name}
              onChange={(e) => set({ name: e.target.value })}
              maxLength={128}
              autoFocus
            />
          </Field>
          <Field label={t('common.language')}>
            <Select
              value={form.language}
              onChange={(e) => set({ language: e.target.value as Language })}
            >
              <option value="he">עברית</option>
              <option value="en">English</option>
            </Select>
          </Field>
        </div>
        <ScopeEditor form={form} set={set} />
        <SectionsEditor value={form.sections} onChange={(sections) => set({ sections })} />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label={t('reports.schedule')}>
            <Select
              value={form.schedule}
              onChange={(e) => set({ schedule: e.target.value as ReportSchedule })}
            >
              {REPORT_SCHEDULES.map((s) => (
                <option key={s} value={s}>
                  {t(`reports.schedules.${s}`)}
                </option>
              ))}
            </Select>
          </Field>
          {form.schedule === 'weekly' && (
            <Field label={t('reports.weekdayLabel')}>
              <Select
                value={form.weekday}
                onChange={(e) => set({ weekday: Number(e.target.value) })}
              >
                {[1, 2, 3, 4, 5, 6, 7].map((d) => (
                  <option key={d} value={d}>
                    {t(`reports.weekday.${d}`)}
                  </option>
                ))}
              </Select>
            </Field>
          )}
          {form.schedule === 'monthly' && (
            <Field label={t('reports.monthDay')} hint={t('reports.monthDayHint')}>
              <Input
                type="number"
                min={1}
                max={28}
                value={form.month_day}
                onChange={(e) => set({ month_day: Number(e.target.value) })}
                dir="ltr"
              />
            </Field>
          )}
          {form.schedule !== 'none' && (
            <Field label={t('reports.sendTime')}>
              <Input
                type="time"
                value={form.send_time}
                onChange={(e) => set({ send_time: e.target.value })}
                dir="ltr"
              />
            </Field>
          )}
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label={t('reports.emailTo')} hint={t('reports.emailToHint')}>
            <Textarea
              rows={3}
              value={form.email_to}
              onChange={(e) => set({ email_to: e.target.value })}
              dir="ltr"
            />
          </Field>
          <FieldGroup label={t('reports.channels')}>
            <div className="max-h-28 space-y-1 overflow-y-auto">
              {(channels.data?.items ?? []).map((c) => (
                <Checkbox
                  key={c.id}
                  label={`${c.name} (${t(`channels.${c.type}`)})`}
                  checked={form.channel_ids.includes(c.id)}
                  onChange={(v) =>
                    set({
                      channel_ids: v
                        ? [...form.channel_ids, c.id]
                        : form.channel_ids.filter((x) => x !== c.id),
                    })
                  }
                />
              ))}
              {channels.data && !channels.data.items.length && (
                <p className="text-xs text-ink-muted">{t('reports.noChannels')}</p>
              )}
            </div>
          </FieldGroup>
        </div>
        {aiOn && (
          <div>
            <Checkbox label={t('reports.aiSummary')} checked={aiSummary} onChange={setAiSummary} />
            <p className="ms-6 mt-0.5 text-xs text-slate-500">{t('reports.aiSummaryHint')}</p>
          </div>
        )}
        <Checkbox
          label={t('common.enabled')}
          checked={form.enabled}
          onChange={(v) => set({ enabled: v })}
        />
        {touched && errs.length > 0 && (
          <ul className="text-xs text-status-down">
            {errs.map((e) => (
              <li key={e}>{t(e)}</li>
            ))}
          </ul>
        )}
        <ErrorBox error={m.error ?? preview.error} />
        <div className="flex flex-wrap justify-end gap-2">
          <Button
            className="me-auto"
            busy={preview.isPending}
            disabled={!!errs.length}
            onClick={() => preview.mutate()}
          >
            {t('reports.preview')}
          </Button>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="submit" variant="primary" busy={m.isPending}>
            {t('common.save')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function RunModal({ report, onClose }: { report: Report | null; onClose: () => void }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useToast();
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [send, setSend] = useState(false);
  useEffect(() => {
    if (!report) return;
    const p = defaultPeriod(report.schedule, new Date());
    setStart(p.start);
    setEnd(p.end);
    setSend(false);
  }, [report]);
  const err = periodError(start, end);
  const m = useMutation({
    mutationFn: () =>
      endpoints3.runReport((report as Report).id, {
        period_start: dateToIso(start) ?? undefined,
        period_end: dateToIso(end) ?? undefined,
        send,
      }),
    onSuccess: (run) => {
      toast.push('success', t('reports.started'));
      onClose();
      navigate(`/reports/runs/${run.id}`);
    },
  });
  return (
    <Modal
      open={!!report}
      onClose={onClose}
      title={t('reports.runTitle', { name: report?.name ?? '' })}
      footer={
        <>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button variant="primary" busy={m.isPending} disabled={!!err} onClick={() => m.mutate()}>
            {t('reports.runNow')}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <Field label={t('reports.periodStart')}>
            <Input type="date" value={start} onChange={(e) => setStart(e.target.value)} dir="ltr" />
          </Field>
          <Field label={t('reports.periodEnd')} hint={t('reports.periodEndHint')}>
            <Input type="date" value={end} onChange={(e) => setEnd(e.target.value)} dir="ltr" />
          </Field>
        </div>
        {err && <p className="text-xs text-status-down">{t(err)}</p>}
        <Checkbox label={t('reports.sendToRecipients')} checked={send} onChange={setSend} />
        <ErrorBox error={m.error} />
      </div>
    </Modal>
  );
}

export function DeliveriesCell({ run }: { run: ReportRun }) {
  const { t } = useTranslation();
  const channels = useQuery({
    queryKey: qk.channels,
    queryFn: endpoints.channels,
    staleTime: 60_000,
  });
  const name = (id: number) => channels.data?.items.find((c) => c.id === id)?.name;
  if (!run.deliveries.length)
    return <span className="text-xs text-ink-muted">{t('reports.notSent')}</span>;
  return (
    <ul className="space-y-0.5 text-xs">
      {run.deliveries.map((d, i) => (
        <li
          key={i}
          className={d.ok ? 'text-status-up' : 'text-status-down'}
          title={d.error ?? undefined}
        >
          {d.ok ? '✓' : '✗'} <bdi dir="ltr">{deliveryLabel(d.target, name)}</bdi>
          {d.error && <span className="ms-1 text-ink-muted">— {d.error}</span>}
        </li>
      ))}
    </ul>
  );
}

function RunsCard() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const toast = useToast();
  const [page, setPage] = useState(1);
  const q = useQuery({
    queryKey: qk3.reportRuns({ page }),
    queryFn: () => endpoints3.reportRuns({ page }),
    placeholderData: keepPreviousData,
  });
  const resend = useMutation({
    mutationFn: (id: number) => endpoints3.resendReport(id),
    onSuccess: () => {
      toast.push('success', t('reports.resent'));
      void qc.invalidateQueries({ queryKey: ['report-runs'] });
    },
  });
  return (
    <Card title={t('reports.runs')} bodyClassName="p-0">
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error ?? resend.error} className="m-3" />
      {q.data && !q.data.items.length && <Empty>{t('reports.noRuns')}</Empty>}
      {q.data && q.data.items.length > 0 && (
        <>
          <Table>
            <thead>
              <tr>
                <th className={th}>{t('reports.report')}</th>
                <th className={th}>{t('reports.period')}</th>
                <th className={th}>{t('reports.trigger')}</th>
                <th className={th}>{t('common.status')}</th>
                <th className={th}>{t('reports.deliveries')}</th>
                <th className={th} />
              </tr>
            </thead>
            <tbody>
              {q.data.items.map((r) => (
                <tr key={r.id}>
                  <td className={td}>
                    <Link
                      to={`/reports/runs/${r.id}`}
                      className="font-medium text-accent-ink hover:underline"
                    >
                      {r.report_name}
                    </Link>
                    <div className="text-xs text-ink-muted">{formatDateTime(r.started_at)}</div>
                  </td>
                  <td className={`${td} text-xs`}>
                    {formatDateTime(r.period_start)} – {formatDateTime(r.period_end)}
                  </td>
                  <td className={td}>
                    {t(`reports.triggers.${r.trigger}`)}
                    {r.created_by && (
                      <span className="ms-1 text-xs text-ink-muted">({r.created_by})</span>
                    )}
                  </td>
                  <td className={td}>
                    <RunStatusBadge status={r.status} />
                  </td>
                  <td className={td}>
                    <DeliveriesCell run={r} />
                  </td>
                  <td className={`${td} text-end`}>
                    {isAdmin && r.deliveries.some((d) => !d.ok) && r.status !== 'running' && (
                      <Button size="sm" busy={resend.isPending} onClick={() => resend.mutate(r.id)}>
                        {t('reports.resendFailed')}
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
          <Pagination
            page={page}
            pageSize={q.data.page_size}
            total={q.data.total}
            onPage={setPage}
          />
        </>
      )}
    </Card>
  );
}

/** [3] Reports (/reports): definitions, run now, runs history. */
export function ReportsPage() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const { features } = useFeatures();
  const qc = useQueryClient();
  const toast = useToast();
  const on = features.reports;
  const q = useQuery({ queryKey: qk3.reports, queryFn: endpoints3.reports, enabled: on });
  const [editing, setEditing] = useState<Report | null | undefined>(undefined);
  const [running, setRunning] = useState<Report | null>(null);
  const [del, setDel] = useState<Report | null>(null);
  useLiveMessages(['report.run'], () => {
    void qc.invalidateQueries({ queryKey: ['report-runs'] });
    void qc.invalidateQueries({ queryKey: qk3.reports });
  });
  const remove = useMutation({
    mutationFn: (id: number) => endpoints3.deleteReport(id),
    onSuccess: () => {
      setDel(null);
      toast.push('success', t('common.deleted'));
      void qc.invalidateQueries({ queryKey: qk3.reports });
    },
  });
  return (
    <div className="space-y-4">
      <PageHeader
        title={t('reports.title')}
        actions={
          on &&
          isAdmin && (
            <Button variant="primary" onClick={() => setEditing(null)}>
              <PlusIcon size={14} /> {t('reports.add')}
            </Button>
          )
        }
      />
      {!on && <FeatureOffNotice feature="reports" />}
      {on && (
        <>
          <Card title={t('reports.definitions')} bodyClassName="p-0">
            {q.isLoading && <Loading />}
            <ErrorBox error={q.error} className="m-3" />
            {q.data && !q.data.items.length && <Empty>{t('reports.empty')}</Empty>}
            {q.data && q.data.items.length > 0 && (
              <Table>
                <thead>
                  <tr>
                    <th className={th}>{t('common.name')}</th>
                    <th className={th}>{t('reports.schedule')}</th>
                    <th className={th}>{t('reports.nextRun')}</th>
                    <th className={th}>{t('reports.lastRun')}</th>
                    <th className={th}>{t('reports.recipients')}</th>
                    {isAdmin && <th className={th} />}
                  </tr>
                </thead>
                <tbody>
                  {q.data.items.map((r) => (
                    <tr key={r.id} className={r.enabled ? undefined : 'opacity-60'}>
                      <td className={td}>
                        <span className="font-medium">{r.name}</span>
                        <div className="text-xs text-ink-muted">
                          {r.sections.map((s) => t(`reportSection.${s}`)).join(' · ')}
                        </div>
                      </td>
                      <td className={td}>{scheduleText(t, r)}</td>
                      <td className={td}>
                        {r.enabled ? formatDateTime(r.next_run_at) : t('reports.disabled')}
                      </td>
                      <td className={td}>
                        {r.last_run ? (
                          <Link
                            to={`/reports/runs/${r.last_run.id}`}
                            className="inline-flex items-center gap-1"
                          >
                            <RunStatusBadge status={r.last_run.status} />
                            <span className="text-xs text-ink-muted">
                              {formatDateTime(r.last_run.finished_at)}
                            </span>
                          </Link>
                        ) : (
                          '—'
                        )}
                      </td>
                      <td className={`${td} text-xs`}>
                        {t('reports.recipientsCount', {
                          emails: r.email_to.length,
                          channels: r.channel_ids.length,
                        })}
                      </td>
                      {isAdmin && (
                        <td className={`${td} text-end`}>
                          <span className="inline-flex gap-1">
                            <Button size="sm" onClick={() => setRunning(r)}>
                              <PlayIcon size={12} className="rtl:rotate-180" />{' '}
                              {t('reports.runNow')}
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              aria-label={t('common.edit')}
                              onClick={() => setEditing(r)}
                            >
                              <EditIcon size={14} />
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              aria-label={t('common.delete')}
                              onClick={() => setDel(r)}
                            >
                              <TrashIcon size={14} />
                            </Button>
                          </span>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </Table>
            )}
          </Card>
          <RunsCard />
        </>
      )}
      <ReportModal
        open={editing !== undefined}
        report={editing ?? null}
        onClose={() => setEditing(undefined)}
      />
      <RunModal report={running} onClose={() => setRunning(null)} />
      <ConfirmDialog
        open={!!del}
        title={t('reports.deleteTitle')}
        message={t('reports.deleteConfirm', { name: del?.name ?? '' })}
        onConfirm={() => del && remove.mutate(del.id)}
        onClose={() => setDel(null)}
        busy={remove.isPending}
        error={remove.error}
      />
    </div>
  );
}
