import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints4, qk4 } from '../../api/endpoints4';
import {
  AWS_AUTH_MODES,
  AWS_OVERRIDE_SERVICES,
  AWS_PROXY_MODES,
  type AwsAccess,
  type AwsConnection,
  type AwsOverrideService,
  type AwsTestResult,
} from '../../api/types';
import {
  accessErrors,
  arnAccountsDiffer,
  awsErrors,
  awsFormFrom,
  awsInput,
  awsKey,
  awsMissing,
  awsTestFor,
  certVersion,
  normalizeAccess,
  rememberAwsTest,
  type AwsErrKey,
  type AwsForm,
} from '../../lib/aws';
import { formatDateTime } from '../../lib/format';
import { cardAnchor } from '../../lib/settingsIndex';
import { SecretField } from '../SnmpEditor';
import { useToast } from '../toast';
import {
  Button,
  Card,
  Checkbox,
  ErrorBox,
  Field,
  FieldGroup,
  Input,
  Select,
  Textarea,
} from '../ui';
import { AwsPoliciesPanel } from './AwsPoliciesPanel';
import { AwsTestSteps, TestWarnings } from './AwsTestSteps';
import { RolesAnywhereCertificateCard } from './RolesAnywhereCertificateCard';
import { ExternalIdField } from './AwsAccessBlock';

export function AwsErr({ k }: { k?: AwsErrKey | string }) {
  const { t } = useTranslation();
  if (!k) return null;
  return (
    <span className="text-red-600" data-error={k}>
      {t(`aws.err.${k}`)}
    </span>
  );
}

function hint(err: string | undefined, text?: string) {
  return err ? <AwsErr k={err} /> : text;
}

/** The result of a chain Test: steps per hop with their hints, warnings, the identities. */
export function AwsTestResultView({ r }: { r: AwsTestResult }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-1 text-xs" data-testid="aws-test-result" data-ok={r.ok}>
      <AwsTestSteps steps={r.steps} />
      <TestWarnings warnings={r.warnings} />
      {r.ok && <p className="font-medium text-green-700 dark:text-green-400">{t('aws.test.ok')}</p>}
    </div>
  );
}

type Hop = { mode: 'none' | 'assume_role' | 'ra_role'; role_arn: string; external_id: string };

/**
 * [4] Create / edit one AWS connection (architecture § 15.1.10): name, authentication with its
 * fields, region, network (proxy, endpoint overrides), the Roles Anywhere certificate, the
 * expected IAM policies and *Test* with the step list per hop.
 */
export function AwsConnectionForm({
  connection: c,
  connections,
  assistantOn,
  onClose,
  onCreated,
}: {
  connection: AwsConnection | null;
  connections: AwsConnection[];
  assistantOn: boolean;
  onClose: () => void;
  onCreated: (c: AwsConnection) => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const [f, setF] = useState<AwsForm>(() => awsFormFrom(c));
  const [hop, setHop] = useState<Hop>({ mode: 'none', role_arn: '', external_id: '' });
  const [result, setResult] = useState<AwsTestResult | null>(null);
  const [, force] = useState(0);
  const id = c?.id ?? null;
  useEffect(() => {
    setF(awsFormFrom(c));
    // re-initialise only when another connection is opened or after a save
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, c?.updated_at]);
  const set = <K extends keyof AwsForm>(k: K, v: AwsForm[K]) => setF((x) => ({ ...x, [k]: v }));
  const errs = awsErrors(f);
  const invalid = Object.keys(errs).length > 0;
  const cv = certVersion(c?.certificate ?? null);
  const key = useMemo(() => awsKey(id, f, cv), [id, f, cv]);
  const storedKey = c ? awsKey(id, awsFormFrom(c), cv) : '';
  const credDirty = !c || key !== storedKey;
  const dirty = credDirty || f.name.trim() !== (c?.name ?? '');
  const testId = awsTestFor(key);
  const usedByOn = assistantOn && (c?.used_by.length ?? 0) > 0;
  const needsTest = !!c && usedByOn && credDirty && !testId;
  const missing = awsMissing(f, c);
  const hopAccess: AwsAccess | null =
    hop.mode === 'none'
      ? null
      : normalizeAccess({ mode: hop.mode, role_arn: hop.role_arn, external_id: hop.external_id });
  const hopErrs = hopAccess
    ? accessErrors(hopAccess, f.auth_mode, f.ra_profile_arn)
    : ({} as ReturnType<typeof accessErrors>);
  const hopInvalid = Object.keys(hopErrs).length > 0;
  const refresh = () => void qc.invalidateQueries({ queryKey: qk4.connections });

  const save = useMutation({
    mutationFn: () =>
      c
        ? endpoints4.updateConnection(c.id, {
            ...awsInput(f),
            ...(testId ? { test_id: testId } : {}),
          })
        : endpoints4.createConnection(awsInput(f)),
    onSuccess: (r) => {
      toast.push('success', t('common.saved'));
      refresh();
      void qc.invalidateQueries({ queryKey: ['assistant-settings'] });
      if (!c) onCreated(r);
    },
  });
  const test = useMutation({
    mutationFn: () =>
      endpoints4.test({ connection_id: id, connection: awsInput(f), access: hopAccess }),
    onMutate: () => setResult(null),
    onSuccess: (r) => {
      setResult(r);
      if (r.ok && r.test_id) {
        rememberAwsTest(key, r.test_id, r.expires_at);
        force((n) => n + 1);
      }
      if (c) refresh();
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!invalid && !needsTest) save.mutate();
  };
  const usedList = (c?.used_by ?? [])
    .map((u) => t(`aws.connections.consumer.${u.kind}`, { defaultValue: u.name }))
    .join(', ');

  return (
    <Card
      title={c ? t('aws.form.titleEdit', { name: c.name }) : t('aws.form.titleNew')}
      anchor={cardAnchor('awsConnection')}
      actions={
        <Button size="sm" variant="ghost" onClick={onClose}>
          {t('common.close')}
        </Button>
      }
    >
      <form onSubmit={submit} className="space-y-4" data-testid="aws-connection-form">
        {(c?.warnings ?? []).map((w) => (
          <p key={w} className="text-xs text-amber-700 dark:text-amber-300" data-warning={w}>
            {w === 'endpoint_unresolved' && c?.unresolved_hosts?.length
              ? t('aws.form.unresolved', { hosts: c.unresolved_hosts.join(', ') })
              : t(`aws.warning.${w}`, { defaultValue: w })}
          </p>
        ))}
        {c && c.used_by.length > 0 && (
          <p className="text-xs text-slate-500">{t('aws.form.used', { list: usedList })}</p>
        )}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label={t('aws.form.name')} required hint={hint(errs.name)}>
            <Input
              value={f.name}
              onChange={(e) => set('name', e.target.value)}
              maxLength={64}
              aria-invalid={!!errs.name}
              data-testid="aws-name"
            />
          </Field>
          <Field
            label={t('aws.form.region')}
            required
            className="sm:col-span-2"
            hint={hint(errs.region, t('aws.form.regionHint'))}
          >
            <Input
              value={f.region}
              onChange={(e) => set('region', e.target.value)}
              dir="ltr"
              list="nm-aws-conn-regions"
              aria-invalid={!!errs.region}
              data-testid="aws-region"
            />
            <datalist id="nm-aws-conn-regions">
              {['us-east-1', 'us-west-2', 'il-central-1', 'eu-central-1'].map((r) => (
                <option key={r} value={r} />
              ))}
            </datalist>
          </Field>
        </div>

        <FieldGroup label={t('aws.form.authMode')}>
          <div role="radiogroup" className="flex flex-col gap-1">
            {AWS_AUTH_MODES.map((m) => (
              <label key={m} className="inline-flex items-start gap-2 text-sm">
                <input
                  type="radio"
                  name="aws_auth_mode"
                  className="mt-1"
                  checked={f.auth_mode === m}
                  onChange={() => set('auth_mode', m)}
                  data-testid={`aws-auth-${m}`}
                />
                <span>
                  <span className="font-medium">{t(`aws.auth.${m}`)}</span>
                  {m === 'roles_anywhere' && (
                    <span className="ms-1 rounded bg-green-100 px-1 text-[10px] text-green-800 dark:bg-green-900 dark:text-green-200">
                      {t('assistant.settings.recommended')}
                    </span>
                  )}
                  <span className="block text-xs text-slate-500">{t(`aws.authHint.${m}`)}</span>
                </span>
              </label>
            ))}
          </div>
        </FieldGroup>

        {f.auth_mode === 'roles_anywhere' && (
          <div className="space-y-3">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              {(
                [
                  [
                    'ra_trust_anchor_arn',
                    'raTrustAnchor',
                    'arn:aws:rolesanywhere:us-east-1:111111111111:trust-anchor/…',
                  ],
                  [
                    'ra_profile_arn',
                    'raProfile',
                    'arn:aws:rolesanywhere:us-east-1:111111111111:profile/…',
                  ],
                  ['ra_role_arn', 'raRole', 'arn:aws:iam::111111111111:role/netmon-base'],
                ] as const
              ).map(([k, label, ph]) => (
                <Field
                  key={k}
                  label={t(`aws.form.${label}`)}
                  required
                  className="sm:col-span-2"
                  hint={hint(f[k].trim() ? errs[k] : undefined)}
                >
                  <Input
                    value={f[k]}
                    onChange={(e) => set(k, e.target.value)}
                    dir="ltr"
                    placeholder={ph}
                    aria-invalid={!!f[k].trim() && !!errs[k]}
                    data-testid={`aws-${k}`}
                  />
                </Field>
              ))}
              <Field
                label={t('aws.form.raRegion')}
                hint={hint(errs.ra_region, t('aws.form.raRegionHint'))}
              >
                <Input
                  value={f.ra_region}
                  onChange={(e) => set('ra_region', e.target.value)}
                  dir="ltr"
                  placeholder={f.region}
                />
              </Field>
              <Field
                label={t('aws.form.raSession')}
                hint={hint(errs.ra_session_seconds, t('aws.form.raSessionHint'))}
              >
                <Input
                  type="number"
                  min={900}
                  max={43200}
                  value={f.ra_session_seconds}
                  onChange={(e) => set('ra_session_seconds', e.target.value)}
                  dir="ltr"
                />
              </Field>
            </div>
            {arnAccountsDiffer(f) && (
              <p
                className="text-xs text-amber-700 dark:text-amber-300"
                data-warning="arn_accounts_differ"
              >
                {t('aws.form.arnAccounts')}
              </p>
            )}
            {c ? (
              <RolesAnywhereCertificateCard c={c} connections={connections} locked={usedByOn} />
            ) : (
              <p className="text-xs text-slate-500" data-testid="save-first">
                {t('aws.form.saveFirst')}
              </p>
            )}
          </div>
        )}

        {f.auth_mode === 'access_key' && (
          <div className="space-y-2">
            <p className="text-xs text-amber-700 dark:text-amber-300">
              {t('aws.form.accessKeyAdvice')}
            </p>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <Field label={t('aws.form.accessKeyId')} hint={hint(errs.access_key_id)}>
                <Input
                  value={f.access_key_id}
                  onChange={(e) => set('access_key_id', e.target.value)}
                  dir="ltr"
                  autoComplete="off"
                  aria-invalid={!!errs.access_key_id}
                  data-testid="aws-access_key_id"
                />
              </Field>
              <SecretField
                label={t('aws.form.secretKey')}
                value={f.secret_access_key}
                isSet={!!c?.secret_access_key_set}
                clear={f.clear_secret}
                onValue={(v) => set('secret_access_key', v)}
                onClear={(v) => set('clear_secret', v)}
              />
              <Field label={t('aws.form.keyCreated')} hint={t('aws.form.keyCreatedHint')}>
                <Input
                  type="date"
                  value={f.access_key_created_at.slice(0, 10)}
                  onChange={(e) => set('access_key_created_at', e.target.value)}
                  dir="ltr"
                />
              </Field>
            </div>
            {c?.secret_access_key_unreadable && (
              <p className="text-xs text-red-600">{t('aws.form.secretUnreadable')}</p>
            )}
            <Field
              label={t('aws.form.sessionToken')}
              hint={hint(
                errs.session_token,
                c?.session_token_set
                  ? t('aws.form.sessionTokenStored')
                  : t('aws.form.sessionTokenHint'),
              )}
            >
              <Textarea
                value={f.session_token}
                rows={2}
                disabled={f.clear_session_token}
                placeholder={c?.session_token_set ? t('secret.stored') : ''}
                onChange={(e) => set('session_token', e.target.value)}
                dir="ltr"
                autoComplete="off"
                spellCheck={false}
                className="font-mono text-[11px]"
              />
            </Field>
            {c?.session_token_set && (
              <Checkbox
                label={t('aws.form.sessionTokenClear')}
                checked={f.clear_session_token}
                onChange={(v) => set('clear_session_token', v)}
              />
            )}
          </div>
        )}

        {f.auth_mode === 'default_chain' && (
          <p className="text-xs text-slate-600 dark:text-slate-400">
            {t('aws.form.defaultChainInfo')}
          </p>
        )}

        <fieldset className="space-y-3 border-t border-slate-200 pt-3 dark:border-slate-800">
          <legend className="text-sm font-semibold">{t('aws.form.network')}</legend>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label={t('aws.form.proxyMode')} hint={t(`aws.form.proxyHint.${f.proxy_mode}`)}>
              <Select
                value={f.proxy_mode}
                onChange={(e) => set('proxy_mode', e.target.value as AwsForm['proxy_mode'])}
              >
                {AWS_PROXY_MODES.map((m) => (
                  <option key={m} value={m}>
                    {t(`aws.form.proxy.${m}`)}
                  </option>
                ))}
              </Select>
            </Field>
            {f.proxy_mode === 'custom' && (
              <Field label={t('aws.form.proxyUrl')} hint={hint(errs.proxy_url)}>
                <Input
                  value={f.proxy_url}
                  onChange={(e) => set('proxy_url', e.target.value)}
                  dir="ltr"
                  placeholder="http://proxy:3128"
                />
              </Field>
            )}
          </div>
          <FieldGroup label={t('aws.form.overrides')} hint={t('aws.form.overridesHint')}>
            <div className="space-y-2" data-testid="aws-overrides">
              {f.overrides.map((o, i) => {
                const e = errs[`override.${i}`];
                const upd = (patch: Partial<typeof o>) =>
                  set(
                    'overrides',
                    f.overrides.map((x, j) => (j === i ? { ...x, ...patch } : x)),
                  );
                return (
                  <div key={i} className="space-y-0.5" data-override={i}>
                    <div className="grid grid-cols-[10rem_8rem_1fr_auto] items-center gap-2 max-sm:grid-cols-2">
                      <Select
                        value={o.service}
                        aria-label={t('aws.form.overrideService')}
                        onChange={(ev) => upd({ service: ev.target.value as AwsOverrideService })}
                      >
                        {AWS_OVERRIDE_SERVICES.map((s) => (
                          <option key={s} value={s}>
                            {s}
                          </option>
                        ))}
                      </Select>
                      <Input
                        value={o.region}
                        aria-label={t('aws.form.overrideRegion')}
                        onChange={(ev) => upd({ region: ev.target.value })}
                        dir="ltr"
                        placeholder={f.region}
                      />
                      <Input
                        value={o.url}
                        aria-label={t('aws.form.overrideUrl')}
                        onChange={(ev) => upd({ url: ev.target.value })}
                        dir="ltr"
                        placeholder={`https://vpce-….${o.service}.${o.region || f.region}.vpce.amazonaws.com`}
                        className="max-sm:col-span-2"
                      />
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() =>
                          set(
                            'overrides',
                            f.overrides.filter((_, j) => j !== i),
                          )
                        }
                      >
                        {t('aws.form.removeOverride')}
                      </Button>
                    </div>
                    {e && (
                      <p className="text-xs">
                        <AwsErr k={e} />
                      </p>
                    )}
                  </div>
                );
              })}
              <Button
                size="sm"
                onClick={() =>
                  set('overrides', [...f.overrides, { service: 'sts', region: f.region, url: '' }])
                }
                data-testid="add-override"
              >
                {t('aws.form.addOverride')}
              </Button>
            </div>
          </FieldGroup>
        </fieldset>

        <ErrorBox error={save.error} />
        <div className="flex flex-wrap items-center justify-end gap-2">
          {needsTest && (
            <p
              className="me-auto text-xs text-amber-700 dark:text-amber-300"
              data-testid="needs-test"
            >
              {t('aws.form.needsTest')}
            </p>
          )}
          <Button
            type="submit"
            variant="primary"
            busy={save.isPending}
            disabled={invalid || !dirty || needsTest}
            data-testid="aws-save"
          >
            {c ? t('common.save') : t('aws.form.create')}
          </Button>
        </div>

        {/* Test: the step list per hop, with an optional role hop (not stored) */}
        <div className="space-y-3 border-t border-slate-200 pt-4 dark:border-slate-800">
          <FieldGroup label={t('aws.form.hopTitle')} hint={t('aws.form.hopHint')}>
            <div className="space-y-2">
              <div role="radiogroup" className="flex flex-wrap gap-4 text-sm">
                {(['none', 'assume_role', 'ra_role'] as const)
                  .filter((m) => m !== 'ra_role' || f.auth_mode === 'roles_anywhere')
                  .map((m) => (
                    <label key={m} className="inline-flex items-center gap-1.5">
                      <input
                        type="radio"
                        name="aws_test_hop"
                        checked={hop.mode === m}
                        onChange={() => setHop((h) => ({ ...h, mode: m }))}
                        data-testid={`hop-${m}`}
                      />
                      {m === 'none' ? t('aws.access.mode.direct') : t(`aws.access.mode.${m}`)}
                    </label>
                  ))}
              </div>
              {hop.mode !== 'none' && (
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <Field
                    label={t('aws.access.roleArn')}
                    hint={hint(hop.role_arn.trim() ? hopErrs.role_arn : undefined)}
                  >
                    <Input
                      value={hop.role_arn}
                      onChange={(e) => setHop((h) => ({ ...h, role_arn: e.target.value }))}
                      dir="ltr"
                      placeholder="arn:aws:iam::222222222222:role/netmon-bedrock"
                      data-testid="hop-role-arn"
                    />
                  </Field>
                  {hop.mode === 'assume_role' && (
                    <ExternalIdField
                      value={hop.external_id}
                      err={hopErrs.external_id}
                      onChange={(v) => setHop((h) => ({ ...h, external_id: v }))}
                    />
                  )}
                </div>
              )}
            </div>
          </FieldGroup>
          <div className="flex flex-wrap items-center gap-3">
            <Button
              onClick={() => test.mutate()}
              busy={test.isPending}
              disabled={invalid || hopInvalid}
              data-testid="aws-test"
            >
              {t('aws.form.test')}
            </Button>
            {c?.last_test && (
              <span className="text-xs text-slate-500">
                {t('assistant.settings.lastTest', {
                  result: c.last_test.ok
                    ? t('assistant.test.okShort')
                    : (c.last_test.error_code ?? ''),
                  time: formatDateTime(c.last_test.at),
                  by: c.last_test.by,
                })}
              </span>
            )}
          </div>
          {missing.length > 0 && (
            <p className="text-xs text-amber-700 dark:text-amber-300">
              {missing.map((m) => t(`aws.form.missing.${m}`)).join(' · ')}
            </p>
          )}
          <ErrorBox error={test.error} />
          {result && <AwsTestResultView r={result} />}
        </div>

        {c && (
          <AwsPoliciesPanel
            connectionId={c.id}
            roleArn={
              hopAccess?.mode === 'assume_role' && !hopErrs.role_arn ? hopAccess.role_arn : null
            }
            externalId={
              hopAccess?.mode === 'assume_role' && !hopErrs.external_id
                ? hopAccess.external_id
                : null
            }
          />
        )}
      </form>
    </Card>
  );
}
