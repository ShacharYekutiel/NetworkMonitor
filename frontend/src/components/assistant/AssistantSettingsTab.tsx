import { useEffect, useState, type FormEvent, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { endpoints, qk } from '../../api/endpoints';
import { endpoints3b, qk3b } from '../../api/endpoints3b';
import {
  AI_FEATURES,
  type AiFeatureName,
  type AssistantPrices,
  type AssistantSettings,
  type KnowledgeInput,
  type KnowledgeTestResult,
  type Settings,
  type Settings3b,
} from '../../api/types';
import { compactTokens, formatCost, parseCidrs, pctTone } from '../../lib/assistant';
import { featureHints, featuresFromSettings } from '../../lib/features';
import { formatDateTime } from '../../lib/format';
import { cardAnchor } from '../../lib/settingsIndex';
import { Toggle, useSaveSettings } from '../features/common';
import { useFeatureToggle } from '../features/FeatureToggle';
import { CheckIcon, XIcon } from '../icons';
import { useToast } from '../toast';
import {
  Button,
  Card,
  Checkbox,
  cx,
  ErrorBox,
  Field,
  FieldGroup,
  Input,
  Loading,
  Select,
  Textarea,
} from '../ui';
import { useAssistantErrorText, useAssistantStatus } from './common';
import { ChatModelsCard } from './ChatModelsCard';
import { ConnectionSettings } from './ConnectionSettings';

// ---------------------------------------------------------------------------
// Scalar settings (PUT /api/settings)

type ScalarKey = keyof Settings3b;
type FieldSpec =
  | { key: ScalarKey; kind: 'int'; min: number; max: number; hint?: string }
  | { key: ScalarKey; kind: 'money'; hint?: string }
  | { key: ScalarKey; kind: 'bool'; hint?: string }
  | { key: ScalarKey; kind: 'time'; hint?: string };

function specError(f: FieldSpec, v: string): boolean {
  const s = v.trim();
  if (f.kind === 'int') {
    if (!/^\d+$/.test(s)) return true;
    const n = Number(s);
    return n < f.min || n > f.max;
  }
  if (f.kind === 'money') return s !== '' && !(Number(s) >= 0 && Number.isFinite(Number(s)));
  if (f.kind === 'time') return !/^([01]\d|2[0-3]):[0-5]\d$/.test(s);
  return false;
}

/** A small form of 3b scalar settings saved with one PUT /api/settings (partial merge). */
function ScalarForm({
  settings,
  fields,
  testId,
  extra,
  extraBody,
}: {
  settings: Settings;
  fields: FieldSpec[];
  testId?: string;
  extra?: ReactNode;
  extraBody?: () => Record<string, unknown> | null;
}) {
  const { t } = useTranslation();
  const raw = settings as unknown as Record<string, unknown>;
  const init = () =>
    Object.fromEntries(
      fields.map((f) => [
        f.key,
        f.kind === 'bool'
          ? !!raw[f.key]
          : raw[f.key] === null || raw[f.key] === undefined
            ? ''
            : String(raw[f.key]),
      ]),
    ) as Record<string, string | boolean>;
  const [v, setV] = useState(init);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => setV(init()), [settings]);
  const save = useSaveSettings();
  const bad = fields.filter((f) => f.kind !== 'bool' && specError(f, String(v[f.key] ?? '')));
  const extraB = extraBody?.();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (bad.length || extraB === null) return;
    const body: Record<string, unknown> = { ...(extraB ?? {}) };
    for (const f of fields) {
      const x = v[f.key];
      if (f.kind === 'bool') body[f.key] = !!x;
      else if (f.kind === 'money') body[f.key] = String(x).trim() === '' ? null : Number(x);
      else if (f.kind === 'int') body[f.key] = Number(x);
      else body[f.key] = String(x).trim();
    }
    save.mutate(body);
  };
  return (
    <form onSubmit={submit} className="space-y-3" data-testid={testId}>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {fields
          .filter((f) => f.kind !== 'bool')
          .map((f) => {
            const err = bad.includes(f);
            return (
              <Field
                key={f.key}
                label={t(`assistant.key.${f.key}`)}
                hint={
                  err ? (
                    <span className="text-status-down">
                      {f.kind === 'int'
                        ? t('assistant.settings.err.range', { min: f.min, max: f.max })
                        : f.kind === 'time'
                          ? t('assistant.settings.err.time')
                          : t('assistant.settings.err.money')}
                    </span>
                  ) : (
                    f.hint
                  )
                }
              >
                <Input
                  type={f.kind === 'time' ? 'time' : 'number'}
                  min={f.kind === 'int' ? f.min : f.kind === 'money' ? 0 : undefined}
                  max={f.kind === 'int' ? f.max : undefined}
                  step={f.kind === 'money' ? 'any' : undefined}
                  value={String(v[f.key] ?? '')}
                  onChange={(e) => setV((x) => ({ ...x, [f.key]: e.target.value }))}
                  dir="ltr"
                  aria-invalid={err}
                  name={f.key}
                />
              </Field>
            );
          })}
      </div>
      {fields
        .filter((f) => f.kind === 'bool')
        .map((f) => (
          <div key={f.key}>
            <Checkbox
              label={t(`assistant.key.${f.key}`)}
              checked={!!v[f.key]}
              onChange={(c) => setV((x) => ({ ...x, [f.key]: c }))}
            />
            {f.hint && <p className="ms-6 text-xs text-ink-muted">{f.hint}</p>}
          </div>
        ))}
      {extra}
      <ErrorBox error={save.error} />
      <div className="flex justify-end">
        <Button type="submit" busy={save.isPending} disabled={bad.length > 0 || extraB === null}>
          {t('common.save')}
        </Button>
      </div>
    </form>
  );
}

// ---------------------------------------------------------------------------
// Data sharing (PUT /api/assistant/settings, non-connection fields: no test needed)

function DataSharingCard({ s, settings }: { s: AssistantSettings; settings: Settings }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const [excerpts, setExcerpts] = useState(s.send_config_excerpts);
  const [redact, setRedact] = useState(s.redact_internal_ips);
  const [cidrs, setCidrs] = useState(s.internal_cidrs.join('\n'));
  const [chatRedact, setChatRedact] = useState(s.chat_redact_ips);
  useEffect(() => {
    setExcerpts(s.send_config_excerpts);
    setRedact(s.redact_internal_ips);
    setCidrs(s.internal_cidrs.join('\n'));
    setChatRedact(s.chat_redact_ips);
  }, [s]);
  const parsed = parseCidrs(cidrs);
  const tooMany = parsed.items.length > 64;
  const save = useMutation({
    mutationFn: () =>
      endpoints3b.updateSettings({
        send_config_excerpts: excerpts,
        redact_internal_ips: redact,
        internal_cidrs: parsed.items,
        chat_redact_ips: chatRedact,
      }),
    onSuccess: (r) => {
      qc.setQueryData(qk3b.settings, r);
      void qc.invalidateQueries({ queryKey: qk3b.status });
      toast.push('success', t('common.saved'));
    },
  });
  const maskOff = settings.config_backup_mask_secrets === false;
  return (
    <Card title={t('assistant.settings.dataSharing')} anchor={cardAnchor('assistantData')}>
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (!parsed.bad.length && !tooMany) save.mutate();
        }}
        data-testid="assistant-privacy"
      >
        <div>
          <Checkbox
            label={t('assistant.settings.sendConfig')}
            checked={excerpts}
            onChange={setExcerpts}
          />
          <p className="ms-6 text-xs text-ink-muted">{t('assistant.settings.sendConfigHint')}</p>
          {excerpts && maskOff && (
            <p className="ms-6 text-xs text-status-warning">{t('assistant.settings.maskOff')}</p>
          )}
        </div>
        <div>
          <Checkbox
            label={t('assistant.settings.redactIps')}
            checked={redact}
            onChange={setRedact}
          />
          <p className="ms-6 text-xs text-ink-muted">{t('assistant.settings.redactTradeoff')}</p>
        </div>
        {redact && (
          <Field
            label={t('assistant.settings.internalCidrs')}
            className="ms-6"
            hint={
              parsed.bad.length ? (
                <span className="text-status-down">
                  {t('assistant.settings.badCidrs', { list: parsed.bad.join(', ') })}
                </span>
              ) : tooMany ? (
                <span className="text-status-down">{t('assistant.settings.tooManyCidrs')}</span>
              ) : (
                t('assistant.settings.internalCidrsHint')
              )
            }
          >
            <Textarea
              rows={3}
              value={cidrs}
              onChange={(e) => setCidrs(e.target.value)}
              dir="ltr"
              className="font-mono text-xs"
            />
          </Field>
        )}
        <div>
          <Checkbox
            label={t('assistant.settings.chatRedact')}
            checked={chatRedact}
            onChange={setChatRedact}
          />
          <p className="ms-6 text-xs text-ink-muted">{t('assistant.settings.chatRedactHint')}</p>
        </div>
        <ErrorBox error={save.error} />
        <div className="flex justify-end">
          <Button type="submit" busy={save.isPending} disabled={parsed.bad.length > 0 || tooMany}>
            {t('common.save')}
          </Button>
        </div>
        <div
          className="space-y-1 rounded-md bg-surface-sunken p-3 text-xs text-ink-muted"
          data-testid="data-handling"
        >
          <p className="font-semibold">{t('assistant.settings.dataHandling')}</p>
          <p>{t('assistant.settings.dataHandlingText')}</p>
          <p className="flex flex-wrap gap-x-3">
            <a
              className="text-accent-ink underline"
              target="_blank"
              rel="noopener noreferrer"
              href="https://docs.aws.amazon.com/bedrock/latest/userguide/data-protection.html"
            >
              {t('assistant.settings.linkDataProtection')}
            </a>
            <a
              className="text-accent-ink underline"
              target="_blank"
              rel="noopener noreferrer"
              href="https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html"
            >
              {t('assistant.settings.linkInvocationLogging')}
            </a>
            <a
              className="text-accent-ink underline"
              target="_blank"
              rel="noopener noreferrer"
              href="https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html"
            >
              {t('assistant.settings.linkCrossRegion')}
            </a>
          </p>
          <p className="text-ink-muted">{t('assistant.settings.auditTitlesOnly')}</p>
        </div>
      </form>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Limits & cost (scalar keys + prices)

function PricesForm({ s }: { s: AssistantSettings }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const p = s.prices;
  const str = (n: number | null) => (n === null ? '' : String(n));
  const [v, setV] = useState({
    currency: p.currency || 'USD',
    input: str(p.input_per_mtok),
    output: str(p.output_per_mtok),
    cacheRead: str(p.cache_read_per_mtok),
    cacheWrite: str(p.cache_write_per_mtok),
  });
  useEffect(() => {
    setV({
      currency: p.currency || 'USD',
      input: str(p.input_per_mtok),
      output: str(p.output_per_mtok),
      cacheRead: str(p.cache_read_per_mtok),
      cacheWrite: str(p.cache_write_per_mtok),
    });
  }, [p]);
  const num = (x: string) => (x.trim() === '' ? null : Number(x));
  const bad = (x: string) => x.trim() !== '' && !(Number(x) >= 0 && Number(x) <= 1000);
  const invalid =
    [v.input, v.output, v.cacheRead, v.cacheWrite].some(bad) || !/^[A-Za-z]{3}$/.test(v.currency);
  const save = useMutation({
    mutationFn: () => {
      const prices: AssistantPrices = {
        currency: v.currency.toUpperCase(),
        input_per_mtok: num(v.input),
        output_per_mtok: num(v.output),
        cache_read_per_mtok: num(v.cacheRead),
        cache_write_per_mtok: num(v.cacheWrite),
      };
      return endpoints3b.updateSettings({ prices });
    },
    onSuccess: (r) => {
      qc.setQueryData(qk3b.settings, r);
      toast.push('success', t('common.saved'));
    },
  });
  const field = (k: 'input' | 'output' | 'cacheRead' | 'cacheWrite') => (
    <Field
      key={k}
      label={t(`assistant.prices.${k}`, { currency: v.currency })}
      hint={
        bad(v[k]) ? (
          <span className="text-status-down">{t('assistant.settings.err.money')}</span>
        ) : undefined
      }
    >
      <Input
        type="number"
        min={0}
        max={1000}
        step="any"
        value={v[k]}
        onChange={(e) => setV((x) => ({ ...x, [k]: e.target.value }))}
        dir="ltr"
      />
    </Field>
  );
  return (
    <form
      className="space-y-3 border-t border-line pt-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (!invalid) save.mutate();
      }}
      data-testid="assistant-prices"
    >
      <p className="text-sm font-medium">{t('assistant.prices.title')}</p>
      <p className="text-xs text-ink-muted">{t('assistant.prices.hint')}</p>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <Field label={t('assistant.prices.currency')}>
          <Input
            value={v.currency}
            maxLength={3}
            onChange={(e) => setV((x) => ({ ...x, currency: e.target.value }))}
            dir="ltr"
          />
        </Field>
        {field('input')}
        {field('output')}
        {field('cacheRead')}
        {field('cacheWrite')}
      </div>
      <ErrorBox error={save.error} />
      <div className="flex justify-end">
        <Button type="submit" busy={save.isPending} disabled={invalid}>
          {t('common.save')}
        </Button>
      </div>
    </form>
  );
}

function UsageStrip() {
  const { t } = useTranslation();
  const st = useAssistantStatus();
  const m = st.data?.month;
  if (!m) return null;
  const tone = pctTone(m.pct_of_cap);
  return (
    <div
      className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-md bg-surface-sunken px-3 py-2 text-xs"
      data-testid="usage-strip"
    >
      <span>
        {t('usage.thisMonth')}: {compactTokens(m.tokens)} / {compactTokens(m.token_cap)}{' '}
        <span
          className={cx(
            tone === 'error' && 'text-status-down',
            tone === 'warn' && 'text-status-warning',
          )}
        >
          ({Math.round(m.pct_of_cap)} %)
        </span>
      </span>
      {m.cost_estimate !== null && (
        <span>
          ≈ {formatCost(m.cost_estimate, m.currency)}
          {m.cost_cap !== null && ` / ${formatCost(m.cost_cap, m.currency)}`}
        </span>
      )}
      <span>{t('usage.projection', { pct: Math.round(m.projection_pct) })}</span>
      {m.background_stopped && (
        <span className="text-status-warning">{t('usage.backgroundStopped')}</span>
      )}
      <Link to="/assistant/usage" className="ms-auto text-accent-ink hover:underline">
        {t('usage.open')}
      </Link>
    </div>
  );
}

function LimitsCard({ s, settings }: { s: AssistantSettings; settings: Settings }) {
  const { t } = useTranslation();
  return (
    <Card title={t('assistant.settings.limits')} anchor={cardAnchor('assistantLimits')}>
      <div className="space-y-4">
        <UsageStrip />
        <ScalarForm
          settings={settings}
          testId="assistant-limits"
          fields={[
            { key: 'assistant_daily_questions_per_user', kind: 'int', min: 1, max: 1000 },
            {
              key: 'assistant_monthly_token_cap',
              kind: 'int',
              min: 100000,
              max: 1000000000,
              hint: t('assistant.hint.tokenCap'),
            },
            { key: 'assistant_monthly_cost_cap', kind: 'money', hint: t('assistant.hint.costCap') },
            {
              key: 'assistant_background_stop_pct',
              kind: 'int',
              min: 10,
              max: 100,
              hint: t('assistant.hint.backgroundStop'),
            },
            { key: 'assistant_max_model_calls', kind: 'int', min: 2, max: 15 },
            { key: 'assistant_max_tool_calls', kind: 'int', min: 1, max: 30 },
            { key: 'assistant_max_output_tokens', kind: 'int', min: 256, max: 8192 },
            { key: 'assistant_question_timeout_s', kind: 'int', min: 30, max: 300 },
            { key: 'assistant_max_concurrent', kind: 'int', min: 1, max: 16 },
            { key: 'assistant_context_budget_tokens', kind: 'int', min: 8000, max: 150000 },
          ]}
        />
        {!s.prices.input_per_mtok && (
          <p className="text-xs text-status-warning">{t('assistant.hint.noPrices')}</p>
        )}
        <PricesForm s={s} />
      </div>
    </Card>
  );
}

function ConversationsCard({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  return (
    <Card
      title={t('assistant.settings.conversations')}
      anchor={cardAnchor('assistantConversations')}
    >
      <ScalarForm
        settings={settings}
        testId="assistant-retention"
        fields={[
          { key: 'assistant_retention_days', kind: 'int', min: 1, max: 730 },
          { key: 'assistant_artifact_retention_days', kind: 'int', min: 7, max: 730 },
        ]}
        extra={<p className="text-xs text-ink-muted">{t('assistant.settings.auditTitlesOnly')}</p>}
      />
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Capabilities: one switch per AI feature (§ 14.0), settings shown while the feature is ON

function DigestForm({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const channels = useQuery({ queryKey: qk.channels, queryFn: endpoints.channels });
  const [lang, setLang] = useState<string>(settings.digest_language ?? '');
  const [emails, setEmails] = useState((settings.digest_email_to ?? []).join('\n'));
  const [ids, setIds] = useState<number[]>(settings.digest_channel_ids ?? []);
  useEffect(() => {
    setLang(settings.digest_language ?? '');
    setEmails((settings.digest_email_to ?? []).join('\n'));
    setIds(settings.digest_channel_ids ?? []);
  }, [settings]);
  const list = emails
    .split(/[\s,;]+/)
    .map((x) => x.trim())
    .filter(Boolean);
  const bad = list.filter((x) => !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(x));
  const invalid = bad.length > 0 || list.length > 20 || ids.length > 10;
  return (
    <ScalarForm
      settings={settings}
      testId="digest-settings"
      fields={[{ key: 'digest_time', kind: 'time' }]}
      extraBody={() =>
        invalid
          ? null
          : { digest_language: lang || null, digest_email_to: list, digest_channel_ids: ids }
      }
      extra={
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label={t('assistant.key.digest_language')}>
            <Select value={lang} onChange={(e) => setLang(e.target.value)}>
              <option value="">{t('assistant.settings.defaultLanguage')}</option>
              <option value="en">English</option>
              <option value="he">עברית</option>
            </Select>
          </Field>
          <Field
            label={t('assistant.key.digest_email_to')}
            hint={
              bad.length ? (
                <span className="text-status-down">
                  {t('assistant.settings.badEmails', { list: bad.join(', ') })}
                </span>
              ) : (
                t('assistant.settings.emailsHint')
              )
            }
          >
            <Textarea
              rows={2}
              value={emails}
              onChange={(e) => setEmails(e.target.value)}
              dir="ltr"
            />
          </Field>
          <FieldGroup label={t('assistant.key.digest_channel_ids')} className="sm:col-span-2">
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              {(channels.data?.items ?? [])
                .filter((c) => c.type !== 'webhook')
                .map((c) => (
                  <Checkbox
                    key={c.id}
                    label={`${c.name} (${t(`channels.${c.type}`)})`}
                    checked={ids.includes(c.id)}
                    onChange={(v) =>
                      setIds((x) => (v ? [...x, c.id] : x.filter((y) => y !== c.id)))
                    }
                  />
                ))}
              {channels.data && !channels.data.items.length && (
                <span className="text-xs text-ink-muted">{t('rules.noChannels')}</span>
              )}
            </div>
          </FieldGroup>
        </div>
      }
    />
  );
}

function ChatAskExtra() {
  const { t } = useTranslation();
  return (
    <div className="space-y-1 text-xs text-ink-muted" data-testid="chat-ask-info">
      <p>{t('assistant.chatAsk.howTo')}</p>
      <p>{t('assistant.chatAsk.linking')}</p>
      <p className="text-ink-muted">{t('assistant.chatAsk.teams')}</p>
      <p>
        <Link to="/account" className="text-accent-ink hover:underline">
          {t('assistant.chatAsk.accountLink')}
        </Link>
        {' · '}
        <Link to="/notifications" className="text-accent-ink hover:underline">
          {t('assistant.chatAsk.channelsLink')}
        </Link>
      </p>
    </div>
  );
}

/** [1.3.1] Settings-search anchors of the per-feature settings (src/lib/settingsIndex.ts). */
const FEATURE_SETTINGS_ANCHOR: Partial<Record<AiFeatureName, string>> = {
  ai_correlation: cardAnchor('aiCorrelation'),
  ai_alert_notes: cardAnchor('aiAlertNotes'),
  ai_config_diff: cardAnchor('aiConfigDiff'),
  ai_syslog_digest: cardAnchor('aiSyslogDigest'),
  ai_chat_ask: cardAnchor('aiChatAsk'),
};

function FeatureSettings({ name, settings }: { name: AiFeatureName; settings: Settings }) {
  const { t } = useTranslation();
  switch (name) {
    case 'ai_correlation':
      return (
        <ScalarForm
          settings={settings}
          fields={[
            { key: 'correlation_window_min', kind: 'int', min: 1, max: 30 },
            { key: 'correlation_min_alerts', kind: 'int', min: 2, max: 50 },
            { key: 'correlation_daily_summaries', kind: 'int', min: 0, max: 500 },
            { key: 'correlation_attach_to_alert', kind: 'bool', hint: t('assistant.hint.attach') },
          ]}
        />
      );
    case 'ai_alert_notes':
      return (
        <ScalarForm
          settings={settings}
          fields={[
            { key: 'alert_note_timeout_s', kind: 'int', min: 10, max: 120 },
            { key: 'alert_notes_daily_cap', kind: 'int', min: 0, max: 5000 },
          ]}
          extra={<p className="text-xs text-ink-muted">{t('assistant.hint.notesWhere')}</p>}
        />
      );
    case 'ai_config_diff':
      return (
        <ScalarForm
          settings={settings}
          fields={[
            { key: 'config_diff_daily_cap', kind: 'int', min: 0, max: 500 },
            { key: 'config_diff_auto', kind: 'bool', hint: t('assistant.hint.diffAuto') },
          ]}
        />
      );
    case 'ai_syslog_digest':
      return (
        <div className="space-y-2">
          <DigestForm settings={settings} />
          <Link to="/assistant/digests" className="text-xs text-accent-ink hover:underline">
            {t('digest.open')}
          </Link>
        </div>
      );
    case 'ai_chat_ask':
      return (
        <ScalarForm
          settings={settings}
          fields={[{ key: 'chat_ask_per_user_hour', kind: 'int', min: 1, max: 60 }]}
          extra={<ChatAskExtra />}
        />
      );
    case 'ai_report_summary':
      return <p className="text-xs text-ink-muted">{t('assistant.hint.reportOptIn')}</p>;
    case 'ai_rule_drafts':
      return <p className="text-xs text-ink-muted">{t('assistant.hint.ruleDraftsWhere')}</p>;
    case 'ai_template_drafts':
      return <p className="text-xs text-ink-muted">{t('assistant.hint.templateDraftsWhere')}</p>;
    case 'ai_explain_alert':
      return <p className="text-xs text-ink-muted">{t('assistant.hint.explainWhere')}</p>;
    default:
      return null;
  }
}

function CapabilityRow({
  name,
  settings,
  ft,
}: {
  name: AiFeatureName;
  settings: Settings;
  ft: ReturnType<typeof useFeatureToggle>;
}) {
  const { t } = useTranslation();
  const f = featuresFromSettings(settings);
  const st = useAssistantStatus();
  const info = st.data?.features.find((x) => x.name === name);
  const on = f[name];
  const hints = featureHints(name, f, settings);
  const kb = name === 'ai_knowledge';
  return (
    <li className="space-y-2 px-4 py-3" data-feature={name}>
      <div className="flex items-start gap-3">
        <div className="pt-0.5">
          {kb ? (
            <span
              className={cx(
                'inline-block h-2 w-2 rounded-full',
                on ? 'bg-status-up' : 'bg-status-paused',
              )}
              aria-hidden
            />
          ) : (
            <Toggle
              checked={on}
              onChange={(v) => ft.toggle(name, v)}
              label={t(`features.name.${name}`)}
              busy={ft.pending === name}
            />
          )}
        </div>
        <div className="min-w-0 flex-1 space-y-0.5">
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="font-medium">{t(`features.name.${name}`)}</span>
            <span className={cx('text-xs', on ? 'text-status-up' : 'text-ink-muted')}>
              {on ? t('features.on') : t('features.off')}
            </span>
          </div>
          <p className="text-sm text-ink-muted">{t(`features.desc.${name}`)}</p>
          {on && info?.idle_reason && (
            <p className="text-xs text-status-warning" data-testid="idle-reason">
              {t('assistant.idle', {
                reason: info.idle_reason
                  .split(',')
                  .map((n) =>
                    t(`features.name.${n.trim()}`, {
                      defaultValue: t(`assistant.idleNeed.${n.trim()}`, { defaultValue: n }),
                    }),
                  )
                  .join(', '),
              })}
            </p>
          )}
          {on && info?.status_line && <p className="text-xs text-status-up">{info.status_line}</p>}
          {hints.map((h) => (
            <p
              key={h.key}
              className={cx(
                'text-xs',
                h.tone === 'warn' ? 'text-status-warning' : 'text-ink-muted',
              )}
            >
              {t(h.key)}
            </p>
          ))}
          {kb && (
            <a href="#assistant-kb" className="text-xs text-accent-ink hover:underline">
              {t('assistant.kb.configureBelow')}
            </a>
          )}
          {!!ft.errors[name] && <ErrorBox error={ft.errors[name]} className="mt-1" />}
        </div>
      </div>
      {on && !kb && (
        <div className="ms-12" data-anchor={FEATURE_SETTINGS_ANCHOR[name]}>
          <FeatureSettings name={name} settings={settings} />
        </div>
      )}
    </li>
  );
}

function CapabilitiesCard({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const ft = useFeatureToggle();
  return (
    <Card
      title={t('assistant.settings.capabilities')}
      bodyClassName="p-0"
      anchor={cardAnchor('assistantCapabilities')}
    >
      <p className="border-b border-line px-4 py-2 text-xs text-ink-muted">
        {t('assistant.settings.capabilitiesIntro')}
      </p>
      <ul className="divide-y divide-line" data-testid="capabilities">
        {AI_FEATURES.map((n) => (
          <CapabilityRow key={n} name={n} settings={settings} ft={ft} />
        ))}
      </ul>
      {ft.dialogs}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Knowledge Base (configure-first, its own Test + bottom switch)

let kbSessionTest: { key: string; id: string; expires: number } | null = null;

function KnowledgeCard({ assistantKey }: { assistantKey: string }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const errText = useAssistantErrorText();
  const q = useQuery({ queryKey: qk3b.knowledge, queryFn: endpoints3b.knowledge });
  const [f, setF] = useState({
    id: '',
    region: '',
    endpoint: '',
    results: '5',
    minScore: '0',
    description: '',
  });
  const [result, setResult] = useState<KnowledgeTestResult | null>(null);
  const [, force] = useState(0);
  useEffect(() => {
    if (!q.data) return;
    setF({
      id: q.data.knowledge_base_id ?? '',
      region: q.data.region ?? '',
      endpoint: q.data.endpoint_url ?? '',
      results: String(q.data.results ?? 5),
      minScore: String(q.data.min_score ?? 0),
      description: q.data.description ?? '',
    });
  }, [q.data]);
  const input = (): KnowledgeInput => ({
    knowledge_base_id: f.id.trim(),
    region: f.region.trim() || null,
    endpoint_url: f.endpoint.trim() || null,
    results: Number(f.results),
    min_score: Number(f.minScore),
    description: f.description.trim(),
  });
  const errs = {
    id: !/^[0-9A-Z]{10}$/.test(f.id.trim()),
    region: !!f.region.trim() && !/^[a-z]{2}(-gov)?-[a-z]+-\d$/.test(f.region.trim()),
    endpoint: !!f.endpoint.trim() && !/^https:\/\/[^/\s]+\/?$/.test(f.endpoint.trim()),
    results: !/^\d+$/.test(f.results) || Number(f.results) < 1 || Number(f.results) > 10,
    minScore: !(Number(f.minScore) >= 0 && Number(f.minScore) <= 1) || f.minScore.trim() === '',
    description: f.description.length > 300,
  };
  const invalid = Object.values(errs).some(Boolean);
  const { knowledge_base_id, region, endpoint_url, results, min_score } = input();
  const key = JSON.stringify([
    knowledge_base_id,
    region,
    endpoint_url,
    results,
    min_score,
    assistantKey,
  ]);
  const testId =
    kbSessionTest && kbSessionTest.key === key && Date.now() < kbSessionTest.expires
      ? kbSessionTest.id
      : null;
  const onSaved = () => {
    void qc.invalidateQueries({ queryKey: qk3b.knowledge });
    void qc.invalidateQueries({ queryKey: qk.publicSettings });
    void qc.invalidateQueries({ queryKey: qk.settings });
  };
  const save = useMutation({
    mutationFn: () =>
      endpoints3b.updateKnowledge({ ...input(), ...(testId ? { test_id: testId } : {}) }),
    onSuccess: () => {
      onSaved();
      toast.push('success', t('common.saved'));
    },
  });
  const test = useMutation({
    mutationFn: () => endpoints3b.testKnowledge(input()),
    onMutate: () => setResult(null),
    onSuccess: (r) => {
      setResult(r);
      if (r.ok && r.test_id) {
        kbSessionTest = {
          key,
          id: r.test_id,
          expires: r.expires_at ? Date.parse(r.expires_at) : Date.now() + 30 * 60_000,
        };
        force((n) => n + 1);
      }
    },
  });
  const toggle = useMutation({
    mutationFn: (on: boolean) =>
      endpoints3b.updateKnowledge(
        on ? { ...input(), enabled: true, test_id: testId ?? undefined } : { enabled: false },
      ),
    onSuccess: onSaved,
  });
  const enabled = !!q.data?.enabled;
  const set = (k: keyof typeof f, v: string) => setF((x) => ({ ...x, [k]: v }));
  const err = (bad: boolean, hint?: string) =>
    bad ? <span className="text-status-down">{t('assistant.kb.invalid')}</span> : hint;
  return (
    <Card title={t('assistant.kb.title')} anchor={cardAnchor('assistantKb')}>
      <div id="assistant-kb" className="space-y-3" data-testid="assistant-kb">
        <p className="text-xs text-ink-muted">{t('assistant.kb.intro')}</p>
        {q.isLoading && <Loading />}
        <ErrorBox error={q.error} />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field
            label={t('assistant.kb.id')}
            required
            hint={err(errs.id && !!f.id, t('assistant.kb.idHint'))}
          >
            <Input
              value={f.id}
              onChange={(e) => set('id', e.target.value.toUpperCase())}
              dir="ltr"
              maxLength={10}
            />
          </Field>
          <Field
            label={t('assistant.kb.region')}
            hint={err(errs.region, t('assistant.kb.regionHint'))}
          >
            <Input value={f.region} onChange={(e) => set('region', e.target.value)} dir="ltr" />
          </Field>
          <Field
            label={t('assistant.kb.endpoint')}
            hint={err(errs.endpoint, t('assistant.kb.endpointHint'))}
          >
            <Input
              value={f.endpoint}
              onChange={(e) => set('endpoint', e.target.value)}
              dir="ltr"
              placeholder="https://bedrock-agent-runtime.us-east-1.amazonaws.com"
            />
          </Field>
          <Field label={t('assistant.kb.results')} hint={err(errs.results, '1–10')}>
            <Input
              type="number"
              min={1}
              max={10}
              value={f.results}
              onChange={(e) => set('results', e.target.value)}
              dir="ltr"
            />
          </Field>
          <Field label={t('assistant.kb.minScore')} hint={err(errs.minScore, '0–1')}>
            <Input
              type="number"
              min={0}
              max={1}
              step="0.05"
              value={f.minScore}
              onChange={(e) => set('minScore', e.target.value)}
              dir="ltr"
            />
          </Field>
          <Field
            label={t('assistant.kb.description')}
            className="sm:col-span-3"
            hint={err(errs.description, t('assistant.kb.descriptionHint'))}
          >
            <Input
              value={f.description}
              onChange={(e) => set('description', e.target.value)}
              maxLength={300}
            />
          </Field>
        </div>
        <ErrorBox error={save.error} />
        <div className="flex justify-end">
          <Button onClick={() => save.mutate()} busy={save.isPending} disabled={invalid}>
            {t('common.save')}
          </Button>
        </div>
        <div className="space-y-2 border-t border-line pt-3">
          <Button
            onClick={() => test.mutate()}
            busy={test.isPending}
            disabled={invalid}
            data-testid="kb-test"
          >
            {t('assistant.kb.test')}
          </Button>
          <ErrorBox error={test.error} />
          {result && (
            <div className="text-xs" data-testid="kb-test-result" data-ok={result.ok}>
              {result.ok ? (
                <p className="flex items-center gap-1 text-status-up">
                  <CheckIcon size={13} />
                  {t('assistant.kb.testOk', { n: result.results, ms: result.ms })}
                  {result.first_source && (
                    <bdi dir="auto" className="text-ink-muted">
                      {' '}
                      · {result.first_source.title}
                    </bdi>
                  )}
                </p>
              ) : (
                <p className="flex items-center gap-1 text-status-down">
                  <XIcon size={13} />
                  {errText(result.error ?? { code: 'kb_unavailable', message: '' })}
                </p>
              )}
            </div>
          )}
          {q.data?.last_test && (
            <p className="text-xs text-ink-muted">
              {t('assistant.settings.lastTest', {
                result: q.data.last_test.ok
                  ? t('assistant.test.okShort')
                  : (q.data.last_test.error_code ?? ''),
                time: formatDateTime(q.data.last_test.at),
                by: q.data.last_test.by ?? '',
              })}
            </p>
          )}
          <div className="flex flex-wrap items-center gap-2" data-feature="ai_knowledge">
            <Toggle
              checked={enabled}
              onChange={(v) => toggle.mutate(v)}
              label={t('features.name.ai_knowledge')}
              busy={toggle.isPending}
              disabled={!enabled && (!testId || invalid)}
            />
            <span className="text-sm font-medium">{t('features.name.ai_knowledge')}</span>
            <span className={cx('text-xs', enabled ? 'text-status-up' : 'text-ink-muted')}>
              {enabled ? t('features.on') : t('features.off')}
            </span>
          </div>
          {!enabled && !testId && (
            <p className="text-xs text-status-warning" data-testid="kb-enable-hint">
              {t('assistant.settings.enableNeedsTest')}
            </p>
          )}
          <ErrorBox error={toggle.error} />
        </div>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------

/** [3b] Settings → Assistant: connection (configure-first), data sharing, limits, capabilities, KB. */
export function AssistantSettingsTab({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const q = useQuery({ queryKey: qk3b.settings, queryFn: endpoints3b.settings });
  const f = featuresFromSettings(settings);
  if (q.isLoading) return <Loading />;
  if (q.error || !q.data) return <ErrorBox error={q.error} />;
  const s = q.data;
  const sections = [
    'connection',
    'chatModels',
    'dataSharing',
    'limits',
    'conversations',
    'capabilities',
    'kb',
  ] as const;
  return (
    <div className="space-y-6" data-testid="assistant-settings">
      <nav
        className="flex flex-wrap gap-x-4 gap-y-1 text-xs"
        aria-label={t('settings.tab.assistant')}
      >
        {sections.map((k) => (
          <a key={k} href={`#as-${k}`} className="text-accent-ink hover:underline">
            {t(`assistant.settings.section.${k}`)}
          </a>
        ))}
        {f.assistant && (
          <Link to="/assistant" className="ms-auto font-medium text-accent-ink hover:underline">
            {t('assistant.open')}
          </Link>
        )}
      </nav>
      <div id="as-connection">
        <ConnectionSettings s={s} auditOn={f.audit} />
      </div>
      <div id="as-chatModels">
        <ChatModelsCard s={s} />
      </div>
      <div id="as-dataSharing">
        <DataSharingCard s={s} settings={settings} />
      </div>
      <div id="as-limits">
        <LimitsCard s={s} settings={settings} />
      </div>
      <div id="as-conversations">
        <ConversationsCard settings={settings} />
      </div>
      <div id="as-capabilities">
        <CapabilitiesCard settings={settings} />
      </div>
      <div id="as-kb">
        <KnowledgeCard
          assistantKey={`${s.auth_mode}|${s.region}|${s.model_id}|${s.endpoint_url ?? ''}`}
        />
      </div>
    </div>
  );
}
