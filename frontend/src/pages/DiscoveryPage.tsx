import { useEffect, useState, type FormEvent } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { isApiError } from '../api/client';
import { endpoints, qk, type DiscoveryProfileInput } from '../api/endpoints';
import type { DiscoveryProfile, DiscoveryScan, NotificationChannel } from '../api/types';
import { ScanStatusBadge } from '../components/discovery/Badges';
import {
  ScanOptionsEditor,
  TargetsField,
  useTargetsSummary,
} from '../components/discovery/ScanForm';
import { GroupSelect } from '../components/EntityForms';
import { EditIcon, PlayIcon, PlusIcon, TrashIcon } from '../components/icons';
import { useToast } from '../components/toast';
import {
  Button,
  buttonClass,
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
  Ltr,
  Modal,
  Notice,
  PageHeader,
  Select,
  Table,
  td,
  th,
  TruncLtr,
  Value,
  tabClass,
} from '../components/ui';
import { useTree } from '../hooks/queries';
import {
  progressPct,
  scanOptionsForm,
  scanOptionsToInput,
  scheduleChoice,
  scheduleFromChoice,
  withBuffered,
  type OptionsErrors,
  type ScanOptionsForm,
  type ScheduleChoice,
} from '../lib/discovery';
import { errorMessage } from '../lib/errors';
import { formatDateTime, formatNumber, formatRelative } from '../lib/format';
import { splitTargets } from '../lib/targets';
import { findGroup } from '../lib/tree';

type Tab = 'scans' | 'profiles';

/** Error box that also links to the running scan on 409 scan_running. */
function ScanErrorBox({ error }: { error: unknown }) {
  const { t } = useTranslation();
  if (!error) return null;
  const runningId =
    isApiError(error) && error.code === 'scan_running' ? error.details?.[0]?.scan_id : undefined;
  return (
    <div className="space-y-1">
      <ErrorBox error={error} />
      {runningId !== undefined && (
        <Link
          to={`/discovery/scans/${runningId}`}
          className="text-sm text-accent-ink hover:underline"
        >
          {t('discovery.openRunning', { id: runningId })}
        </Link>
      )}
    </div>
  );
}

/** "Profile saved; a scan is already running" with Run now (L6). */
function SavedProfileNotice({ profile, error }: { profile: DiscoveryProfile; error: unknown }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const runningId =
    isApiError(error) && error.code === 'scan_running' ? error.details?.[0]?.scan_id : undefined;
  const run = useMutation({
    mutationFn: () => endpoints.runDiscoveryProfile(profile.id),
    onSuccess: (scan) => {
      qc.setQueryData(qk.discoveryScan(scan.id), scan);
      qc.setQueryData(qk.discoveryResults(scan.id), withBuffered(scan.id, []));
      void qc.invalidateQueries({ queryKey: ['discoveryScans'] });
      void qc.invalidateQueries({ queryKey: qk.discoveryProfiles });
      navigate(`/discovery/scans/${scan.id}`);
    },
  });
  return (
    <div role="status" className="space-y-2">
      <Notice>
        <bdi dir="auto">{t('discovery.profileSavedBusy', { name: profile.name })}</bdi>
      </Notice>
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" busy={run.isPending} onClick={() => run.mutate()}>
          <PlayIcon size={12} className="rtl:rotate-180" /> {t('discovery.runNow')}
        </Button>
        <Link to="/discovery?tab=profiles" className={buttonClass('ghost', 'sm')}>
          {t('discovery.openProfiles')}
        </Link>
        {runningId !== undefined && (
          <Link
            to={`/discovery/scans/${runningId}`}
            className="text-sm text-accent-ink hover:underline"
          >
            {t('discovery.openRunning', { id: runningId })}
          </Link>
        )}
      </div>
      <ScanErrorBox error={run.error} />
    </div>
  );
}

function useGroupName() {
  const tree = useTree();
  return (id: number | null) => {
    if (id === null) return '—';
    return findGroup(tree.data?.groups ?? [], id)?.name ?? `#${id}`;
  };
}

function NewScanCard() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const tree = useTree();
  const [targets, setTargets] = useState('');
  const [groupId, setGroupId] = useState<number | null>(null);
  const [opts, setOpts] = useState<ScanOptionsForm>(() => scanOptionsForm());
  const [optErrors, setOptErrors] = useState<OptionsErrors>({});
  const [saveProfile, setSaveProfile] = useState(false);
  const [profileName, setProfileName] = useState('');
  const [submitted, setSubmitted] = useState(false);
  /** Set when "Save as profile" created the profile but /run answered 409 scan_running. */
  const [savedProfile, setSavedProfile] = useState<DiscoveryProfile | null>(null);
  const summary = useTargetsSummary(targets);

  useEffect(() => {
    if (groupId === null && tree.data?.groups.length) setGroupId(tree.data.groups[0].id);
  }, [tree.data, groupId]);

  const start = useMutation({
    mutationFn: async (): Promise<DiscoveryScan> => {
      const { options } = scanOptionsToInput(opts);
      const list = splitTargets(targets);
      if (saveProfile) {
        const p = await endpoints.createDiscoveryProfile({
          name: profileName.trim(),
          targets: list,
          group_id: groupId!,
          options,
        });
        void qc.invalidateQueries({ queryKey: qk.discoveryProfiles });
        try {
          return await endpoints.runDiscoveryProfile(p.id);
        } catch (e) {
          // The profile exists now: a retry must not create it again (409 conflict on the name).
          setSaveProfile(false);
          setProfileName('');
          if (isApiError(e) && e.code === 'scan_running') setSavedProfile(p);
          throw e;
        }
      }
      return endpoints.startScan({ targets: list, group_id: groupId!, options });
    },
    onMutate: () => setSavedProfile(null),
    onSuccess: (scan) => {
      qc.setQueryData(qk.discoveryScan(scan.id), scan);
      qc.setQueryData(qk.discoveryResults(scan.id), withBuffered(scan.id, []));
      void qc.invalidateQueries({ queryKey: ['discoveryScans'] });
      navigate(`/discovery/scans/${scan.id}`);
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    const { errors } = scanOptionsToInput(opts);
    setOptErrors(errors);
    if (!summary.valid || Object.keys(errors).length || groupId === null) return;
    if (saveProfile && !profileName.trim()) return;
    start.mutate();
  };

  return (
    <Card title={t('discovery.newScan')}>
      <form onSubmit={submit} className="space-y-4" noValidate>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <div className="md:col-span-2">
            <TargetsField
              value={targets}
              onChange={setTargets}
              summary={summary}
              showErrors={submitted}
            />
          </div>
          <Field label={t('discovery.targetGroup')} hint={t('discovery.targetGroupHint')}>
            <GroupSelect value={groupId} onChange={setGroupId} />
          </Field>
        </div>
        <ScanOptionsEditor value={opts} onChange={setOpts} errors={optErrors} />
        <div className="flex flex-wrap items-end gap-3">
          <Checkbox
            label={t('discovery.saveAsProfile')}
            checked={saveProfile}
            onChange={setSaveProfile}
          />
          {saveProfile && (
            <Input
              className="max-w-xs"
              value={profileName}
              onChange={(e) => setProfileName(e.target.value)}
              placeholder={t('discovery.profileName')}
              aria-label={t('discovery.profileName')}
              maxLength={128}
              required
              aria-invalid={submitted && !profileName.trim()}
            />
          )}
        </div>
        {savedProfile ? (
          <SavedProfileNotice profile={savedProfile} error={start.error} />
        ) : (
          <ScanErrorBox error={start.error} />
        )}
        <div className="flex flex-wrap items-center justify-end gap-3">
          <p className="me-auto text-xs text-ink-muted">{t('discovery.scopeNote')}</p>
          <Button
            type="submit"
            variant="primary"
            busy={start.isPending}
            disabled={!summary.entries.length}
          >
            <PlayIcon size={14} className="rtl:rotate-180" /> {t('discovery.startScan')}
          </Button>
        </div>
      </form>
    </Card>
  );
}

export function MiniProgress({ scan }: { scan: DiscoveryScan }) {
  const pct = progressPct(scan);
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-20 overflow-hidden rounded-sm bg-surface-sunken">
        <div className="h-full bg-accent" style={{ width: `${pct}%` }} />
      </div>
      <Value className="text-xs tabular-nums text-ink-muted" text={`${pct}%`} />
    </div>
  );
}

function ScansCard() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const scans = useQuery({
    queryKey: qk.discoveryScans({}),
    queryFn: () => endpoints.discoveryScans(),
    refetchInterval: (q) =>
      q.state.data?.items.some((s) => s.status === 'running') ? 5000 : false,
  });
  const [del, setDel] = useState<DiscoveryScan | null>(null);
  const remove = useMutation({
    mutationFn: (s: DiscoveryScan) => endpoints.deleteScan(s.id),
    onSuccess: (_r, s) => {
      qc.removeQueries({ queryKey: qk.discoveryScan(s.id) });
      qc.removeQueries({ queryKey: qk.discoveryResults(s.id) });
      void qc.invalidateQueries({ queryKey: ['discoveryScans'] });
      setDel(null);
    },
  });

  return (
    <Card title={t('discovery.history')} bodyClassName="p-0">
      {scans.isLoading && <Loading />}
      <ErrorBox error={scans.error} className="m-3" />
      {scans.data && !scans.data.items.length && <Empty>{t('discovery.noScans')}</Empty>}
      {scans.data && scans.data.items.length > 0 && (
        <Table>
          <thead>
            <tr>
              <th className={th}>#</th>
              <th className={th}>{t('common.status')}</th>
              <th className={th}>{t('discovery.started')}</th>
              <th className={th}>{t('discovery.source')}</th>
              <th className={th}>{t('discovery.targets')}</th>
              <th className={th}>{t('discovery.found')}</th>
              <th className={th}>{t('discovery.new')}</th>
              <th className={th} />
            </tr>
          </thead>
          <tbody>
            {scans.data.items.map((s) => (
              <tr key={s.id} className="hover:bg-surface-hover">
                <td className={`${td} font-mono text-[12.5px] tabular-nums`}>
                  <Link
                    to={`/discovery/scans/${s.id}`}
                    className="font-medium text-accent-ink hover:underline"
                  >
                    <Ltr>#{s.id}</Ltr>
                  </Link>
                </td>
                <td className={td}>
                  <div className="flex flex-wrap items-center gap-2">
                    <ScanStatusBadge status={s.status} />
                    {s.status === 'running' && <MiniProgress scan={s} />}
                  </div>
                </td>
                <td
                  className={`${td} whitespace-nowrap text-ink-muted`}
                  title={formatDateTime(s.started_at)}
                >
                  {formatRelative(s.started_at)}
                </td>
                <td className={`${td} text-ink-muted`}>
                  <div>
                    {s.profile_name ? (
                      <bdi dir="auto">{s.profile_name}</bdi>
                    ) : (
                      t('discovery.manual')
                    )}
                  </div>
                  <div className="text-xs text-ink-muted">
                    {t(`discovery.trigger.${s.trigger}`)}
                    {s.created_by && (
                      <>
                        {' · '}
                        <bdi dir="auto">{s.created_by}</bdi>
                      </>
                    )}
                  </div>
                </td>
                <td className={`${td} max-w-[14rem]`}>
                  <TruncLtr className="font-mono text-xs" title={s.targets.join(', ')}>
                    {s.targets.join(', ')}
                  </TruncLtr>
                </td>
                <td className={`${td} font-mono text-[12.5px] tabular-nums`}>
                  {formatNumber(s.found_hosts, 0)}
                </td>
                <td className={`${td} font-mono text-[12.5px] tabular-nums`}>
                  {s.new_hosts > 0 ? (
                    <span className="font-semibold text-accent-ink">
                      {formatNumber(s.new_hosts, 0)}
                    </span>
                  ) : (
                    0
                  )}
                </td>
                <td className={`${td} whitespace-nowrap text-end`}>
                  <Link to={`/discovery/scans/${s.id}`} className={buttonClass('secondary', 'sm')}>
                    {t('discovery.open')}
                  </Link>
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={t('common.delete')}
                    title={s.status === 'running' ? t('discovery.deleteRunningHint') : undefined}
                    disabled={s.status === 'running'}
                    onClick={() => setDel(s)}
                  >
                    <TrashIcon size={14} />
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      <ConfirmDialog
        open={!!del}
        title={t('discovery.deleteScanTitle')}
        message={t('discovery.deleteScanConfirm', { id: del?.id })}
        onConfirm={() => del && remove.mutate(del)}
        onClose={() => setDel(null)}
        busy={remove.isPending}
        error={remove.error}
      />
    </Card>
  );
}

function ProfileModal({
  open,
  onClose,
  profile,
  channels,
}: {
  open: boolean;
  onClose: () => void;
  profile?: DiscoveryProfile;
  channels: NotificationChannel[];
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const tree = useTree();
  const [name, setName] = useState('');
  const [targets, setTargets] = useState('');
  const [groupId, setGroupId] = useState<number | null>(null);
  const [opts, setOpts] = useState<ScanOptionsForm>(() => scanOptionsForm());
  const [optErrors, setOptErrors] = useState<OptionsErrors>({});
  const [schedule, setSchedule] = useState<ScheduleChoice>('off');
  const [hours, setHours] = useState('24');
  const [notify, setNotify] = useState(false);
  const [channelIds, setChannelIds] = useState<number[]>([]);
  const [submitted, setSubmitted] = useState(false);
  const summary = useTargetsSummary(targets);

  useEffect(() => {
    if (!open) return;
    setName(profile?.name ?? '');
    setTargets(profile?.targets.join('\n') ?? '');
    setGroupId(profile?.group_id ?? tree.data?.groups[0]?.id ?? null);
    setOpts(scanOptionsForm(profile?.options));
    setOptErrors({});
    setSchedule(
      profile ? scheduleChoice(profile.schedule_enabled, profile.schedule_every_hours) : 'off',
    );
    setHours(String(profile?.schedule_every_hours ?? 24));
    setNotify(profile?.notify_new_hosts ?? false);
    setChannelIds(profile?.notify_channel_ids ?? []);
    setSubmitted(false);
    m.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, profile]);

  const m = useMutation({
    mutationFn: (body: DiscoveryProfileInput) =>
      profile
        ? endpoints.updateDiscoveryProfile(profile.id, body)
        : endpoints.createDiscoveryProfile(body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.discoveryProfiles });
      toast.push('success', t('common.saved'));
      onClose();
    },
  });

  const sched = scheduleFromChoice(schedule, hours);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    const { options, errors } = scanOptionsToInput(opts);
    setOptErrors(errors);
    if (!name.trim() || !summary.valid || groupId === null || !sched) return;
    if (Object.keys(errors).length) return;
    m.mutate({
      name: name.trim(),
      targets: splitTargets(targets),
      group_id: groupId,
      options,
      ...sched,
      notify_new_hosts: notify,
      notify_channel_ids: notify ? channelIds : [],
    });
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      wide
      title={profile ? t('discovery.editProfile') : t('discovery.addProfile')}
    >
      <form onSubmit={submit} className="space-y-4" noValidate>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label={t('common.name')} required>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={128}
              required
              autoFocus
              dir="auto"
              aria-invalid={submitted && !name.trim()}
            />
          </Field>
          <Field label={t('discovery.targetGroup')} hint={t('discovery.targetGroupHint')}>
            <GroupSelect value={groupId} onChange={setGroupId} />
          </Field>
        </div>
        <TargetsField
          value={targets}
          onChange={setTargets}
          summary={summary}
          showErrors={submitted}
        />
        <ScanOptionsEditor
          value={opts}
          onChange={setOpts}
          errors={optErrors}
          editingProfile={!!profile}
        />
        <fieldset className="min-w-0 rounded-md border border-line p-3">
          <legend className="px-1 text-xs font-semibold text-ink-muted">
            {t('discovery.schedule')}
          </legend>
          <div className="space-y-3">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field label={t('discovery.runs')} hint={t('discovery.scheduleHint')}>
                <Select
                  value={schedule}
                  onChange={(e) => setSchedule(e.target.value as ScheduleChoice)}
                >
                  {(['off', 'daily', 'weekly', 'hours'] as const).map((c) => (
                    <option key={c} value={c}>
                      {t(`discovery.scheduleChoice.${c}`)}
                    </option>
                  ))}
                </Select>
              </Field>
              {schedule === 'hours' && (
                <Field
                  label={t('discovery.everyHours')}
                  hint={
                    !sched ? (
                      <span className="text-status-down">{t('discovery.err.hours')}</span>
                    ) : undefined
                  }
                >
                  <Input
                    type="number"
                    min={1}
                    max={720}
                    value={hours}
                    onChange={(e) => setHours(e.target.value)}
                    dir="ltr"
                  />
                </Field>
              )}
            </div>
            <Checkbox label={t('discovery.notifyNew')} checked={notify} onChange={setNotify} />
            {notify && (
              <FieldGroup label={t('discovery.notifyChannels')} hint={t('discovery.notifyHint')}>
                {channels.length ? (
                  <div className="flex flex-col gap-1">
                    {channels.map((c) => (
                      <Checkbox
                        key={c.id}
                        label={`${c.name} (${t(`channels.${c.type}`)})`}
                        checked={channelIds.includes(c.id)}
                        onChange={(v) =>
                          setChannelIds((x) => (v ? [...x, c.id] : x.filter((y) => y !== c.id)))
                        }
                      />
                    ))}
                  </div>
                ) : (
                  <p className="text-sm text-ink-muted">{t('rules.noChannels')}</p>
                )}
              </FieldGroup>
            )}
          </div>
        </fieldset>
        <ErrorBox error={m.error} />
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="submit" variant="primary" busy={m.isPending}>
            {t('common.save')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function scheduleText(p: DiscoveryProfile, t: TFunction): string {
  const c = scheduleChoice(p.schedule_enabled, p.schedule_every_hours);
  if (c === 'hours') return t('discovery.everyNHours', { count: p.schedule_every_hours });
  return t(`discovery.scheduleChoice.${c}`);
}

function ProfilesCard() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const groupName = useGroupName();
  const profiles = useQuery({
    queryKey: qk.discoveryProfiles,
    queryFn: endpoints.discoveryProfiles,
  });
  const channels = useQuery({ queryKey: qk.channels, queryFn: endpoints.channels });
  const [edit, setEdit] = useState<DiscoveryProfile | 'new' | null>(null);
  const [del, setDel] = useState<DiscoveryProfile | null>(null);
  const [runError, setRunError] = useState<unknown>(null);

  const run = useMutation({
    mutationFn: (p: DiscoveryProfile) => endpoints.runDiscoveryProfile(p.id),
    onMutate: () => setRunError(null),
    onSuccess: (scan) => {
      qc.setQueryData(qk.discoveryScan(scan.id), scan);
      qc.setQueryData(qk.discoveryResults(scan.id), withBuffered(scan.id, []));
      void qc.invalidateQueries({ queryKey: ['discoveryScans'] });
      void qc.invalidateQueries({ queryKey: qk.discoveryProfiles });
      navigate(`/discovery/scans/${scan.id}`);
    },
    onError: (e) => {
      setRunError(e);
      toast.push('error', errorMessage(t, e));
    },
  });
  const remove = useMutation({
    mutationFn: (p: DiscoveryProfile) => endpoints.deleteDiscoveryProfile(p.id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.discoveryProfiles });
      setDel(null);
    },
  });
  const chName = (id: number) => channels.data?.items.find((c) => c.id === id)?.name ?? `#${id}`;

  return (
    <Card
      title={t('discovery.profiles')}
      bodyClassName="p-0"
      actions={
        <Button size="sm" variant="primary" onClick={() => setEdit('new')}>
          <PlusIcon size={14} /> {t('discovery.addProfile')}
        </Button>
      }
    >
      {profiles.isLoading && <Loading />}
      <ErrorBox error={profiles.error} className="m-3" />
      {runError ? (
        <div className="m-3">
          <ScanErrorBox error={runError} />
        </div>
      ) : null}
      {profiles.data && !profiles.data.items.length && <Empty>{t('discovery.noProfiles')}</Empty>}
      {profiles.data && profiles.data.items.length > 0 && (
        <Table>
          <thead>
            <tr>
              <th className={th}>{t('common.name')}</th>
              <th className={th}>{t('discovery.targets')}</th>
              <th className={th}>{t('discovery.targetGroup')}</th>
              <th className={th}>{t('discovery.schedule')}</th>
              <th className={th}>{t('discovery.lastRun')}</th>
              <th className={th}>{t('discovery.notify')}</th>
              <th className={th} />
            </tr>
          </thead>
          <tbody>
            {profiles.data.items.map((p) => (
              <tr key={p.id}>
                <td className={`${td} font-medium`}>
                  <bdi dir="auto">{p.name}</bdi>
                </td>
                <td className={`${td} max-w-[14rem]`}>
                  <TruncLtr className="font-mono text-xs" title={p.targets.join(', ')}>
                    {p.targets.join(', ')}
                  </TruncLtr>
                </td>
                <td className={td}>
                  <bdi dir="auto">{groupName(p.group_id)}</bdi>
                </td>
                <td className={td}>
                  <div className={cx(!p.schedule_enabled && 'text-ink-muted')}>
                    {scheduleText(p, t)}
                  </div>
                  {p.next_run_at && (
                    <div className="text-xs text-ink-muted" title={formatDateTime(p.next_run_at)}>
                      {t('discovery.nextRun')}: {formatRelative(p.next_run_at)}
                    </div>
                  )}
                </td>
                <td className={`${td} whitespace-nowrap text-ink-muted`}>
                  {p.last_scan_id !== null ? (
                    <Link
                      to={`/discovery/scans/${p.last_scan_id}`}
                      className="text-accent-ink hover:underline"
                      title={formatDateTime(p.last_run_at)}
                    >
                      {formatRelative(p.last_run_at)}
                    </Link>
                  ) : (
                    formatRelative(p.last_run_at)
                  )}
                </td>
                <td className={`${td} text-ink-muted`}>
                  {p.notify_new_hosts
                    ? p.notify_channel_ids.length
                      ? p.notify_channel_ids.map(chName).join(', ')
                      : t('discovery.eventOnly')
                    : t('common.no')}
                </td>
                <td className={`${td} whitespace-nowrap text-end`}>
                  <Button
                    size="sm"
                    busy={run.isPending && run.variables?.id === p.id}
                    onClick={() => run.mutate(p)}
                  >
                    <PlayIcon size={12} className="rtl:rotate-180" /> {t('discovery.runNow')}
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={t('common.edit')}
                    onClick={() => setEdit(p)}
                  >
                    <EditIcon size={14} />
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={t('common.delete')}
                    onClick={() => setDel(p)}
                  >
                    <TrashIcon size={14} />
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      <ProfileModal
        open={edit !== null}
        onClose={() => setEdit(null)}
        profile={edit && edit !== 'new' ? edit : undefined}
        channels={channels.data?.items ?? []}
      />
      <ConfirmDialog
        open={!!del}
        title={t('discovery.deleteProfileTitle')}
        message={t('discovery.deleteProfileConfirm', { name: del?.name })}
        onConfirm={() => del && remove.mutate(del)}
        onClose={() => setDel(null)}
        busy={remove.isPending}
        error={remove.error}
      />
    </Card>
  );
}

export function DiscoveryPage() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const tab: Tab = params.get('tab') === 'profiles' ? 'profiles' : 'scans';
  const setTab = (x: Tab) => setParams(x === 'scans' ? {} : { tab: x }, { replace: true });
  return (
    <div className="space-y-4">
      <PageHeader title={t('discovery.title')} subtitle={t('discovery.subtitle')} />
      <div role="tablist" className="flex overflow-x-auto border-b border-line">
        {(['scans', 'profiles'] as const).map((x) => (
          <button
            key={x}
            type="button"
            role="tab"
            aria-selected={tab === x}
            onClick={() => setTab(x)}
            className={tabClass(tab === x)}
          >
            {t(`discovery.tab.${x}`)}
          </button>
        ))}
      </div>
      {tab === 'scans' ? (
        <>
          <NewScanCard />
          <ScansCard />
        </>
      ) : (
        <ProfilesCard />
      )}
    </div>
  );
}
