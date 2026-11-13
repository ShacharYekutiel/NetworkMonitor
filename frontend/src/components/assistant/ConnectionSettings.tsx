import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { qk } from '../../api/endpoints';
import { endpoints3b, qk3b } from '../../api/endpoints3b';
import { endpoints4, qk4 } from '../../api/endpoints4';
import type { AssistantSettings, AssistantTestResult, ModelListItem } from '../../api/types';
import {
  connErrors,
  connFormFrom,
  connInput,
  connKey,
  connMissing,
  formAccess,
  profileScope,
  SUGGESTED_REGIONS,
  type ConnForm,
} from '../../lib/assistant';
import { accessErrors, certVersion } from '../../lib/aws';
import { formatDateTime } from '../../lib/format';
import { cardAnchor } from '../../lib/settingsIndex';
import { AwsAccessBlock } from '../aws/AwsAccessBlock';
import { AwsTestSteps, TestWarnings } from '../aws/AwsTestSteps';
import { Toggle } from '../features/common';
import { useToast } from '../toast';
import { Button, Card, Checkbox, cx, ErrorBox, Field, Input } from '../ui';
import { AssistantStateBadge, useAssistantErrorText } from './common';

/** A successful test in this page session (module scope: survives switching settings tabs). */
let sessionTest: { key: string; id: string; expires: number } | null = null;
export function sessionTestFor(key: string): string | null {
  if (!sessionTest || sessionTest.key !== key || Date.now() > sessionTest.expires) return null;
  return sessionTest.id;
}

const ERR_KEYS: Record<string, string> = {
  region: 'assistant.settings.err.region',
  model: 'assistant.settings.err.model',
  url: 'assistant.settings.err.url',
  arn: 'aws.err.arn',
  arnTwice: 'aws.err.arnTwice',
  required: 'aws.err.required',
  externalId: 'aws.err.externalId',
  raOnly: 'aws.err.raOnly',
  profileAccount: 'aws.err.profileAccount',
};

function Hint({ err, hint }: { err?: string; hint?: string }) {
  const { t } = useTranslation();
  if (err) return <span className="text-red-600">{t(ERR_KEYS[err] ?? err)}</span>;
  return hint ? <>{hint}</> : null;
}

/** Data-location explanation per inference-profile prefix; `global.` gets a warning. */
function GeoNote({ modelId, region }: { modelId: string; region: string }) {
  const { t } = useTranslation();
  const scope = profileScope(modelId);
  if (scope === 'none') return null;
  const warn = scope === 'global';
  return (
    <div
      className={cx(
        'rounded-md px-3 py-2 text-xs ring-1 ring-inset',
        warn
          ? 'bg-amber-50 text-amber-900 ring-amber-200 dark:bg-amber-950/40 dark:text-amber-200 dark:ring-amber-900'
          : 'bg-slate-50 text-slate-700 ring-slate-200 dark:bg-slate-800/60 dark:text-slate-300 dark:ring-slate-700',
      )}
      data-testid="geo-note"
      data-scope={scope}
    >
      {t(`assistant.settings.geo.${scope}`, { region })}
      {region === 'il-central-1' && (
        <span className="mt-1 block">{t('assistant.settings.geo.ilCentral')}</span>
      )}
    </div>
  );
}

function TestResultView({ r }: { r: AssistantTestResult }) {
  const { t } = useTranslation();
  const errText = useAssistantErrorText();
  return (
    <div className="space-y-1 text-xs" data-testid="test-result" data-ok={r.ok}>
      <AwsTestSteps steps={r.steps} />
      <p className="text-slate-500">
        {r.endpoint_host && (
          <>
            {t('assistant.test.endpoint')}: <bdi dir="ltr">{r.endpoint_host}</bdi>
            {r.address_class && ` (${t(`assistant.test.class.${r.address_class}`)})`} ·{' '}
          </>
        )}
        {r.via && (
          <>
            {t('assistant.test.via')}: <bdi dir="ltr">{r.via}</bdi>
          </>
        )}
        {r.time_to_first_event_ms !== null &&
          ` · ${t('assistant.test.ttfe', { ms: r.time_to_first_event_ms })}`}
        {r.usage &&
          ` · ${t('assistant.tokens', { n: r.usage.input_tokens + r.usage.output_tokens })}`}
      </p>
      <TestWarnings warnings={r.warnings} />
      {r.error && !r.steps.some((s) => s.error_code) && (
        <p className="text-red-600">{errText(r.error)}</p>
      )}
      {r.ok && (
        <p className="font-medium text-green-700 dark:text-green-400">{t('assistant.test.ok')}</p>
      )}
    </div>
  );
}

/**
 * [3b] Settings → Assistant → Connection (configure-first, § 14.2.6): the form is always shown;
 * Test connection + the enable switch at the bottom; the switch is locked until a test of the
 * current form values succeeded in this page session (any connection edit locks it again).
 * [4] The authentication moved to Settings → AWS: this form picks the AWS connection and how
 * Bedrock is reached (*AWS access*, architecture § 15.1.10).
 */
export function ConnectionSettings({ s, auditOn }: { s: AssistantSettings; auditOn: boolean }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const errText = useAssistantErrorText();
  const conns = useQuery({ queryKey: qk4.connections, queryFn: endpoints4.connections });
  const [f, setF] = useState<ConnForm>(() => connFormFrom(s));
  const [models, setModels] = useState<ModelListItem[] | null>(null);
  const [testResult, setTestResult] = useState<AssistantTestResult | null>(null);
  const [, force] = useState(0);
  useEffect(() => {
    setF(connFormFrom(s));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    s.region,
    s.model_id,
    s.model_id_light,
    s.endpoint_url,
    s.control_endpoint_url,
    s.prompt_caching,
    s.connection_id,
    s.access?.mode,
    s.access?.role_arn,
    s.access?.external_id,
  ]);
  const set = <K extends keyof ConnForm>(k: K, v: ConnForm[K]) => setF((x) => ({ ...x, [k]: v }));
  const list = conns.data;
  const selected = f.connection_id
    ? (list?.find((c) => String(c.id) === f.connection_id) ?? null)
    : (list?.find((c) => c.is_default) ?? null);
  const errs = connErrors(f, selected?.auth_mode ?? null, selected?.ra_profile_arn);
  const invalid = Object.keys(errs).length > 0;
  const missing = connMissing(f, list);
  // the connection's certificate / update time: a change there needs a new test as well
  const connVersion = selected
    ? `${selected.id}|${selected.updated_at ?? ''}|${certVersion(selected.certificate)}`
    : '';
  const key = useMemo(() => connKey(f, connVersion), [f, connVersion]);
  const testId = sessionTestFor(key);
  const storedKey = connKey(connFormFrom(s), connVersion);
  const dirty = key !== storedKey;
  const onSaved = (r: AssistantSettings) => {
    qc.setQueryData(qk3b.settings, r);
    void qc.invalidateQueries({ queryKey: qk.publicSettings });
    void qc.invalidateQueries({ queryKey: qk.settings });
    void qc.invalidateQueries({ queryKey: qk3b.status });
    void qc.invalidateQueries({ queryKey: qk4.connections });
  };
  const save = useMutation({
    mutationFn: () =>
      endpoints3b.updateSettings({ ...connInput(f), ...(testId ? { test_id: testId } : {}) }),
    onSuccess: (r) => {
      onSaved(r);
      toast.push('success', t('common.saved'));
    },
  });
  const test = useMutation({
    mutationFn: () => endpoints3b.test({ ...connInput(f) }),
    onMutate: () => setTestResult(null),
    onSuccess: (r) => {
      setTestResult(r);
      if (r.ok && r.test_id) {
        sessionTest = {
          key,
          id: r.test_id,
          expires: r.expires_at ? Date.parse(r.expires_at) : Date.now() + 30 * 60_000,
        };
        force((n) => n + 1);
      }
    },
  });
  const listModels = useMutation({
    mutationFn: () => endpoints3b.models({ ...connInput(f) }),
    onSuccess: (r) => setModels(r.items),
  });
  const toggle = useMutation({
    mutationFn: (on: boolean) =>
      endpoints3b.updateSettings(
        on ? { ...connInput(f), enabled: true, test_id: testId ?? undefined } : { enabled: false },
      ),
    onSuccess: (r) => onSaved(r),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!invalid) save.mutate();
  };

  return (
    <Card
      title={t('assistant.settings.connection')}
      anchor={cardAnchor('assistantConnection')}
      actions={<AssistantStateBadge state={s.state} />}
    >
      <form onSubmit={submit} className="space-y-4" data-testid="assistant-connection">
        <p className="text-xs text-slate-500">{t('assistant.settings.connectionIntro')}</p>
        {s.state_error && (
          <p role="alert" className="text-sm text-red-600">
            {errText(s.state_error)} · {formatDateTime(s.state_error.at)}
          </p>
        )}
        {s.warnings.map((w) => (
          <p key={w} className="text-xs text-amber-700 dark:text-amber-300" data-warning={w}>
            {t(`assistant.warning.${w}`, { defaultValue: w })}
          </p>
        ))}

        <section
          className="space-y-2 rounded-md border border-slate-200 p-3 dark:border-slate-700"
          data-anchor={cardAnchor('assistantAwsAccess')}
          data-testid="assistant-aws-access"
        >
          <h3 className="text-sm font-semibold">{t('aws.access.title')}</h3>
          <p className="text-xs text-slate-500">{t('aws.access.intro')}</p>
          {s.connection && (
            <p className="text-xs" data-testid="assistant-aws-current">
              {t('aws.access.current', {
                name: s.connection.name ?? '—',
                mode: t(`aws.access.mode.${s.access.mode}`),
              })}
              {s.connection.account && (
                <>
                  {' · '}
                  <bdi dir="ltr" className="font-mono">
                    {s.connection.account}
                  </bdi>
                </>
              )}
            </p>
          )}
          <AwsAccessBlock
            value={{
              connection_id: f.connection_id,
              mode: f.access_mode,
              role_arn: f.role_arn,
              external_id: f.external_id,
            }}
            connections={list}
            errors={accessErrors(
              formAccess(f),
              selected?.auth_mode ?? null,
              selected?.ra_profile_arn,
            )}
            onChange={(v) =>
              setF((x) => ({
                ...x,
                connection_id: v.connection_id,
                access_mode: v.mode,
                role_arn: v.role_arn,
                external_id: v.external_id,
              }))
            }
          />
          <ErrorBox error={conns.error} />
        </section>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field
            label={t('assistant.settings.region')}
            required
            hint={<Hint err={errs.region} hint={t('assistant.settings.regionHint')} />}
          >
            <Input
              value={f.region}
              onChange={(e) => set('region', e.target.value)}
              dir="ltr"
              list="nm-aws-regions"
              aria-invalid={!!errs.region}
              data-testid="region"
            />
            <datalist id="nm-aws-regions">
              {SUGGESTED_REGIONS.map((r) => (
                <option key={r} value={r} />
              ))}
            </datalist>
          </Field>
          <Field
            label={t('assistant.settings.modelId')}
            required
            className="sm:col-span-2"
            hint={<Hint err={errs.model_id} hint={t('assistant.settings.modelHint')} />}
          >
            <div className="flex gap-2">
              <Input
                value={f.model_id}
                onChange={(e) => set('model_id', e.target.value)}
                dir="ltr"
                placeholder="us.anthropic.claude-…"
                aria-invalid={!!errs.model_id}
                data-testid="model-id"
              />
              <Button size="sm" onClick={() => listModels.mutate()} busy={listModels.isPending}>
                {t('assistant.settings.listModels')}
              </Button>
            </div>
          </Field>
        </div>
        <GeoNote modelId={f.model_id} region={f.region.trim()} />
        {listModels.data?.error && (
          <p className="text-xs text-red-600">{errText(listModels.data.error)}</p>
        )}
        <ErrorBox error={listModels.error} />
        {models && (
          <div className="max-h-48 overflow-y-auto rounded-md ring-1 ring-slate-200 dark:ring-slate-700">
            {models.length === 0 && (
              <p className="p-2 text-xs text-slate-500">{t('assistant.settings.noModels')}</p>
            )}
            {models.map((m) => (
              <button
                key={m.id}
                type="button"
                onClick={() => {
                  set('model_id', m.id);
                  setModels(null);
                }}
                className="flex w-full flex-wrap items-center gap-2 px-2 py-1 text-start text-xs hover:bg-sky-50 dark:hover:bg-slate-800"
              >
                <span dir="ltr" className="font-mono">
                  {m.id}
                </span>
                <span className="text-slate-500">{m.name}</span>
                <span className="ms-auto text-slate-400">
                  {t(`assistant.settings.modelKind.${m.kind}`)}
                </span>
              </button>
            ))}
          </div>
        )}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field
            label={t('assistant.settings.modelLight')}
            hint={<Hint err={errs.model_id_light} hint={t('assistant.settings.modelLightHint')} />}
          >
            <Input
              value={f.model_id_light}
              onChange={(e) => set('model_id_light', e.target.value)}
              dir="ltr"
            />
          </Field>
          <Field
            label={t('assistant.settings.endpoint')}
            hint={<Hint err={errs.endpoint_url} hint={t('assistant.settings.endpointHint')} />}
          >
            <Input
              value={f.endpoint_url}
              onChange={(e) => set('endpoint_url', e.target.value)}
              dir="ltr"
              placeholder={`https://bedrock-runtime.${f.region || 'us-east-1'}.amazonaws.com`}
            />
          </Field>
          <Field
            label={t('assistant.settings.controlEndpoint')}
            hint={
              <Hint
                err={errs.control_endpoint_url}
                hint={t('assistant.settings.controlEndpointHint')}
              />
            }
          >
            <Input
              value={f.control_endpoint_url}
              onChange={(e) => set('control_endpoint_url', e.target.value)}
              dir="ltr"
            />
          </Field>
        </div>
        <div>
          <Checkbox
            label={t('assistant.settings.promptCaching')}
            checked={f.prompt_caching}
            onChange={(v) => set('prompt_caching', v)}
          />
          <p className="ms-6 text-xs text-slate-500">{t('assistant.settings.promptCachingHint')}</p>
        </div>
        <ErrorBox error={save.error} />
        <div className="flex justify-end">
          <Button type="submit" busy={save.isPending} disabled={invalid || !dirty}>
            {t('common.save')}
          </Button>
        </div>

        {/* bottom: Test connection + the enable switch (configure-first) */}
        <div className="space-y-3 border-t border-slate-200 pt-4 dark:border-slate-800">
          <div className="flex flex-wrap items-center gap-3">
            <Button
              onClick={() => test.mutate()}
              busy={test.isPending}
              disabled={invalid}
              data-testid="test-connection"
            >
              {t('assistant.settings.test')}
            </Button>
            {s.last_test && (
              <span className="text-xs text-slate-500">
                {t('assistant.settings.lastTest', {
                  result: s.last_test.ok
                    ? t('assistant.test.okShort')
                    : (s.last_test.error_code ?? ''),
                  time: formatDateTime(s.last_test.at),
                  by: s.last_test.by,
                })}
              </span>
            )}
          </div>
          {missing.length > 0 && (
            <p
              className="text-xs text-amber-700 dark:text-amber-300"
              data-testid="assistant-missing"
            >
              {missing.map((m) => t(`assistant.settings.missing.${m}`)).join(' · ')}
            </p>
          )}
          <ErrorBox error={test.error} />
          {testResult && <TestResultView r={testResult} />}
          <div
            className="flex flex-wrap items-center gap-2"
            data-testid="assistant-switch"
            data-feature="assistant"
          >
            <Toggle
              checked={s.enabled}
              onChange={(v) => toggle.mutate(v)}
              label={t('features.name.assistant')}
              busy={toggle.isPending}
              disabled={!s.enabled && (!testId || invalid || !auditOn)}
            />
            <span className="text-sm font-medium">{t('features.name.assistant')}</span>
            <span
              className={cx(
                'text-xs',
                s.enabled ? 'text-green-700 dark:text-green-400' : 'text-slate-500',
              )}
            >
              {s.enabled ? t('features.on') : t('features.off')}
            </span>
          </div>
          {!s.enabled && !testId && (
            <p className="text-xs text-amber-700 dark:text-amber-300" data-testid="enable-hint">
              {t('assistant.settings.enableNeedsTest')}
            </p>
          )}
          {!s.enabled && !auditOn && (
            <p className="text-xs text-amber-700 dark:text-amber-300">
              {t('features.hint.assistantAudit')}
            </p>
          )}
          {s.enabled && dirty && !testId && (
            <p className="text-xs text-amber-700 dark:text-amber-300">
              {t('assistant.settings.changeNeedsTest')}
            </p>
          )}
          <ErrorBox error={toggle.error} />
        </div>
      </form>
    </Card>
  );
}
