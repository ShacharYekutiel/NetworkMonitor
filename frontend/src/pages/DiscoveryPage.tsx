import { useEffect, useState, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { isApiError } from '../api/client';
import { endpoints, qk, type DiscoveryProfileInput } from '../api/endpoints';
import type { DiscoveryProfile, DiscoveryScan, NotificationChannel } from '../api/types';
import {
  ScanOptionsEditor,
  TargetsField,
  useTargetsSummary,
} from '../components/discovery/ScanForm';
import { GroupSelect } from '../components/EntityForms';
import { EditIcon, PlayIcon, PlusIcon, RadarIcon, TrashIcon } from '../components/icons';
import { ActionRow } from '../components/kit';
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
  GroupList,
  GroupTitle,
  Input,
  Loading,
  Modal,
  Notice,
  PageHeader,
  Select,
} from '../components/ui';
import { useTree } from '../hooks/queries';
import {
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
        <Link to="/discovery#profiles" className={buttonClass('ghost', 'sm')}>
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
      <form onSubmit={submit} className="grid gap-5" noValidate>
        <TargetsField
          value={targets}
          onChange={setTargets}
          summary={summary}
          showErrors={submitted}
        />
        <ScanOptionsEditor value={opts} onChange={setOpts} errors={optErrors} />
        <Field label={t('discovery.targetGroup')} hint={t('discovery.targetGroupHint')}>
          <GroupSelect value={groupId} onChange={setGroupId} />
        </Field>
        <div className="flex flex-wrap items-center gap-3">
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
        <div className="grid gap-2.5">
          <div>
            <Button
              type="submit"
              variant="primary"
              busy={start.isPending}
              disabled={!summary.entries.length}
            >
              {t('discovery.startScan')}
            </Button>
          </div>
          <p className="m-0 text-xs text-ink-subtle">{t('discovery.scopeNote')}</p>
        </div>
      </form>
    </Card>
  );
}

/** Dot of a scan row: running in ink, then the outcome in its status colour. */
const SCAN_DOT: Record<DiscoveryScan['status'], string> = {
  running: 'bg-accent motion-safe:animate-pulse',
  completed: 'bg-status-up',
  failed: 'bg-status-down',
  cancelled: 'bg-status-paused',
};

/** "205 of 307 · found 37" while running, else "yesterday 03:00 · found 214 · 3 new". */
function useScanLine() {
  const { t } = useTranslation();
  return (s: DiscoveryScan) => {
    const parts =
      s.status === 'running'
        ? [
            t('discovery.scanned', {
              scanned: formatNumber(s.scanned_hosts, 0),
              total: formatNumber(s.total_hosts, 0),
            }),
          ]
        : [formatRelative(s.started_at)];
    parts.push(t('discovery.foundN', { n: formatNumber(s.found_hosts, 0) }));
    if (s.new_hosts > 0) parts.push(t('discovery.newN', { n: formatNumber(s.new_hosts, 0) }));
    return parts.join(' · ');
  };
}

function ScansList() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const line = useScanLine();
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
    <section aria-labelledby="discovery-history">
      <GroupTitle id="discovery-history">{t('discovery.history')}</GroupTitle>
      {scans.isLoading && <Loading />}
      <ErrorBox error={scans.error} className="mb-3" />
      {scans.data && !scans.data.items.length && (
        <div className="rounded-xl bg-surface shadow-card">
          <Empty>{t('discovery.noScans')}</Empty>
        </div>
      )}
      {scans.data && scans.data.items.length > 0 && (
        <GroupList label={t('discovery.history')}>
          {scans.data.items.map((s) => (
            <ActionRow
              key={s.id}
              testId="scan-row"
              to={`/discovery/scans/${s.id}`}
              dot={SCAN_DOT[s.status]}
              title={
                <>
                  <bdi>{t('discovery.scanTitle', { id: s.id })}</bdi>
                  {' · '}
                  <span className={cx(s.status === 'failed' && 'text-status-down')}>
                    {t(`discovery.status.${s.status}`)}
                  </span>
                </>
              }
              subtitle={
                <>
                  {s.profile_name ? <bdi dir="auto">{s.profile_name}</bdi> : t('discovery.manual')}
                  {' · '}
                  {line(s)}
                </>
              }
              end={
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
              }
            />
          ))}
        </GroupList>
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
    </section>
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
        <fieldset className="min-w-0 rounded-[18px] bg-surface-sunken p-4">
          <legend className="float-start mb-2 w-full p-0 text-[13.5px] font-semibold text-ink">
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

function ProfilesList() {
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
    <section aria-labelledby="discovery-profiles" id="profiles">
      <GroupTitle
        id="discovery-profiles"
        action={
          <Button size="sm" onClick={() => setEdit('new')}>
            <PlusIcon size={14} /> {t('discovery.addProfile')}
          </Button>
        }
      >
        {t('discovery.profiles')}
      </GroupTitle>
      {profiles.isLoading && <Loading />}
      <ErrorBox error={profiles.error} className="mb-3" />
      {runError ? (
        <div className="mb-3">
          <ScanErrorBox error={runError} />
        </div>
      ) : null}
      {profiles.data && !profiles.data.items.length && (
        <div className="rounded-xl bg-surface shadow-card">
          <Empty>{t('discovery.noProfiles')}</Empty>
        </div>
      )}
      {profiles.data && profiles.data.items.length > 0 && (
        <GroupList label={t('discovery.profiles')}>
          {profiles.data.items.map((p) => (
            <ActionRow
              key={p.id}
              testId="profile-row"
              wrap
              icon={<RadarIcon size={18} />}
              title={<bdi dir="auto">{p.name}</bdi>}
              subtitle={
                <>
                  <span className={cx(!p.schedule_enabled && 'text-ink-subtle')}>
                    {scheduleText(p, t)}
                  </span>
                  {p.next_run_at && (
                    <span title={formatDateTime(p.next_run_at)}>
                      {' · '}
                      {t('discovery.nextRun')}: {formatRelative(p.next_run_at)}
                    </span>
                  )}
                  {' · '}
                  {p.notify_new_hosts
                    ? p.notify_channel_ids.length
                      ? t('discovery.notifyTo', {
                          list: p.notify_channel_ids.map(chName).join(', '),
                        })
                      : t('discovery.eventOnly')
                    : t('discovery.noNotify')}
                  {' · '}
                  <bdi dir="auto">{groupName(p.group_id)}</bdi>
                  {p.last_scan_id !== null && (
                    <>
                      {' · '}
                      <Link
                        to={`/discovery/scans/${p.last_scan_id}`}
                        className="text-ink-muted underline-offset-2 hover:text-ink hover:underline"
                        title={formatDateTime(p.last_run_at)}
                      >
                        {t('discovery.lastRun')}: {formatRelative(p.last_run_at)}
                      </Link>
                    </>
                  )}
                </>
              }
              end={
                <>
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
                </>
              }
            />
          ))}
        </GroupList>
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
    </section>
  );
}

export function DiscoveryPage() {
  const { t } = useTranslation();
  return (
    <div>
      <PageHeader title={t('discovery.title')} subtitle={t('discovery.subtitle')} />
      {/* prototype .two: the new-scan form, and the recent scans + saved profiles beside it */}
      <div className="grid grid-cols-1 items-start gap-6 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
        <NewScanCard />
        <div className="grid gap-[26px]">
          <ScansList />
          <ProfilesList />
        </div>
      </div>
    </div>
  );
}
