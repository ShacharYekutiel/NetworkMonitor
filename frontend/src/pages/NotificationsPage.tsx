import { useEffect, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Trans, useTranslation } from 'react-i18next';
import { endpoints, qk, type AlertRuleInput, type ChannelInput } from '../api/endpoints';
import type {
  AlertRule,
  EmailChannelConfig,
  NotificationChannel,
  NotificationType,
  TelegramChannelConfig,
  TelegramChannelConfigInput,
  TestResult,
} from '../api/types';
import { NOTIFICATION_TYPES } from '../api/types';
import type { WebhookChannelConfig } from '../api/types';
import { ChevronIcon, EditIcon, EscalateIcon, PlusIcon, TrashIcon } from '../components/icons';
import { escalationForm, escalationToInput, type EscalationForm } from '../lib/escalation';
import { formatDuration } from '../lib/format';
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
  FieldGroup,
  Input,
  Loading,
  Ltr,
  Modal,
  Notice,
  PageHeader,
  Select,
  Table,
  Textarea,
  td,
  th,
} from '../components/ui';
import { errorMessage } from '../lib/errors';
import { headersToText, textToHeaders } from '../lib/sensorConfig';
import { isTelegramChatId, isTelegramToken } from '../lib/telegram';

function channelTarget(c: NotificationChannel): string {
  if (c.type === 'email') return (c.config as EmailChannelConfig).to?.join(', ') ?? '';
  if (c.type === 'telegram') return (c.config as TelegramChannelConfig).chat_id ?? '';
  return (c.config as WebhookChannelConfig).url ?? '';
}

/** LTR isolate for tokens (@BotFather, /newbot, -100…) inside translated sentences. */
const CODE = <bdi dir="ltr" className="font-mono text-[0.95em]" />;

function Tx({ k }: { k: string }) {
  return <Trans i18nKey={k} components={{ c: CODE }} />;
}

/** Short how-to for the Telegram fields (architecture.md § 9.7). */
function TelegramHelp() {
  const { t } = useTranslation();
  return (
    <details className="rounded-md bg-slate-50 px-3 py-2 text-xs text-slate-600 ring-1 ring-inset ring-slate-200 dark:bg-slate-800/60 dark:text-slate-300 dark:ring-slate-700">
      <summary className="cursor-pointer select-none font-medium text-slate-700 dark:text-slate-200">
        {t('telegram.helpTitle')}
      </summary>
      <ol className="mt-2 list-decimal space-y-1 ps-5">
        <li>
          <Tx k="telegram.help1" />
        </li>
        <li>{t('telegram.help2')}</li>
        <li>
          <Tx k="telegram.help3" />
          <code
            dir="ltr"
            className="mt-1 block break-all rounded bg-white px-1.5 py-0.5 text-left dark:bg-slate-900"
          >
            https://api.telegram.org/bot&lt;TOKEN&gt;/getUpdates
          </code>
        </li>
        <li>
          <Tx k="telegram.help4" />
        </li>
      </ol>
    </details>
  );
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
  const [type, setType] = useState<NotificationType>('email');
  const [enabled, setEnabled] = useState(true);
  const [to, setTo] = useState('');
  const [url, setUrl] = useState('');
  const [headers, setHeaders] = useState('');
  const [verifyTls, setVerifyTls] = useState(true);
  const [timeout, setTimeoutS] = useState('10');
  const [botToken, setBotToken] = useState('');
  const [chatId, setChatId] = useState('');
  const [parseMode, setParseMode] = useState<'HTML' | ''>('HTML');
  const [silent, setSilent] = useState(false);
  const [touched, setTouched] = useState(false);
  const [testResult, setTestResult] = useState<TestResult | null>(null);

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
    const tc = channel?.type === 'telegram' ? (channel.config as TelegramChannelConfig) : null;
    setBotToken('');
    setChatId(tc?.chat_id ?? '');
    setParseMode(tc ? (tc.parse_mode ?? '') : 'HTML');
    setSilent(tc?.disable_notification ?? false);
    setTouched(false);
    setTestResult(null);
    test.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, channel]);

  const tokenSet =
    channel?.type === 'telegram' && (channel.config as TelegramChannelConfig).bot_token_set;
  const tokenErr =
    type !== 'telegram'
      ? null
      : botToken.trim()
        ? isTelegramToken(botToken.trim())
          ? null
          : t('telegram.tokenInvalid')
        : tokenSet
          ? null
          : t('validation.required');
  const chatErr =
    type !== 'telegram'
      ? null
      : !chatId.trim()
        ? t('validation.required')
        : isTelegramChatId(chatId.trim())
          ? null
          : t('telegram.chatIdInvalid');

  const test = useMutation({
    mutationFn: () => endpoints.testChannel(channel!.id),
    onSuccess: setTestResult,
    onMutate: () => setTestResult(null),
  });

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
    setTouched(true);
    if (tokenErr || chatErr) return;
    const telegram: TelegramChannelConfigInput = {
      chat_id: chatId.trim(),
      parse_mode: parseMode || null,
      disable_notification: silent,
    };
    // Write-only secret: omitted keeps the stored token on PATCH (api.md § telegram).
    if (botToken.trim()) telegram.bot_token = botToken.trim();
    const config =
      type === 'telegram'
        ? telegram
        : type === 'email'
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
            onChange={(e) => setType(e.target.value as NotificationType)}
          >
            {NOTIFICATION_TYPES.map((nt) => (
              <option key={nt} value={nt}>
                {t(`channels.${nt}`)}
              </option>
            ))}
          </Select>
        </Field>
        {type === 'telegram' ? (
          <>
            <TelegramHelp />
            <Field
              label={t('telegram.botToken')}
              required={!tokenSet}
              hint={
                touched && tokenErr ? (
                  <span className="text-red-600">{tokenErr}</span>
                ) : tokenSet ? (
                  t('secret.keepHint')
                ) : (
                  <Tx k="telegram.botTokenHint" />
                )
              }
            >
              <Input
                type="password"
                autoComplete="new-password"
                value={botToken}
                placeholder={tokenSet ? t('secret.stored') : '123456789:AAE…'}
                onChange={(e) => setBotToken(e.target.value)}
                dir="ltr"
                aria-invalid={touched && !!tokenErr}
              />
            </Field>
            <Field
              label={t('telegram.chatId')}
              required
              hint={
                touched && chatErr ? (
                  <span className="text-red-600">{chatErr}</span>
                ) : (
                  <Tx k="telegram.chatIdHint" />
                )
              }
            >
              <Input
                value={chatId}
                placeholder="-1001234567890"
                onChange={(e) => setChatId(e.target.value)}
                dir="ltr"
                aria-invalid={touched && !!chatErr}
              />
            </Field>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field label={t('telegram.parseMode')}>
                <Select
                  value={parseMode}
                  onChange={(e) => setParseMode(e.target.value as 'HTML' | '')}
                >
                  <option value="HTML">{t('telegram.parseHtml')}</option>
                  <option value="">{t('telegram.parsePlain')}</option>
                </Select>
              </Field>
              <div className="flex items-end pb-1.5">
                <Checkbox label={t('telegram.silent')} checked={silent} onChange={setSilent} />
              </div>
            </div>
          </>
        ) : type === 'email' ? (
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
        <ErrorBox error={test.error} />
        {testResult && (
          <Notice tone={testResult.ok ? 'success' : 'info'}>
            {testResult.ok ? t('channels.testSent') : testResult.message}
          </Notice>
        )}
        <div className="flex flex-wrap justify-end gap-2">
          {channel && (
            <Button
              className="me-auto"
              busy={test.isPending}
              title={t('channels.testSavedHint')}
              onClick={() => test.mutate()}
            >
              {t('channels.test')}
            </Button>
          )}
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
  const [esc, setEsc] = useState<EscalationForm>(escalationForm());
  const [escOpen, setEscOpen] = useState(false);
  const setE = <K extends keyof EscalationForm>(k: K, v: EscalationForm[K]) =>
    setEsc((x) => ({ ...x, [k]: v }));

  useEffect(() => {
    if (!open) return;
    const ef = escalationForm(rule);
    setEsc(ef);
    setEscOpen(ef.escalate || ef.remind);
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

  const delayS = Math.max(0, Math.min(86400, Number(delay) || 0));
  const escResult = escalationToInput(esc, { delay_s: delayS, on_statuses: statuses });
  const escErrors = escResult.errors;
  const valid =
    name.trim() && statuses.length > 0 && channelIds.length > 0 && !Object.keys(escErrors).length;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!valid) return;
    m.mutate({
      name: name.trim(),
      sensor_id: sensorId,
      on_statuses: statuses,
      delay_s: delayS,
      enabled,
      channel_ids: channelIds,
      ...escResult.body,
    });
  };
  const EscErr = ({ k }: { k?: string }) =>
    k ? <span className="text-red-600">{t(k)}</span> : null;

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
        <FieldGroup label={t('rules.onStatuses')} hint={t('rules.onStatusesHint')}>
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
        </FieldGroup>
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
        <FieldGroup label={t('rules.channels')} required>
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
        </FieldGroup>
        <section className="rounded-md border border-slate-200 dark:border-slate-700">
          <button
            type="button"
            onClick={() => setEscOpen((o) => !o)}
            aria-expanded={escOpen}
            className="flex w-full items-center gap-2 px-3 py-2 text-start text-sm font-semibold"
          >
            <ChevronIcon
              size={14}
              className={
                escOpen ? 'rotate-90 transition-transform' : 'transition-transform rtl:rotate-180'
              }
            />
            <EscalateIcon size={14} />
            {t('escalation.title')}
            {(esc.escalate || esc.remind) && (
              <span className="ms-auto rounded bg-sky-100 px-1.5 text-xs font-medium text-sky-800 dark:bg-sky-900 dark:text-sky-100">
                {t('escalation.on')}
              </span>
            )}
          </button>
          {escOpen && (
            <div className="space-y-3 border-t border-slate-200 p-3 dark:border-slate-700">
              <p className="text-xs text-slate-500">{t('escalation.help')}</p>
              {escErrors.statuses && (
                <p className="text-xs">
                  <EscErr k={escErrors.statuses} />
                </p>
              )}
              <Checkbox
                label={t('escalation.enable')}
                checked={esc.escalate}
                onChange={(v) => setE('escalate', v)}
              />
              {esc.escalate && (
                <div className="space-y-3 ps-6">
                  <Field
                    label={t('escalation.after')}
                    hint={
                      escErrors.escalation_after ? (
                        <EscErr k={escErrors.escalation_after} />
                      ) : (
                        t('escalation.afterHint')
                      )
                    }
                  >
                    <div className="flex items-center gap-2">
                      <Input
                        type="number"
                        min={1}
                        max={10080}
                        step="any"
                        value={esc.escalation_after_min}
                        onChange={(e) => setE('escalation_after_min', e.target.value)}
                        dir="ltr"
                        className="w-28"
                        aria-invalid={!!escErrors.escalation_after}
                      />
                      <span className="text-sm text-slate-500">{t('escalation.minutes')}</span>
                    </div>
                  </Field>
                  <FieldGroup
                    label={t('escalation.channels')}
                    required
                    hint={
                      escErrors.escalation_channels ? (
                        <EscErr k={escErrors.escalation_channels} />
                      ) : (
                        t('escalation.channelsHint')
                      )
                    }
                  >
                    <div className="flex flex-col gap-1">
                      {channels.map((c) => (
                        <Checkbox
                          key={c.id}
                          label={`${c.name} (${t(`channels.${c.type}`)})`}
                          checked={esc.escalation_channel_ids.includes(c.id)}
                          onChange={(v) =>
                            setE(
                              'escalation_channel_ids',
                              v
                                ? [...esc.escalation_channel_ids, c.id]
                                : esc.escalation_channel_ids.filter((y) => y !== c.id),
                            )
                          }
                        />
                      ))}
                    </div>
                  </FieldGroup>
                </div>
              )}
              <Checkbox
                label={t('escalation.remindEnable')}
                checked={esc.remind}
                onChange={(v) => setE('remind', v)}
              />
              {esc.remind && (
                <div className="ps-6">
                  <Field
                    label={t('escalation.repeat')}
                    hint={
                      escErrors.repeat_every ? (
                        <EscErr k={escErrors.repeat_every} />
                      ) : (
                        t('escalation.repeatHint')
                      )
                    }
                  >
                    <div className="flex items-center gap-2">
                      <Input
                        type="number"
                        min={5}
                        max={10080}
                        step="any"
                        value={esc.repeat_every_min}
                        onChange={(e) => setE('repeat_every_min', e.target.value)}
                        dir="ltr"
                        className="w-28"
                        aria-invalid={!!escErrors.repeat_every}
                      />
                      <span className="text-sm text-slate-500">{t('escalation.minutes')}</span>
                    </div>
                  </Field>
                </div>
              )}
            </div>
          )}
        </section>
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
                  <td className={`${td} font-medium`}>
                    <span className="inline-flex items-center gap-1.5">
                      {r.name}
                      {(r.escalation_after_s != null || r.repeat_every_s != null) && (
                        <span
                          className="text-sky-600 dark:text-sky-400"
                          title={[
                            r.escalation_after_s != null &&
                              t('escalation.summary', {
                                after: formatDuration(r.escalation_after_s),
                                channels: r.escalation_channel_ids.map(chName).join(', '),
                              }),
                            r.repeat_every_s != null &&
                              t('escalation.summaryRepeat', {
                                every: formatDuration(r.repeat_every_s),
                              }),
                          ]
                            .filter(Boolean)
                            .join(' · ')}
                        >
                          <EscalateIcon
                            size={13}
                            aria-hidden={false}
                            aria-label={t('escalation.title')}
                          />
                        </span>
                      )}
                    </span>
                  </td>
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
