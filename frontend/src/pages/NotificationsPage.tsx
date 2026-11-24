import { useEffect, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Trans, useTranslation } from 'react-i18next';
import { useAiFeature } from '../components/assistant/common';
import { DraftBanner, RuleDraftBox } from '../components/assistant/RuleDraft';
import { alertDraftToForm } from '../lib/drafts';
import type { Severity } from '../api/severity';
import { SeverityChips } from '../components/severity/Severity';
import { useFeature } from '../hooks/features';
import type { RuleDraft } from '../api/types';
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
import {
  BellIcon,
  ChevronIcon,
  EditIcon,
  EscalateIcon,
  LinkIcon,
  PlusIcon,
  TrashIcon,
  UsersIcon,
} from '../components/icons';
import { ActionRow, EmptyState, Timeline, TimelineEntry } from '../components/kit';
import type { TFunction } from 'i18next';
import { escalationForm, escalationToInput, type EscalationForm } from '../lib/escalation';
import { formatDuration, formatDurationCol } from '../lib/format';
import { useToast } from '../components/toast';
import {
  Button,
  Card,
  Checkbox,
  ConfirmDialog,
  ErrorBox,
  Field,
  FieldGroup,
  GroupList,
  GroupTitle,
  Input,
  Loading,
  Ltr,
  Modal,
  Notice,
  PageHeader,
  Select,
  Switch,
  Textarea,
} from '../components/ui';
import { errorMessage } from '../lib/errors';
import { headersToText, textToHeaders } from '../lib/sensorConfig';
import { isTelegramChatId, isTelegramToken } from '../lib/telegram';
import { ChatAckFields } from '../components/chatack/ChannelAckFields';
import type { TeamsChannelConfig, TeamsChannelConfigInput } from '../api/types';
import { isLegacyTeamsHost, teamsUrlError, urlHost } from '../lib/teams';
import { defaultRuleInput, noActiveRule, offerDefaultRuleWithChannel } from '../lib/defaultRule';

function channelTarget(c: NotificationChannel): string {
  if (c.type === 'email') return (c.config as EmailChannelConfig).to?.join(', ') ?? '';
  if (c.type === 'telegram') return (c.config as TelegramChannelConfig).chat_id ?? '';
  if (c.type === 'teams') return (c.config as TeamsChannelConfig).url_host ?? '';
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
    <details className="rounded-lg bg-chip px-4 py-2.5 text-xs text-ink-muted">
      <summary className="cursor-pointer select-none font-medium text-ink">
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
            className="mt-1 block break-all rounded bg-surface px-1.5 py-0.5 text-left"
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
  offerDefaultRule = false,
}: {
  open: boolean;
  onClose: () => void;
  channel?: NotificationChannel;
  /** no alert rule exists yet: offer to create the default rule with this (new) channel */
  offerDefaultRule?: boolean;
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
  const [teamsUrl, setTeamsUrl] = useState('');
  // [3] chat acknowledge
  const [ackButtons, setAckButtons] = useState(false);
  const [ackLink, setAckLink] = useState(false);
  // [3b] Telegram /ask and the email AI-note reply
  const chatAskOn = useAiFeature('ai_chat_ask');
  const notesOn = useAiFeature('ai_alert_notes');
  const [aiAsk, setAiAsk] = useState(false);
  const [aiNoteEmail, setAiNoteEmail] = useState(true);
  const [withDefaultRule, setWithDefaultRule] = useState(true);
  const showDefaultRule = !channel && offerDefaultRule;

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
    setTeamsUrl('');
    // [3] defaults (data-model § Channel config additions): teams ack_link true, others false
    const cfg = (channel?.config ?? {}) as { ack_buttons?: boolean; ack_link?: boolean };
    setAckButtons(cfg.ack_buttons ?? false);
    setAckLink(cfg.ack_link ?? (channel ? channel.type === 'teams' : false));
    const c3b = (channel?.config ?? {}) as { ai_ask?: boolean; ai_note_email?: boolean };
    setAiAsk(c3b.ai_ask ?? false);
    setAiNoteEmail(c3b.ai_note_email ?? true);
    setWithDefaultRule(true);
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

  // [2b-3] Teams: the URL is write-only (url_set / url_host / legacy in responses).
  const teamsCfg = channel?.type === 'teams' ? (channel.config as TeamsChannelConfig) : null;
  const teamsErr = type === 'teams' ? teamsUrlError(teamsUrl, !teamsCfg?.url_set) : null;
  const teamsLegacy = teamsUrl.trim() ? isLegacyTeamsHost(urlHost(teamsUrl)) : !!teamsCfg?.legacy;

  const test = useMutation({
    mutationFn: () => endpoints.testChannel(channel!.id),
    onSuccess: setTestResult,
    onMutate: () => setTestResult(null),
  });

  const m = useMutation({
    mutationFn: async (body: ChannelInput): Promise<{ ruleMade: boolean; ruleError: unknown }> => {
      if (channel) {
        await endpoints.updateChannel(channel.id, body);
        return { ruleMade: false, ruleError: null };
      }
      const created = await endpoints.createChannel(body);
      if (!(showDefaultRule && withDefaultRule)) return { ruleMade: false, ruleError: null };
      // the channel is saved either way; a failed rule is reported, not rolled back
      try {
        await endpoints.createAlertRule(defaultRuleInput(t, [created.id]));
        return { ruleMade: true, ruleError: null };
      } catch (e) {
        return { ruleMade: false, ruleError: e };
      }
    },
    onSuccess: ({ ruleMade, ruleError }) => {
      void qc.invalidateQueries({ queryKey: qk.channels });
      if (ruleMade || ruleError) void qc.invalidateQueries({ queryKey: ['alertRules'] });
      toast.push('success', t('common.saved'));
      if (ruleMade) toast.push('success', t('rules.defaultRule.created'));
      if (ruleError)
        toast.push(
          'error',
          t('rules.defaultRule.createFailed', { error: errorMessage(t, ruleError) }),
        );
      onClose();
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (tokenErr || chatErr || teamsErr) return;
    const telegram: TelegramChannelConfigInput = {
      chat_id: chatId.trim(),
      parse_mode: parseMode || null,
      disable_notification: silent,
      ack_buttons: ackButtons,
      ack_link: ackLink,
      ai_ask: aiAsk,
    };
    // Write-only secret: omitted keeps the stored token on PATCH (api.md § telegram).
    if (botToken.trim()) telegram.bot_token = botToken.trim();
    const teams: TeamsChannelConfigInput = { ack_link: ackLink };
    if (teamsUrl.trim()) teams.url = teamsUrl.trim();
    const config =
      type === 'teams'
        ? teams
        : type === 'telegram'
          ? telegram
          : type === 'email'
            ? {
                to: to
                  .split(/[\s,;]+/)
                  .map((x) => x.trim())
                  .filter(Boolean),
                ack_link: ackLink,
                ai_note_email: aiNoteEmail,
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
            onChange={(e) => {
              const v = e.target.value as NotificationType;
              setType(v);
              setAckLink(v === 'teams');
            }}
          >
            {NOTIFICATION_TYPES.map((nt) => (
              <option key={nt} value={nt}>
                {t(`channels.${nt}`)}
              </option>
            ))}
          </Select>
        </Field>
        {type === 'teams' ? (
          <>
            <p className="text-xs text-ink-muted">{t('teams.help')}</p>
            <Field
              label={t('teams.url')}
              required={!teamsCfg?.url_set}
              hint={
                touched && teamsErr ? (
                  <span className="text-status-down">{t(teamsErr)}</span>
                ) : teamsCfg?.url_set ? (
                  t('teams.storedHint', { host: teamsCfg.url_host ?? '' })
                ) : (
                  t('teams.urlHint')
                )
              }
            >
              <Input
                type="password"
                autoComplete="new-password"
                value={teamsUrl}
                placeholder={
                  teamsCfg?.url_set ? t('secret.stored') : 'https://…logic.azure.com/workflows/…'
                }
                onChange={(e) => setTeamsUrl(e.target.value)}
                dir="ltr"
                aria-invalid={touched && !!teamsErr}
              />
            </Field>
            {teamsLegacy && (
              <div
                role="alert"
                className="rounded-md bg-status-warning-soft px-3 py-2 text-sm text-status-warning"
                data-testid="teams-legacy"
              >
                {t('teams.legacy')}
              </div>
            )}
          </>
        ) : type === 'telegram' ? (
          <>
            <TelegramHelp />
            <Field
              label={t('telegram.botToken')}
              required={!tokenSet}
              hint={
                touched && tokenErr ? (
                  <span className="text-status-down">{tokenErr}</span>
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
                  <span className="text-status-down">{chatErr}</span>
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
        {type !== 'webhook' && (
          <ChatAckFields
            type={type}
            channel={channel}
            ackButtons={ackButtons}
            ackLink={ackLink}
            onAckButtons={setAckButtons}
            onAckLink={setAckLink}
          />
        )}
        {type === 'telegram' && chatAskOn && (
          <div>
            <Checkbox label={t('channels.aiAsk')} checked={aiAsk} onChange={setAiAsk} />
            <p className="ms-6 mt-0.5 text-xs text-ink-muted">{t('channels.aiAskHint')}</p>
          </div>
        )}
        {type === 'email' && notesOn && (
          <div>
            <Checkbox
              label={t('channels.aiNoteEmail')}
              checked={aiNoteEmail}
              onChange={setAiNoteEmail}
            />
            <p className="ms-6 mt-0.5 text-xs text-ink-muted">{t('channels.aiNoteEmailHint')}</p>
          </div>
        )}
        <Checkbox label={t('common.enabled')} checked={enabled} onChange={setEnabled} />
        {showDefaultRule && (
          <div data-testid="default-rule-option">
            <Checkbox
              label={t('rules.defaultRule.create')}
              checked={withDefaultRule}
              onChange={setWithDefaultRule}
            />
            <p className="ms-6 mt-0.5 text-xs text-ink-muted">
              {t('rules.defaultRule.createHint')}
            </p>
          </div>
        )}
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
  // [3b] AI note opt-in and "describe the rule" drafts
  const aiNotes = useAiFeature('ai_alert_notes');
  const [aiNote, setAiNote] = useState(false);
  // [severity] the severities the rule applies to ([] = all), only while the feature is on
  const severityOn = useFeature('alert_severity');
  const [severities, setSeverities] = useState<Severity[]>([]);
  const [draft, setDraft] = useState<RuleDraft | null>(null);
  const applyDraft = (d: RuleDraft) => {
    const f = alertDraftToForm(d.draft ?? {});
    setDraft(d);
    if (f.name !== undefined) setName(f.name);
    if (f.sensor_id !== undefined) setSensorId(f.sensor_id);
    if (f.on_statuses?.length) setStatuses(f.on_statuses);
    if (f.delay_s !== undefined) setDelay(String(f.delay_s));
    if (f.enabled !== undefined) setEnabled(f.enabled);
    if (f.channel_ids)
      setChannelIds(f.channel_ids.filter((id) => channels.some((c) => c.id === id)));
    if (f.ai_note !== undefined) setAiNote(f.ai_note);
    const ef = escalationForm({
      escalation_after_s: f.escalation_after_s ?? null,
      escalation_channel_ids: f.escalation_channel_ids ?? [],
      repeat_every_s: f.repeat_every_s ?? null,
    });
    setEsc(ef);
    setEscOpen(ef.escalate || ef.remind);
  };
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
    setAiNote(rule?.ai_note ?? false);
    setSeverities(rule?.severities ?? []);
    setDraft(null);
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
      ...(aiNotes ? { ai_note: aiNote } : {}),
      ...(severityOn ? { severities } : {}),
    });
  };
  const EscErr = ({ k }: { k?: string }) =>
    k ? <span className="text-status-down">{t(k)}</span> : null;

  return (
    <Modal open={open} onClose={onClose} title={rule ? t('rules.edit') : t('rules.add')}>
      <form onSubmit={submit} className="space-y-3">
        {!rule && <RuleDraftBox kind="alert_rule" onDraft={applyDraft} />}
        {draft && <DraftBanner draft={draft} onDismiss={() => setDraft(null)} />}
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
        {severityOn && (
          <FieldGroup label={t('severity.rule.label')} hint={t('severity.rule.hint')}>
            <SeverityChips
              label={t('severity.rule.label')}
              testId="rule-severities"
              value={severities}
              onChange={setSeverities}
            />
          </FieldGroup>
        )}
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
            <p className="text-sm text-ink-muted">{t('rules.noChannels')}</p>
          )}
        </FieldGroup>
        <section className="rounded-lg border border-line">
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
              <span className="ms-auto rounded bg-accent-soft px-1.5 text-xs font-medium text-accent-ink">
                {t('escalation.on')}
              </span>
            )}
          </button>
          {escOpen && (
            <div className="space-y-3 border-t border-line p-3">
              <p className="text-xs text-ink-muted">{t('escalation.help')}</p>
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
                        className="!w-28"
                        aria-invalid={!!escErrors.escalation_after}
                      />
                      <span className="text-sm text-ink-muted">{t('escalation.minutes')}</span>
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
                        className="!w-28"
                        aria-invalid={!!escErrors.repeat_every}
                      />
                      <span className="text-sm text-ink-muted">{t('escalation.minutes')}</span>
                    </div>
                  </Field>
                </div>
              )}
            </div>
          )}
        </section>
        {aiNotes && (
          <div>
            <Checkbox label={t('rules.aiNote')} checked={aiNote} onChange={setAiNote} />
            <p className="ms-6 mt-0.5 text-xs text-ink-muted">{t('rules.aiNoteHint')}</p>
          </div>
        )}
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

const CHANNEL_ICON: Record<NotificationType, typeof BellIcon> = {
  email: BellIcon,
  telegram: BellIcon,
  teams: UsersIcon,
  webhook: LinkIcon,
};

/** Rule row subline: "Down + Warning · after 1 min · escalates after 30 min · NOC, Telegram". */
function ruleSummary(
  t: TFunction,
  r: AlertRule,
  chName: (id: number) => string,
  severityOn = false,
): string {
  const parts = [r.on_statuses.map((s) => t(`status.${s}`)).join(' + ')];
  // [severity] "Severities: Critical, Major" (nothing when the rule applies to every severity)
  if (severityOn && r.severities?.length)
    parts.push(
      t('severity.rule.summary', {
        list: r.severities.map((s) => t(`severity.level.${s}`)).join(', '),
      }),
    );
  parts.push(
    r.delay_s
      ? t('notifications.afterDelay', { d: formatDuration(r.delay_s) })
      : t('notifications.now'),
  );
  if (r.escalation_after_s != null)
    parts.push(t('notifications.escalatesAfter', { d: formatDuration(r.escalation_after_s) }));
  if (r.repeat_every_s != null)
    parts.push(t('notifications.remindsEvery', { d: formatDuration(r.repeat_every_s) }));
  parts.push(r.channel_ids.map(chName).join(', '));
  return parts.join(' · ');
}

/** What happens when the rule fires (prototype "אסקלציה · …" card): a timeline from the rule. */
function RuleFlowCard({ rule, chName }: { rule: AlertRule; chName: (id: number) => string }) {
  const { t } = useTranslation();
  const bad = rule.on_statuses.filter((s) => s !== 'up');
  const startTone = bad.includes('down') ? 'down' : bad.length ? 'warning' : 'up';
  return (
    <Card
      title={t('notifications.flowTitle', { name: rule.name })}
      className="mt-[22px]"
      bodyClassName="px-[22px] pb-3 pt-1"
    >
      <div data-testid="rule-flow">
        <Timeline label={t('notifications.flowTitle', { name: rule.name })}>
          <TimelineEntry
            time={formatDurationCol(0)}
            tone={startTone}
            title={t('notifications.tlStart')}
          >
            {(bad.length ? bad : rule.on_statuses).map((s) => t(`status.${s}`)).join(' + ')}
          </TimelineEntry>
          <TimelineEntry
            time={formatDurationCol(rule.delay_s)}
            tone="warning"
            title={rule.channel_ids.map(chName).join(', ')}
          >
            {rule.delay_s ? t('notifications.tlNotifyStill') : t('notifications.tlNotify')}
          </TimelineEntry>
          {rule.escalation_after_s != null && (
            <TimelineEntry
              time={formatDurationCol(rule.escalation_after_s)}
              tone="warning"
              title={rule.escalation_channel_ids.map(chName).join(', ')}
            >
              {t('notifications.tlEscalate')}
            </TimelineEntry>
          )}
          {rule.repeat_every_s != null && (
            <TimelineEntry
              time={t('notifications.tlEvery', { d: formatDurationCol(rule.repeat_every_s) })}
              tone="paused"
              title={t('notifications.tlRemind')}
            >
              {rule.channel_ids.map(chName).join(', ')}
            </TimelineEntry>
          )}
          {rule.on_statuses.includes('up') && (
            <TimelineEntry time="✓" tone="up" title={t('notifications.tlRecovery')}>
              {t('notifications.tlRecoveryHint')}
            </TimelineEntry>
          )}
        </Timeline>
        {rule.escalation_after_s == null && rule.repeat_every_s == null && (
          <p className="m-0 pb-2 pt-3 text-sm text-ink-muted">{t('notifications.noEscalation')}</p>
        )}
      </div>
    </Card>
  );
}

export function NotificationsPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const channels = useQuery({ queryKey: qk.channels, queryFn: endpoints.channels });
  const rules = useQuery({ queryKey: qk.alertRules({}), queryFn: () => endpoints.alertRules() });
  const severityOn = useFeature('alert_severity'); // [severity]
  const [chEdit, setChEdit] = useState<NotificationChannel | 'new' | null>(null);
  const [ruleEdit, setRuleEdit] = useState<AlertRule | 'new' | null>(null);
  const [chDel, setChDel] = useState<NotificationChannel | null>(null);
  const [ruleDel, setRuleDel] = useState<AlertRule | null>(null);
  const [selId, setSelId] = useState<number | null>(null);

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
  const toggleCh = useMutation({
    mutationFn: (c: NotificationChannel) => endpoints.updateChannel(c.id, { enabled: !c.enabled }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk.channels }),
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  // channels but no enabled rule: one click creates the default rule for every enabled channel
  const enabledChannels = channels.data?.items.filter((c) => c.enabled) ?? [];
  const makeDefaultRule = useMutation({
    mutationFn: () =>
      endpoints.createAlertRule(
        defaultRuleInput(
          t,
          enabledChannels.map((c) => c.id),
        ),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['alertRules'] });
      toast.push('success', t('rules.defaultRule.created'));
    },
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });

  const chName = (id: number) => channels.data?.items.find((c) => c.id === id)?.name ?? `#${id}`;
  const ruleItems = rules.data?.items ?? [];
  // the card shows the chosen rule, else the first one with an escalation, else the first one
  const shown =
    ruleItems.find((r) => r.id === selId) ??
    ruleItems.find((r) => r.escalation_after_s != null || r.repeat_every_s != null) ??
    ruleItems[0];

  return (
    <div>
      <PageHeader
        title={t('notifications.title')}
        subtitle={t('notifications.subtitle')}
        actions={
          <>
            <Button variant="primary" onClick={() => setChEdit('new')}>
              <PlusIcon size={14} /> {t('channels.add')}
            </Button>
            <Button onClick={() => setRuleEdit('new')}>
              <PlusIcon size={14} /> {t('rules.add')}
            </Button>
          </>
        }
      />
      {noActiveRule(channels.data?.items, rules.data?.items) && (
        <Notice tone="warning" className="mb-6">
          <div
            role="status"
            data-testid="no-active-rule"
            className="flex flex-wrap items-center gap-3"
          >
            <div className="min-w-0 flex-1">
              <p className="m-0 font-semibold">{t('rules.defaultRule.bannerTitle')}</p>
              <p className="m-0 mt-0.5 text-[13px] text-ink-muted">
                {ruleItems.some((r) => r.enabled) && (
                  <>{t('rules.defaultRule.bannerChannelsOff')} </>
                )}
                {enabledChannels.length ? (
                  <Trans
                    i18nKey="rules.defaultRule.bannerHint"
                    components={{
                      list: (
                        <span>
                          {enabledChannels.map((c, i) => (
                            <span key={c.id}>
                              {i > 0 && ', '}
                              <bdi>{c.name}</bdi>
                            </span>
                          ))}
                        </span>
                      ),
                    }}
                  />
                ) : (
                  t('rules.defaultRule.bannerNoChannel')
                )}
              </p>
            </div>
            {enabledChannels.length > 0 && (
              <Button
                variant="primary"
                busy={makeDefaultRule.isPending}
                onClick={() => makeDefaultRule.mutate()}
              >
                <PlusIcon size={14} /> {t('rules.defaultRule.bannerButton')}
              </Button>
            )}
          </div>
        </Notice>
      )}
      {/* prototype .two.even: channels | rules + the flow of one rule */}
      <div className="grid grid-cols-1 items-start gap-6 lg:grid-cols-2">
        <section aria-labelledby="nt-channels" data-testid="channels">
          <GroupTitle id="nt-channels">{t('channels.title')}</GroupTitle>
          {channels.isLoading && <Loading />}
          <ErrorBox error={channels.error} className="mb-3" />
          {channels.data && !channels.data.items.length && (
            <div className="rounded-xl bg-surface shadow-card">
              <EmptyState
                icon={BellIcon}
                title={t('channels.none')}
                actions={
                  <Button variant="primary" onClick={() => setChEdit('new')}>
                    <PlusIcon size={14} /> {t('channels.addFirst')}
                  </Button>
                }
              >
                {t('channels.noneHint')}
              </EmptyState>
            </div>
          )}
          {channels.data && channels.data.items.length > 0 && (
            <GroupList label={t('channels.title')}>
              {channels.data.items.map((c) => {
                const Icon = CHANNEL_ICON[c.type] ?? BellIcon;
                const legacy = c.type === 'teams' && (c.config as TeamsChannelConfig).legacy;
                return (
                  <ActionRow
                    key={c.id}
                    testId="channel-row"
                    wrap
                    dim={!c.enabled}
                    icon={<Icon size={17} />}
                    onClick={() => setChEdit(c)}
                    title={<bdi dir="auto">{c.name}</bdi>}
                    subtitle={
                      <>
                        {t(`channels.${c.type}`)}
                        {channelTarget(c) && (
                          <>
                            {' · '}
                            <Ltr>{channelTarget(c)}</Ltr>
                          </>
                        )}
                        {legacy && (
                          <span className="ms-1 text-status-warning" title={t('teams.legacy')}>
                            ({t('teams.legacyShort')})
                          </span>
                        )}
                      </>
                    }
                    end={
                      <>
                        <Button
                          size="sm"
                          busy={test.isPending && test.variables?.id === c.id}
                          onClick={() => test.mutate(c)}
                        >
                          {t('channels.test')}
                        </Button>
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
                        <Switch
                          checked={c.enabled}
                          onChange={() => toggleCh.mutate(c)}
                          disabled={toggleCh.isPending && toggleCh.variables?.id === c.id}
                          label={t('channels.enabledLabel', { name: c.name })}
                          className="ms-1"
                        />
                      </>
                    }
                  />
                );
              })}
            </GroupList>
          )}
        </section>

        <section aria-labelledby="nt-rules" data-testid="rules">
          <GroupTitle id="nt-rules">{t('rules.title')}</GroupTitle>
          {rules.isLoading && <Loading />}
          <ErrorBox error={rules.error} className="mb-3" />
          {rules.data && !ruleItems.length && (
            <div className="rounded-xl bg-surface shadow-card">
              <EmptyState
                icon={EscalateIcon}
                title={t('rules.none')}
                actions={
                  channels.data?.items.length ? (
                    <Button variant="primary" onClick={() => setRuleEdit('new')}>
                      <PlusIcon size={14} /> {t('rules.addFirst')}
                    </Button>
                  ) : undefined
                }
              >
                {channels.data?.items.length ? t('rules.noneHint') : t('rules.noChannels')}
              </EmptyState>
            </div>
          )}
          {ruleItems.length > 0 && (
            <GroupList label={t('rules.title')}>
              {ruleItems.map((r) => (
                <ActionRow
                  key={r.id}
                  testId="rule-row"
                  wrap
                  dim={!r.enabled}
                  selected={shown?.id === r.id}
                  icon={
                    r.escalation_after_s != null || r.repeat_every_s != null ? (
                      <EscalateIcon
                        size={17}
                        aria-hidden={false}
                        aria-label={t('escalation.title')}
                      />
                    ) : (
                      <BellIcon size={17} />
                    )
                  }
                  onClick={() => setSelId(r.id)}
                  title={
                    <>
                      <bdi dir="auto">{r.name}</bdi>
                      <span className="ms-2 text-[13px] font-normal text-ink-muted">
                        {r.sensor_id === null
                          ? t('rules.global')
                          : (r.sensor_name ?? `#${r.sensor_id}`)}
                      </span>
                    </>
                  }
                  subtitle={ruleSummary(t, r, chName, severityOn)}
                  end={
                    <>
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
                      <Switch
                        checked={r.enabled}
                        onChange={() => toggleRule.mutate(r)}
                        disabled={toggleRule.isPending && toggleRule.variables?.id === r.id}
                        label={t('rules.enabledLabel', { name: r.name })}
                        className="ms-1"
                      />
                    </>
                  }
                />
              ))}
            </GroupList>
          )}
          {shown && <RuleFlowCard rule={shown} chName={chName} />}
        </section>
      </div>

      <ChannelModal
        open={chEdit !== null}
        onClose={() => setChEdit(null)}
        channel={chEdit && chEdit !== 'new' ? chEdit : undefined}
        offerDefaultRule={offerDefaultRuleWithChannel(rules.data?.items)}
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
