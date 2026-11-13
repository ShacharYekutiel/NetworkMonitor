import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints3, qk3 } from '../../api/endpoints3';
import type { Settings, SettingsInput } from '../../api/types';
import type { Settings3 } from '../../api/types3';
import { useAuth } from '../../hooks/auth';
import { useFeatures } from '../../hooks/features';
import { useTree } from '../../hooks/queries';
import { formatDateTime } from '../../lib/format';
import { flattenGroups } from '../../lib/tree';
import { learningProgress } from '../../lib/newDevices';
import { useToast } from '../toast';
import { Button, Card, Checkbox, ErrorBox, Field, FieldGroup, Input, Textarea } from '../ui';
import { useSaveSettings } from './common';
import { FeatureTab } from './FeatureToggle';
import { cardAnchor } from '../../lib/settingsIndex';

type NumKey = {
  [K in keyof Settings3]: Settings3[K] extends number ? K : never;
}[keyof Settings3];
type BoolKey = {
  [K in keyof Settings3]: Settings3[K] extends boolean ? K : never;
}[keyof Settings3];

export type NumField = { key: NumKey; min: number; max: number; zeroOk?: boolean };

/** true when the value is not an integer within the field's range. */
export function numFieldError(f: NumField, v: string): boolean {
  const s = v.trim();
  if (!/^\d+$/.test(s)) return true;
  const n = Number(s);
  if (f.zeroOk && n === 0) return false;
  return n < f.min || n > f.max;
}

/** A card of numeric / boolean / HH:MM settings saved with one PUT (partial merge). */
function SettingsForm({
  settings,
  title,
  nums,
  bools = [],
  time,
  prefix,
  extra,
  extraValid = true,
  extraInput,
  anchor,
}: {
  settings: Settings;
  title: string;
  anchor: string;
  nums: NumField[];
  bools?: BoolKey[];
  time?: 'config_backup_time';
  prefix: string;
  extra?: React.ReactNode;
  extraValid?: boolean;
  extraInput?: () => SettingsInput;
}) {
  const { t } = useTranslation();
  const s = settings as unknown as Partial<Settings3>;
  const init = () => ({
    n: Object.fromEntries(nums.map((f) => [f.key, String(s[f.key] ?? '')])) as Record<
      string,
      string
    >,
    b: Object.fromEntries(bools.map((k) => [k, !!s[k]])) as Record<string, boolean>,
    time: time ? String(s[time] ?? '') : '',
  });
  const [form, setForm] = useState(init);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => setForm(init()), [settings]);
  const save = useSaveSettings();
  const bad = nums.filter((f) => numFieldError(f, form.n[f.key] ?? ''));
  const timeBad = !!time && !/^([01]\d|2[0-3]):[0-5]\d$/.test(form.time);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (bad.length || timeBad || !extraValid) return;
    const body: Record<string, unknown> = {};
    for (const f of nums) body[f.key] = Number(form.n[f.key]);
    for (const k of bools) body[k] = form.b[k];
    if (time) body[time] = form.time;
    save.mutate({ ...(body as SettingsInput), ...(extraInput?.() ?? {}) });
  };
  return (
    <Card title={title} anchor={cardAnchor(anchor)}>
      <form onSubmit={submit} className="space-y-3">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {time && (
            <Field
              label={t(`${prefix}.${time}`)}
              hint={
                timeBad ? (
                  <span className="text-status-down">{t('settings3.timeErr')}</span>
                ) : (
                  t(`${prefix}.${time}Hint`)
                )
              }
            >
              <Input
                type="time"
                value={form.time}
                onChange={(e) => setForm((x) => ({ ...x, time: e.target.value }))}
                dir="ltr"
              />
            </Field>
          )}
          {nums.map((f) => {
            const err = bad.includes(f);
            return (
              <Field
                key={f.key}
                label={t(`${prefix}.${f.key}`)}
                hint={
                  <span className={err ? 'text-status-down' : undefined}>
                    {f.zeroOk
                      ? t('settings3.rangeZero', { min: f.min, max: f.max })
                      : t('settings3.range', { min: f.min, max: f.max })}
                  </span>
                }
              >
                <Input
                  type="number"
                  min={f.zeroOk ? 0 : f.min}
                  max={f.max}
                  value={form.n[f.key] ?? ''}
                  onChange={(e) =>
                    setForm((x) => ({ ...x, n: { ...x.n, [f.key]: e.target.value } }))
                  }
                  dir="ltr"
                  aria-invalid={err}
                />
              </Field>
            );
          })}
        </div>
        {bools.length > 0 && (
          <div className="flex flex-col gap-1.5">
            {bools.map((k) => (
              <Checkbox
                key={k}
                label={t(`${prefix}.${k}`)}
                checked={!!form.b[k]}
                onChange={(v) => setForm((x) => ({ ...x, b: { ...x.b, [k]: v } }))}
              />
            ))}
          </div>
        )}
        {extra}
        <ErrorBox error={save.error} />
        <div className="flex justify-end">
          <Button
            type="submit"
            variant="primary"
            busy={save.isPending}
            disabled={!!bad.length || timeBad || !extraValid}
          >
            {t('common.save')}
          </Button>
        </div>
      </form>
    </Card>
  );
}

/** [3] Settings → Backups */
export function BackupSettingsTab({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  return (
    <FeatureTab feature="config_backup" settings={settings}>
      <SettingsForm
        settings={settings}
        title={t('settings3.backupTitle')}
        anchor="backup"
        prefix="settings3"
        time="config_backup_time"
        nums={[
          { key: 'config_backup_window_min', min: 0, max: 720 },
          { key: 'config_backup_concurrency', min: 1, max: 16 },
          { key: 'config_backup_timeout_s', min: 30, max: 600 },
          { key: 'config_backup_max_versions', min: 2, max: 1000 },
          { key: 'config_backup_min_versions', min: 1, max: 100 },
          { key: 'config_backup_retention_days', min: 7, max: 3650 },
          { key: 'config_backup_fail_threshold', min: 1, max: 10 },
          { key: 'config_change_clear_min', min: 5, max: 10080 },
        ]}
        bools={['config_backup_mask_secrets', 'config_change_alert']}
      />
    </FeatureTab>
  );
}

/** "6 tcp 8443 Admin portal" per line ↔ flow_app_overrides */
function overridesText(v: Settings3['flow_app_overrides'] | undefined): string {
  return (v ?? []).map((o) => `${o.protocol} ${o.port} ${o.name}`).join('\n');
}
export function parseOverrides(text: string): {
  list: Settings3['flow_app_overrides'];
  error: boolean;
} {
  const list: Settings3['flow_app_overrides'] = [];
  let error = false;
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    const m = /^(\d{1,3})\s+(\d{1,5})\s+(.{1,64})$/.exec(line);
    if (!m || Number(m[1]) > 255 || Number(m[2]) > 65535) {
      error = true;
      continue;
    }
    list.push({ protocol: Number(m[1]), port: Number(m[2]), name: m[3].trim() });
  }
  return { list, error: error || list.length > 200 };
}

/** [3] Settings → Flows */
export function FlowSettingsTab({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const [ov, setOv] = useState(overridesText(settings.flow_app_overrides));
  useEffect(() => setOv(overridesText(settings.flow_app_overrides)), [settings]);
  const parsed = parseOverrides(ov);
  return (
    <FeatureTab feature="flows" settings={settings}>
      <SettingsForm
        settings={settings}
        title={t('settings3.flowsTitle')}
        anchor="flows"
        prefix="settings3"
        nums={[
          { key: 'flow_max_records_per_s', min: 1000, max: 50000 },
          { key: 'flow_top_conversations', min: 50, max: 5000 },
          { key: 'flow_top_hosts', min: 20, max: 2000 },
          { key: 'flow_top_apps', min: 10, max: 1000 },
          { key: 'flow_raw_retention_hours', min: 1, max: 168 },
          { key: 'flow_minute_retention_hours', min: 24, max: 336 },
          { key: 'flow_rollup_retention_days', min: 7, max: 400 },
        ]}
        extraValid={!parsed.error}
        extraInput={() => ({ flow_app_overrides: parsed.list })}
        extra={
          <Field
            label={t('settings3.flow_app_overrides')}
            hint={
              parsed.error ? (
                <span className="text-status-down">{t('settings3.overridesErr')}</span>
              ) : (
                t('settings3.overridesHint')
              )
            }
          >
            <Textarea
              rows={4}
              value={ov}
              onChange={(e) => setOv(e.target.value)}
              dir="ltr"
              className="font-mono"
              placeholder="6 8443 admin-portal"
            />
          </Field>
        }
      />
    </FeatureTab>
  );
}

function ScopeGroups({ value, onChange }: { value: number[]; onChange: (v: number[]) => void }) {
  const { t } = useTranslation();
  const { data } = useTree();
  const groups = useMemo(() => flattenGroups(data?.groups ?? []), [data]);
  return (
    <FieldGroup label={t('newDevices.scope')} hint={t('newDevices.scopeHint')}>
      <div className="max-h-40 space-y-1 overflow-y-auto rounded-md p-2 ring-1 ring-inset ring-line">
        {groups.map(({ group, depth }) => (
          <div key={group.id} style={{ paddingInlineStart: depth * 12 }}>
            <Checkbox
              label={group.name}
              checked={value.includes(group.id)}
              onChange={(v) =>
                onChange(v ? [...value, group.id] : value.filter((x) => x !== group.id))
              }
            />
          </div>
        ))}
      </div>
    </FieldGroup>
  );
}

/** [3] Settings → New devices */
export function NewDeviceSettingsTab({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const [scope, setScope] = useState<number[]>(settings.newdev_scope_group_ids ?? []);
  useEffect(() => setScope(settings.newdev_scope_group_ids ?? []), [settings]);
  return (
    <FeatureTab feature="new_device_alerts" settings={settings}>
      <SettingsForm
        settings={settings}
        title={t('settings3.newdevTitle')}
        anchor="newDevices"
        prefix="settings3"
        nums={[
          { key: 'newdev_learning_days', min: 0, max: 60 },
          { key: 'newdev_condition_clear_min', min: 10, max: 10080 },
          { key: 'newdev_forget_after_days', min: 30, max: 3650, zeroOk: true },
        ]}
        bools={['newdev_ignore_random_macs']}
        extraValid={scope.length <= 100}
        extraInput={() => ({ newdev_scope_group_ids: scope })}
        extra={
          <>
            <ScopeGroups value={scope} onChange={setScope} />
            {settings.newdev_learning_until && (
              <p className="text-xs text-ink-muted">
                {t('newDevices.learningUntil', {
                  date: formatDateTime(settings.newdev_learning_until),
                })}
              </p>
            )}
          </>
        }
      />
    </FeatureTab>
  );
}

/** [3] Settings → Reports (retention) */
export function ReportSettingsTab({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  return (
    <FeatureTab feature="reports" settings={settings}>
      <SettingsForm
        settings={settings}
        title={t('settings3.reportsTitle')}
        anchor="reports"
        prefix="settings3"
        nums={[{ key: 'report_retention_days', min: 30, max: 3650 }]}
      />
    </FeatureTab>
  );
}

/** [3] Settings → Chat acknowledge: TTL, poll timeout, Telegram pollers, all linked accounts. */
export function ChatAckSettingsTab({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const { features } = useFeatures();
  const qc = useQueryClient();
  const toast = useToast();
  const { isAdmin } = useAuth();
  const poll = useQuery({
    queryKey: qk3.telegramStatus,
    queryFn: endpoints3.telegramStatus,
    enabled: features.chat_ack,
    refetchInterval: 30_000,
  });
  const links = useQuery({
    queryKey: qk3.chatLinks(true),
    queryFn: () => endpoints3.chatLinks(true),
    enabled: features.chat_ack && isAdmin,
  });
  const unlink = useMutation({
    mutationFn: (id: number) => endpoints3.deleteChatLink(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['chat-links'] });
      toast.push('success', t('chatAck.unlinked'));
    },
  });
  return (
    <FeatureTab feature="chat_ack" settings={settings}>
      <div className="space-y-4">
        <SettingsForm
          settings={settings}
          title={t('settings3.chatAckTitle')}
          anchor="chatAck"
          prefix="settings3"
          nums={[
            { key: 'chat_ack_ttl_h', min: 1, max: 720 },
            { key: 'telegram_poll_timeout_s', min: 10, max: 50 },
          ]}
          extra={
            <div className="space-y-1 text-xs text-ink-muted">
              <p>{t('chatAck.teamsLimit')}</p>
              {!settings.public_base_url && (
                <p className="text-status-warning">{t('chatAck.needsPublicUrl')}</p>
              )}
            </div>
          }
        />
        {features.chat_ack && (
          <Card title={t('chatAck.pollers')} anchor={cardAnchor('chatAckPollers')}>
            <ErrorBox error={poll.error} />
            {poll.data && !poll.data.pollers.length && (
              <p className="text-sm text-ink-muted">{t('features.status.noPoller')}</p>
            )}
            <ul className="space-y-1 text-sm">
              {poll.data?.pollers.map((p) => (
                <li key={p.bot_username} className="flex flex-wrap items-center gap-2">
                  <bdi dir="ltr" className="font-medium">
                    @{p.bot_username}
                  </bdi>
                  <span
                    className={
                      p.state === 'error'
                        ? 'text-status-down'
                        : p.state === 'polling'
                          ? 'text-status-up'
                          : 'text-ink-muted'
                    }
                  >
                    {t(`chatAck.pollerState.${p.state}`)}
                  </span>
                  {p.last_error && (
                    <bdi dir="ltr" className="text-xs text-status-down">
                      {p.last_error}
                    </bdi>
                  )}
                  <span className="text-xs text-ink-muted">{formatDateTime(p.updated_at)}</span>
                </li>
              ))}
            </ul>
          </Card>
        )}
        {features.chat_ack && isAdmin && (
          <Card title={t('chatAck.allLinks')} anchor={cardAnchor('chatAckLinks')}>
            <ErrorBox error={links.error ?? unlink.error} />
            {links.data && !links.data.items.length && (
              <p className="text-sm text-ink-muted">{t('chatAck.noLinks')}</p>
            )}
            <ul className="divide-y divide-line text-sm">
              {links.data?.items.map((l) => (
                <li key={l.id} className="flex flex-wrap items-center gap-2 py-1.5">
                  <span className="font-medium">{l.username}</span>
                  <bdi dir="ltr">{l.display}</bdi>
                  <span className="text-xs text-ink-muted">
                    {t('chatAck.linkedAt', { date: formatDateTime(l.linked_at) })}
                  </span>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="ms-auto"
                    onClick={() => unlink.mutate(l.id)}
                  >
                    {t('chatAck.unlink')}
                  </Button>
                </li>
              ))}
            </ul>
          </Card>
        )}
      </div>
    </FeatureTab>
  );
}

/** [3] Learning progress bar (New devices status card). */
export function LearningBar({
  until,
  learning,
  days,
}: {
  until: string | null;
  learning: boolean;
  days: number;
}) {
  const p = learningProgress({ learning, learning_until: until }, days, Date.now());
  return (
    <div
      className="h-2 w-full overflow-hidden rounded bg-surface-hover"
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(p * 100)}
    >
      <div className="h-full bg-accent" style={{ width: `${Math.round(p * 100)}%` }} />
    </div>
  );
}
