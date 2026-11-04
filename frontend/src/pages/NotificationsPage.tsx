import { useEffect, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk, type AlertRuleInput, type ChannelInput } from '../api/endpoints';
import type {
  AlertRule,
  EmailChannelConfig,
  NotificationChannel,
  WebhookChannelConfig,
} from '../api/types';
import { EditIcon, PlusIcon, TrashIcon } from '../components/icons';
import { StatusBadge } from '../components/Status';
import { useToast } from '../components/toast';
import {
  Button,
  Card,
  Checkbox,
  ConfirmDialog,
  Empty,
  ErrorBox,
  Field,
  Input,
  Loading,
  Ltr,
  Modal,
  PageHeader,
  Select,
  Table,
  Textarea,
  td,
  th,
} from '../components/ui';
import { errorMessage } from '../lib/errors';
import { headersToText, textToHeaders } from '../lib/sensorConfig';

function channelTarget(c: NotificationChannel): string {
  if (c.type === 'email') return (c.config as EmailChannelConfig).to?.join(', ') ?? '';
  return (c.config as WebhookChannelConfig).url ?? '';
}

function ChannelModal({
  open,
  onClose,
  channel,
}: {
  open: boolean;
  onClose: () => void;
  channel?: NotificationChannel;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const [name, setName] = useState('');
  const [type, setType] = useState<'email' | 'webhook'>('email');
  const [enabled, setEnabled] = useState(true);
  const [to, setTo] = useState('');
  const [url, setUrl] = useState('');
  const [headers, setHeaders] = useState('');
  const [verifyTls, setVerifyTls] = useState(true);
  const [timeout, setTimeoutS] = useState('10');

  useEffect(() => {
    if (!open) return;
    setName(channel?.name ?? '');
    setType(channel?.type ?? 'email');
    setEnabled(channel?.enabled ?? true);
    const ec = channel?.type === 'email' ? (channel.config as EmailChannelConfig) : null;
    const wc = channel?.type === 'webhook' ? (channel.config as WebhookChannelConfig) : null;
    setTo(ec?.to.join('\n') ?? '');
    setUrl(wc?.url ?? '');
    setHeaders(headersToText(wc?.headers ?? {}));
    setVerifyTls(wc?.verify_tls ?? true);
    setTimeoutS(String(wc?.timeout_s ?? 10));
  }, [open, channel]);

  const m = useMutation({
    mutationFn: (body: ChannelInput) =>
      channel ? endpoints.updateChannel(channel.id, body) : endpoints.createChannel(body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.channels });
      toast.push('success', t('common.saved'));
      onClose();
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const config =
      type === 'email'
        ? {
            to: to
              .split(/[\s,;]+/)
              .map((x) => x.trim())
              .filter(Boolean),
          }
        : {
            url: url.trim(),
            headers: textToHeaders(headers),
            verify_tls: verifyTls,
            timeout_s: Number(timeout) || 10,
          };
    const body: ChannelInput = { name: name.trim(), enabled, config };
    if (!channel) body.type = type;
    m.mutate(body);
  };

  return (
    <Modal open={open} onClose={onClose} title={channel ? t('channels.edit') : t('channels.add')}>
      <form onSubmit={submit} className="space-y-3">
        <Field label={t('common.name')} required>
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            maxLength={128}
            autoFocus
          />
        </Field>
        <Field label={t('channels.type')}>
          <Select
            value={type}
            disabled={!!channel}
            onChange={(e) => setType(e.target.value as 'email' | 'webhook')}
          >
            <option value="email">{t('channels.email')}</option>
            <option value="webhook">{t('channels.webhook')}</option>
          </Select>
        </Field>
        {type === 'email' ? (
          <Field label={t('channels.recipients')} hint={t('channels.recipientsHint')} required>
            <Textarea
              rows={3}
              value={to}
              onChange={(e) => setTo(e.target.value)}
              dir="ltr"
              required
            />
          </Field>
        ) : (
          <>
            <Field label={t('channels.url')} required>
              <Input
                type="url"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                dir="ltr"
                required
              />
            </Field>
            <Field label={t('channels.headers')} hint={t('sensors.headersHint')}>
              <Textarea
                rows={3}
                value={headers}
                onChange={(e) => setHeaders(e.target.value)}
                dir="ltr"
                className="font-mono"
              />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label={t('channels.timeout')}>
                <Input
                  type="number"
                  min={1}
                  max={60}
                  value={timeout}
                  onChange={(e) => setTimeoutS(e.target.value)}
                  dir="ltr"
                />
              </Field>
              <div className="flex items-end pb-1.5">
                <Checkbox
                  label={t('channels.verifyTls')}
                  checked={verifyTls}
                  onChange={setVerifyTls}
                />
              </div>
            </div>
          </>
        )}
        <Checkbox label={t('common.enabled')} checked={enabled} onChange={setEnabled} />
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

const RULE_STATUSES = ['down', 'warning', 'up'] as const;

function RuleModal({
  open,
  onClose,
  rule,
  channels,
}: {
  open: boolean;
  onClose: () => void;
  rule?: AlertRule;
  channels: NotificationChannel[];
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const sensors = useQuery({
    queryKey: qk.sensors({}),
    queryFn: () => endpoints.sensors(),
    enabled: open,
  });
  const [name, setName] = useState('');
  const [sensorId, setSensorId] = useState<number | null>(null);
  const [statuses, setStatuses] = useState<AlertRule['on_statuses']>(['down', 'up']);
  const [delay, setDelay] = useState('0');
  const [enabled, setEnabled] = useState(true);
  const [channelIds, setChannelIds] = useState<number[]>([]);

  useEffect(() => {
    if (!open) return;
    setName(rule?.name ?? '');
    setSensorId(rule?.sensor_id ?? null);
    setStatuses(rule?.on_statuses ?? ['down', 'up']);
    setDelay(String(rule?.delay_s ?? 0));
    setEnabled(rule?.enabled ?? true);
    setChannelIds(rule?.channel_ids ?? []);
  }, [open, rule]);

  const m = useMutation({
    mutationFn: (body: AlertRuleInput) =>
      rule ? endpoints.updateAlertRule(rule.id, body) : endpoints.createAlertRule(body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['alertRules'] });
      toast.push('success', t('common.saved'));
      onClose();
    },
  });

  const valid = name.trim() && statuses.length > 0 && channelIds.length > 0;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!valid) return;
    m.mutate({
      name: name.trim(),
      sensor_id: sensorId,
      on_statuses: statuses,
      delay_s: Math.max(0, Math.min(86400, Number(delay) || 0)),
      enabled,
      channel_ids: channelIds,
    });
  };

  return (
    <Modal open={open} onClose={onClose} title={rule ? t('rules.edit') : t('rules.add')}>
      <form onSubmit={submit} className="space-y-3">
        <Field label={t('common.name')} required>
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            maxLength={128}
            autoFocus
          />
        </Field>
        <Field label={t('rules.scope')}>
          <Select
            value={sensorId === null ? '' : String(sensorId)}
            onChange={(e) => setSensorId(e.target.value ? Number(e.target.value) : null)}
          >
            <option value="">{t('rules.global')}</option>
            {sensors.data?.items.map((s) => (
              <option key={s.id} value={s.id}>
                {s.device_name} / {s.name}
              </option>
            ))}
          </Select>
        </Field>
        <Field label={t('rules.onStatuses')} hint={t('rules.onStatusesHint')}>
          <div className="flex gap-4">
            {RULE_STATUSES.map((s) => (
              <Checkbox
                key={s}
                label={t(`status.${s}`)}
                checked={statuses.includes(s)}
                onChange={(v) => setStatuses((x) => (v ? [...x, s] : x.filter((y) => y !== s)))}
              />
            ))}
          </div>
        </Field>
        <Field label={t('rules.delay')} hint={t('rules.delayHint')}>
          <Input
            type="number"
            min={0}
            max={86400}
            value={delay}
            onChange={(e) => setDelay(e.target.value)}
            dir="ltr"
          />
        </Field>
        <Field label={t('rules.channels')} required>
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
            <p className="text-sm text-slate-500">{t('rules.noChannels')}</p>
          )}
        </Field>
        <Checkbox label={t('common.enabled')} checked={enabled} onChange={setEnabled} />
        <ErrorBox error={m.error} />
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="submit" variant="primary" busy={m.isPending} disabled={!valid}>
            {t('common.save')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function NotificationsPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const channels = useQuery({ queryKey: qk.channels, queryFn: endpoints.channels });
  const rules = useQuery({ queryKey: qk.alertRules({}), queryFn: () => endpoints.alertRules() });
  const [chEdit, setChEdit] = useState<NotificationChannel | 'new' | null>(null);
  const [ruleEdit, setRuleEdit] = useState<AlertRule | 'new' | null>(null);
  const [chDel, setChDel] = useState<NotificationChannel | null>(null);
  const [ruleDel, setRuleDel] = useState<AlertRule | null>(null);

  const test = useMutation({
    mutationFn: (c: NotificationChannel) => endpoints.testChannel(c.id),
    onSuccess: (r, c) =>
      toast.push(
        r.ok ? 'success' : 'error',
        `${c.name}: ${r.ok ? t('channels.testOk') : r.message}`,
      ),
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  const delCh = useMutation({
    mutationFn: (c: NotificationChannel) => endpoints.deleteChannel(c.id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.channels });
      void qc.invalidateQueries({ queryKey: ['alertRules'] });
      setChDel(null);
    },
  });
  const delRule = useMutation({
    mutationFn: (r: AlertRule) => endpoints.deleteAlertRule(r.id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['alertRules'] });
      setRuleDel(null);
    },
  });
  const toggleRule = useMutation({
    mutationFn: (r: AlertRule) => endpoints.updateAlertRule(r.id, { enabled: !r.enabled }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['alertRules'] }),
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });

  const chName = (id: number) => channels.data?.items.find((c) => c.id === id)?.name ?? `#${id}`;

  return (
    <div className="space-y-6">
      <PageHeader title={t('notifications.title')} />
      <Card
        title={t('channels.title')}
        bodyClassName="p-0"
        actions={
          <Button size="sm" variant="primary" onClick={() => setChEdit('new')}>
            <PlusIcon size={14} /> {t('channels.add')}
          </Button>
        }
      >
        {channels.isLoading && <Loading />}
        <ErrorBox error={channels.error} className="m-3" />
        {channels.data && !channels.data.items.length && <Empty>{t('channels.none')}</Empty>}
        {channels.data && channels.data.items.length > 0 && (
          <Table>
            <thead>
              <tr>
                <th className={th}>{t('common.name')}</th>
                <th className={th}>{t('channels.type')}</th>
                <th className={th}>{t('channels.target')}</th>
                <th className={th}>{t('common.enabled')}</th>
                <th className={th} />
              </tr>
            </thead>
            <tbody>
              {channels.data.items.map((c) => (
                <tr key={c.id}>
                  <td className={`${td} font-medium`}>{c.name}</td>
                  <td className={td}>{t(`channels.${c.type}`)}</td>
                  <td className={`${td} max-w-xs truncate text-slate-500`}>
                    <Ltr>{channelTarget(c)}</Ltr>
                  </td>
                  <td className={td}>{c.enabled ? t('common.yes') : t('common.no')}</td>
                  <td className={`${td} whitespace-nowrap text-end`}>
                    <Button
                      size="sm"
                      busy={test.isPending && test.variables?.id === c.id}
                      onClick={() => test.mutate(c)}
                    >
                      {t('channels.test')}
                    </Button>{' '}
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label={t('common.edit')}
                      onClick={() => setChEdit(c)}
                    >
                      <EditIcon size={14} />
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label={t('common.delete')}
                      onClick={() => setChDel(c)}
                    >
                      <TrashIcon size={14} />
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      <Card
        title={t('rules.title')}
        bodyClassName="p-0"
        actions={
          <Button size="sm" variant="primary" onClick={() => setRuleEdit('new')}>
            <PlusIcon size={14} /> {t('rules.add')}
          </Button>
        }
      >
        {rules.isLoading && <Loading />}
        <ErrorBox error={rules.error} className="m-3" />
        {rules.data && !rules.data.items.length && <Empty>{t('rules.none')}</Empty>}
        {rules.data && rules.data.items.length > 0 && (
          <Table>
            <thead>
              <tr>
                <th className={th}>{t('common.name')}</th>
                <th className={th}>{t('rules.scope')}</th>
                <th className={th}>{t('rules.onStatuses')}</th>
                <th className={th}>{t('rules.delay')}</th>
                <th className={th}>{t('rules.channels')}</th>
                <th className={th}>{t('common.enabled')}</th>
                <th className={th} />
              </tr>
            </thead>
            <tbody>
              {rules.data.items.map((r) => (
                <tr key={r.id}>
                  <td className={`${td} font-medium`}>{r.name}</td>
                  <td className={td}>
                    {r.sensor_id === null
                      ? t('rules.global')
                      : (r.sensor_name ?? `#${r.sensor_id}`)}
                  </td>
                  <td className={td}>
                    <span className="inline-flex gap-1">
                      {r.on_statuses.map((s) => (
                        <StatusBadge key={s} status={s} />
                      ))}
                    </span>
                  </td>
                  <td className={`${td} tabular-nums`}>
                    <Ltr>{r.delay_s} s</Ltr>
                  </td>
                  <td className={td}>{r.channel_ids.map(chName).join(', ')}</td>
                  <td className={td}>
                    <Checkbox label="" checked={r.enabled} onChange={() => toggleRule.mutate(r)} />
                  </td>
                  <td className={`${td} whitespace-nowrap text-end`}>
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label={t('common.edit')}
                      onClick={() => setRuleEdit(r)}
                    >
                      <EditIcon size={14} />
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label={t('common.delete')}
                      onClick={() => setRuleDel(r)}
                    >
                      <TrashIcon size={14} />
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      <ChannelModal
        open={chEdit !== null}
        onClose={() => setChEdit(null)}
        channel={chEdit && chEdit !== 'new' ? chEdit : undefined}
      />
      <RuleModal
        open={ruleEdit !== null}
        onClose={() => setRuleEdit(null)}
        rule={ruleEdit && ruleEdit !== 'new' ? ruleEdit : undefined}
        channels={channels.data?.items ?? []}
      />
      <ConfirmDialog
        open={!!chDel}
        title={t('channels.deleteTitle')}
        message={t('common.deleteConfirm', { name: chDel?.name })}
        onConfirm={() => chDel && delCh.mutate(chDel)}
        onClose={() => setChDel(null)}
        busy={delCh.isPending}
        error={delCh.error}
      />
      <ConfirmDialog
        open={!!ruleDel}
        title={t('rules.deleteTitle')}
        message={t('common.deleteConfirm', { name: ruleDel?.name })}
        onConfirm={() => ruleDel && delRule.mutate(ruleDel)}
        onClose={() => setRuleDel(null)}
        busy={delRule.isPending}
        error={delRule.error}
      />
    </div>
  );
}
