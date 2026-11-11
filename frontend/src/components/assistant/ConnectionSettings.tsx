import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { qk } from '../../api/endpoints';
import { endpoints3b, qk3b } from '../../api/endpoints3b';
import {
  AUTH_MODES,
  PROXY_MODES,
  type AssistantSettings,
  type AssistantTestResult,
  type CertInfo,
  type ModelListItem,
} from '../../api/types';
import {
  arnAccountsDiffer,
  certTone,
  connErrors,
  connFormFrom,
  connInput,
  connKey,
  connMissing,
  daysUntil,
  profileScope,
  SUGGESTED_REGIONS,
  type ConnForm,
} from '../../lib/assistant';
import { formatDateTime } from '../../lib/format';
import { Toggle } from '../features/common';
import { CheckIcon, CopyIcon, XIcon } from '../icons';
import { SecretField } from '../SnmpEditor';
import { useToast } from '../toast';
import {
  Button,
  buttonClass,
  Card,
  Checkbox,
  ConfirmDialog,
  cx,
  ErrorBox,
  Field,
  FieldGroup,
  Input,
  Select,
  Textarea,
} from '../ui';
import { AssistantStateBadge, useAssistantErrorText } from './common';

/** A successful test in this page session (module scope: survives switching settings tabs). */
let sessionTest: { key: string; id: string; expires: number } | null = null;
export function sessionTestFor(key: string): string | null {
  if (!sessionTest || sessionTest.key !== key || Date.now() > sessionTest.expires) return null;
  return sessionTest.id;
}

function certVersion(c: CertInfo | null): string {
  return c ? `${c.sha256}|${c.uploaded_at ?? ''}|${c.source}` : '';
}

const ERR_KEYS: Record<string, string> = {
  region: 'assistant.settings.err.region',
  model: 'assistant.settings.err.model',
  url: 'assistant.settings.err.url',
  proxy: 'assistant.settings.err.proxy',
  arn: 'assistant.settings.err.arn',
  session: 'assistant.settings.err.session',
  accessKey: 'assistant.settings.err.accessKey',
  sessionToken: 'assistant.settings.err.sessionToken',
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

function CertDetails({ c, onDelete }: { c: CertInfo; onDelete?: () => void }) {
  const { t } = useTranslation();
  const days = daysUntil(c.not_after);
  const tone = certTone(days);
  return (
    <div className="space-y-1 text-xs" data-testid="cert-info">
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
        <dt className="text-slate-500">{t('assistant.cert.subject')}</dt>
        <dd dir="ltr" className="break-all text-start">
          {c.subject}
        </dd>
        <dt className="text-slate-500">{t('assistant.cert.issuer')}</dt>
        <dd dir="ltr" className="break-all text-start">
          {c.issuer}
        </dd>
        <dt className="text-slate-500">{t('assistant.cert.serial')}</dt>
        <dd dir="ltr" className="break-all text-start font-mono">
          {c.serial_hex}
        </dd>
        <dt className="text-slate-500">{t('assistant.cert.validity')}</dt>
        <dd>
          {formatDateTime(c.not_before)} → {formatDateTime(c.not_after)}{' '}
          <span
            className={cx(
              tone === 'error' && 'text-red-600',
              tone === 'warn' && 'text-amber-700 dark:text-amber-300',
              tone === 'ok' && 'text-green-700 dark:text-green-400',
            )}
          >
            (
            {days !== null && days < 0
              ? t('assistant.cert.expired')
              : t('assistant.cert.daysLeft', { count: days ?? 0 })}
            )
          </span>
        </dd>
        <dt className="text-slate-500">{t('assistant.cert.key')}</dt>
        <dd>
          {c.key_type.toUpperCase()} {c.key_bits}
          {c.key_unreadable && (
            <span className="ms-2 text-red-600">{t('assistant.cert.keyUnreadable')}</span>
          )}
        </dd>
        <dt className="text-slate-500">SHA-256</dt>
        <dd dir="ltr" className="break-all text-start font-mono text-[10px]">
          {c.sha256}
        </dd>
        {c.chain.length > 0 && (
          <>
            <dt className="text-slate-500">{t('assistant.cert.chain')}</dt>
            <dd>
              {c.chain.map((x, i) => (
                <span key={i} className="block" dir="ltr">
                  {x.subject} ({formatDateTime(x.not_after)})
                </span>
              ))}
            </dd>
          </>
        )}
        <dt className="text-slate-500">{t('assistant.cert.source')}</dt>
        <dd>
          {c.source === 'files'
            ? t('assistant.cert.fromFiles')
            : t('assistant.cert.uploaded', { time: formatDateTime(c.uploaded_at) })}
        </dd>
      </dl>
      {onDelete && c.source === 'uploaded' && (
        <Button size="sm" variant="ghost" onClick={onDelete}>
          {t('assistant.cert.delete')}
        </Button>
      )}
    </div>
  );
}

/** Roles Anywhere certificate: upload (PEM / PKCS#12) or "Generate CSR" → upload the issued cert. */
function CertificateCard({ s, locked }: { s: AssistantSettings; locked: boolean }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const pending = s.csr?.pem ? s.csr : null;
  const [mode, setMode] = useState<'upload' | 'csr'>(pending ? 'csr' : 'upload');
  const [file, setFile] = useState<File | null>(null);
  const [key, setKey] = useState<File | null>(null);
  const [password, setPassword] = useState('');
  const [cn, setCn] = useState('netmonitor-bedrock');
  const [keyType, setKeyType] = useState<'rsa' | 'ec'>('rsa');
  const [deleting, setDeleting] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const keyRef = useRef<HTMLInputElement>(null);
  const isPfx = !!file && /\.(pfx|p12)$/i.test(file.name);
  const refresh = () => void qc.invalidateQueries({ queryKey: qk3b.settings });
  const upload = useMutation({
    mutationFn: () => endpoints3b.uploadCert(file!, mode === 'csr' ? null : key, password),
    onSuccess: () => {
      toast.push('success', t('assistant.cert.uploadedOk'));
      setFile(null);
      setKey(null);
      setPassword('');
      if (fileRef.current) fileRef.current.value = '';
      if (keyRef.current) keyRef.current.value = '';
      refresh();
    },
  });
  const makeCsr = useMutation({
    mutationFn: () => endpoints3b.createCsr({ common_name: cn.trim(), key_type: keyType }),
    onSuccess: refresh,
  });
  const discardCsr = useMutation({ mutationFn: endpoints3b.deleteCsr, onSuccess: refresh });
  const del = useMutation({
    mutationFn: () => endpoints3b.deleteCert(),
    onSuccess: () => {
      setDeleting(false);
      refresh();
    },
  });
  const fromFiles = s.certificate?.source === 'files' || !!s.certificate_host_files;
  return (
    <div
      className="space-y-3 rounded-md border border-slate-200 p-3 dark:border-slate-700"
      data-testid="cert-card"
    >
      <h4 className="text-sm font-semibold">{t('assistant.cert.title')}</h4>
      {s.certificate ? (
        <CertDetails c={s.certificate} onDelete={locked ? undefined : () => setDeleting(true)} />
      ) : (
        <p className="text-xs text-amber-700 dark:text-amber-300">{t('assistant.cert.none')}</p>
      )}
      {fromFiles ? (
        <p className="text-xs text-slate-500">{t('assistant.cert.filesReadOnly')}</p>
      ) : (
        <>
          <div role="radiogroup" className="flex flex-wrap gap-4 text-sm">
            {(['upload', 'csr'] as const).map((m) => (
              <label key={m} className="inline-flex items-center gap-1.5">
                <input type="radio" checked={mode === m} onChange={() => setMode(m)} />
                {t(`assistant.cert.mode.${m}`)}
              </label>
            ))}
          </div>
          {mode === 'csr' && (
            <div className="space-y-2">
              <p className="text-xs text-slate-500">{t('assistant.cert.csrIntro')}</p>
              {pending ? (
                <div className="space-y-1" data-testid="csr-pending">
                  <p className="text-xs">
                    {t('assistant.cert.csrPending', {
                      subject: pending.subject ?? '',
                      time: formatDateTime(pending.created_at),
                    })}
                  </p>
                  {pending.key_unreadable && (
                    <p className="text-xs text-red-600">{t('assistant.cert.keyUnreadable')}</p>
                  )}
                  <Textarea
                    readOnly
                    value={pending.pem ?? ''}
                    rows={6}
                    dir="ltr"
                    className="font-mono text-[11px]"
                    data-testid="csr-pem"
                  />
                  <div className="flex flex-wrap gap-2">
                    <Button
                      size="sm"
                      onClick={() => {
                        void navigator.clipboard?.writeText(pending.pem ?? '');
                        toast.push('success', t('assistant.copied'));
                      }}
                    >
                      <CopyIcon size={12} /> {t('assistant.copy')}
                    </Button>
                    <a
                      className={buttonClass('secondary', 'sm')}
                      href={endpoints3b.csrDownloadUrl()}
                      download
                    >
                      {t('assistant.cert.downloadCsr')}
                    </a>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => discardCsr.mutate()}
                      busy={discardCsr.isPending}
                    >
                      {t('assistant.cert.discardCsr')}
                    </Button>
                  </div>
                  <p className="text-xs text-slate-500">{t('assistant.cert.csrNext')}</p>
                  <ErrorBox error={discardCsr.error} />
                </div>
              ) : (
                <>
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                    <Field label={t('assistant.cert.cn')} className="sm:col-span-2">
                      <Input
                        value={cn}
                        onChange={(e) => setCn(e.target.value)}
                        dir="ltr"
                        maxLength={64}
                      />
                    </Field>
                    <Field label={t('assistant.cert.keyType')}>
                      <Select
                        value={keyType}
                        onChange={(e) => setKeyType(e.target.value as 'rsa' | 'ec')}
                      >
                        <option value="rsa">RSA 2048</option>
                        <option value="ec">ECDSA P-256</option>
                      </Select>
                    </Field>
                  </div>
                  <Button
                    size="sm"
                    onClick={() => makeCsr.mutate()}
                    busy={makeCsr.isPending}
                    disabled={!cn.trim()}
                    data-testid="csr-generate"
                  >
                    {t('assistant.cert.generateCsr')}
                  </Button>
                  <ErrorBox error={makeCsr.error} />
                </>
              )}
            </div>
          )}
          {(mode === 'upload' || pending) && (
            <>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                <Field
                  label={mode === 'csr' ? t('assistant.cert.signedFile') : t('assistant.cert.file')}
                  hint={
                    mode === 'csr' ? t('assistant.cert.signedHint') : t('assistant.cert.fileHint')
                  }
                >
                  <input
                    ref={fileRef}
                    type="file"
                    accept=".pem,.crt,.cer,.pfx,.p12"
                    className="block w-full text-xs"
                    onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                    data-testid="cert-file"
                  />
                </Field>
                {mode === 'upload' && !isPfx && (
                  <Field label={t('assistant.cert.keyFile')} hint={t('assistant.cert.keyHint')}>
                    <input
                      ref={keyRef}
                      type="file"
                      accept=".pem,.key"
                      className="block w-full text-xs"
                      onChange={(e) => setKey(e.target.files?.[0] ?? null)}
                      data-testid="cert-key"
                    />
                  </Field>
                )}
                {mode === 'upload' && (
                  <Field
                    label={t('assistant.cert.password')}
                    hint={t('assistant.cert.passwordHint')}
                  >
                    <Input
                      type="password"
                      autoComplete="new-password"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      dir="ltr"
                      data-testid="cert-password"
                    />
                  </Field>
                )}
              </div>
              <Button
                size="sm"
                onClick={() => upload.mutate()}
                busy={upload.isPending}
                disabled={!file || (mode === 'upload' && !isPfx && !key)}
                data-testid="cert-upload"
              >
                {t('assistant.cert.upload')}
              </Button>
              <ErrorBox error={upload.error} />
            </>
          )}
        </>
      )}
      <p className="text-xs text-slate-500">{t('assistant.cert.requirements')}</p>
      <ConfirmDialog
        open={deleting}
        title={t('assistant.cert.delete')}
        message={t('assistant.cert.deleteConfirm')}
        onConfirm={() => del.mutate()}
        onClose={() => {
          setDeleting(false);
          del.reset();
        }}
        busy={del.isPending}
        error={del.error}
      />
    </div>
  );
}

function TestResultView({ r }: { r: AssistantTestResult }) {
  const { t } = useTranslation();
  const errText = useAssistantErrorText();
  return (
    <div className="space-y-1 text-xs" data-testid="test-result" data-ok={r.ok}>
      <ul className="space-y-0.5">
        {r.steps.map((s) => (
          <li key={s.step} className="flex flex-wrap items-start gap-1.5" data-step={s.step}>
            {s.ok ? (
              <CheckIcon size={13} className="mt-0.5 text-green-600" />
            ) : (
              <XIcon size={13} className="mt-0.5 text-red-600" />
            )}
            <span className="font-medium">{t(`assistant.test.step.${s.step}`)}</span>
            <span className="text-slate-400">{s.ms} ms</span>
            <span dir="auto" className="basis-full ps-5 text-slate-600 dark:text-slate-400">
              {s.error_code ? errText({ code: s.error_code, message: s.detail }) : s.detail}
            </span>
            {s.error_code && s.detail && (
              <bdi
                dir="ltr"
                className="basis-full ps-5 font-mono text-[11px] text-slate-500"
                data-testid="test-step-raw"
              >
                {s.detail}
              </bdi>
            )}
          </li>
        ))}
      </ul>
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
      {r.warnings.map((w) => (
        <p key={w} className="text-amber-700 dark:text-amber-300">
          {t(`assistant.warning.${w}`, { defaultValue: w })}
        </p>
      ))}
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
 * Optional session token of temporary credentials (ASIA… keys, e.g. IAM Identity Center): write-only
 * like the secret — the stored value is never shown; type a new one to replace it, or clear it.
 */
function SessionTokenField({
  value,
  isSet,
  clear,
  err,
  onValue,
  onClear,
}: {
  value: string;
  isSet: boolean;
  clear: boolean;
  err?: string;
  onValue: (v: string) => void;
  onClear: (v: boolean) => void;
}) {
  const { t } = useTranslation();
  return (
    <div data-testid="session-token-field" data-set={isSet}>
      <Field
        label={t('assistant.settings.sessionToken')}
        hint={
          <Hint
            err={err}
            hint={
              isSet
                ? t('assistant.settings.sessionTokenStored')
                : t('assistant.settings.sessionTokenHint')
            }
          />
        }
      >
        <Textarea
          value={value}
          rows={3}
          disabled={clear}
          placeholder={isSet ? t('secret.stored') : ''}
          onChange={(e) => onValue(e.target.value)}
          dir="ltr"
          autoComplete="off"
          spellCheck={false}
          className="font-mono text-[11px]"
          aria-invalid={!!err}
          data-testid="session-token"
        />
      </Field>
      {isSet && (
        <span className="mt-1 block">
          <Checkbox
            label={t('assistant.settings.sessionTokenClear')}
            checked={clear}
            onChange={onClear}
          />
        </span>
      )}
    </div>
  );
}

/**
 * [3b] Settings → Assistant → Connection (configure-first, § 14.2.6): the form is always shown;
 * Test connection + the enable switch at the bottom; the switch is locked until a test of the
 * current form values succeeded in this page session (any connection edit locks it again).
 */
export function ConnectionSettings({ s, auditOn }: { s: AssistantSettings; auditOn: boolean }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const errText = useAssistantErrorText();
  const [f, setF] = useState<ConnForm>(() => connFormFrom(s));
  const [models, setModels] = useState<ModelListItem[] | null>(null);
  const [testResult, setTestResult] = useState<AssistantTestResult | null>(null);
  const [, force] = useState(0);
  useEffect(() => {
    setF(connFormFrom(s));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    s.auth_mode,
    s.region,
    s.model_id,
    s.model_id_light,
    s.endpoint_url,
    s.control_endpoint_url,
    s.proxy_mode,
    s.proxy_url,
    s.prompt_caching,
    s.access_key_id,
    s.secret_access_key_set,
    s.session_token_set,
    s.access_key_created_at,
    s.ra_region,
    s.ra_endpoint_url,
    s.ra_trust_anchor_arn,
    s.ra_profile_arn,
    s.ra_role_arn,
    s.ra_session_seconds,
  ]);
  const set = <K extends keyof ConnForm>(k: K, v: ConnForm[K]) => setF((x) => ({ ...x, [k]: v }));
  const errs = connErrors(f);
  const invalid = Object.keys(errs).length > 0;
  const missing = connMissing(f, s);
  const key = useMemo(() => connKey(f, certVersion(s.certificate)), [f, s.certificate]);
  const testId = sessionTestFor(key);
  const storedKey = connKey(connFormFrom(s), certVersion(s.certificate));
  const dirty = key !== storedKey;
  const onSaved = (r: AssistantSettings) => {
    qc.setQueryData(qk3b.settings, r);
    void qc.invalidateQueries({ queryKey: qk.publicSettings });
    void qc.invalidateQueries({ queryKey: qk.settings });
    void qc.invalidateQueries({ queryKey: qk3b.status });
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
    mutationFn: () => endpoints3b.test(connInput(f)),
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
    mutationFn: () => endpoints3b.models(connInput(f)),
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
  const accessKeyAge = daysUntil(f.access_key_created_at || null);
  const locked = s.enabled && f.auth_mode === 'roles_anywhere';

  return (
    <Card
      title={t('assistant.settings.connection')}
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
        <FieldGroup label={t('assistant.settings.authMode')}>
          <div role="radiogroup" className="flex flex-col gap-1">
            {AUTH_MODES.map((m) => (
              <label key={m} className="inline-flex items-start gap-2 text-sm">
                <input
                  type="radio"
                  name="auth_mode"
                  className="mt-1"
                  checked={f.auth_mode === m}
                  onChange={() => set('auth_mode', m)}
                />
                <span>
                  <span className="font-medium">{t(`assistant.settings.auth.${m}`)}</span>
                  {m === 'roles_anywhere' && (
                    <span className="ms-1 rounded bg-green-100 px-1 text-[10px] text-green-800 dark:bg-green-900 dark:text-green-200">
                      {t('assistant.settings.recommended')}
                    </span>
                  )}
                  <span className="block text-xs text-slate-500">
                    {t(`assistant.settings.authHint.${m}`)}
                  </span>
                </span>
              </label>
            ))}
          </div>
        </FieldGroup>

        {f.auth_mode === 'roles_anywhere' && (
          <div className="space-y-3">
            <CertificateCard s={s} locked={locked} />
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field
                label={t('assistant.settings.raTrustAnchor')}
                className="sm:col-span-2"
                hint={<Hint err={errs.ra_trust_anchor_arn} />}
              >
                <Input
                  value={f.ra_trust_anchor_arn}
                  onChange={(e) => set('ra_trust_anchor_arn', e.target.value)}
                  dir="ltr"
                  placeholder="arn:aws:rolesanywhere:us-east-1:123456789012:trust-anchor/…"
                  aria-invalid={!!errs.ra_trust_anchor_arn}
                />
              </Field>
              <Field
                label={t('assistant.settings.raProfile')}
                className="sm:col-span-2"
                hint={<Hint err={errs.ra_profile_arn} />}
              >
                <Input
                  value={f.ra_profile_arn}
                  onChange={(e) => set('ra_profile_arn', e.target.value)}
                  dir="ltr"
                  placeholder="arn:aws:rolesanywhere:us-east-1:123456789012:profile/…"
                  aria-invalid={!!errs.ra_profile_arn}
                />
              </Field>
              <Field
                label={t('assistant.settings.raRole')}
                className="sm:col-span-2"
                hint={<Hint err={errs.ra_role_arn} />}
              >
                <Input
                  value={f.ra_role_arn}
                  onChange={(e) => set('ra_role_arn', e.target.value)}
                  dir="ltr"
                  placeholder="arn:aws:iam::123456789012:role/netmonitor-bedrock"
                  aria-invalid={!!errs.ra_role_arn}
                />
              </Field>
              <Field
                label={t('assistant.settings.raRegion')}
                hint={<Hint err={errs.ra_region} hint={t('assistant.settings.raRegionHint')} />}
              >
                <Input
                  value={f.ra_region}
                  onChange={(e) => set('ra_region', e.target.value)}
                  dir="ltr"
                  placeholder={f.region}
                />
              </Field>
              <Field
                label={t('assistant.settings.raSession')}
                hint={
                  <Hint
                    err={errs.ra_session_seconds}
                    hint={t('assistant.settings.raSessionHint')}
                  />
                }
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
              <Field
                label={t('assistant.settings.raEndpoint')}
                className="sm:col-span-2"
                hint={
                  <Hint err={errs.ra_endpoint_url} hint={t('assistant.settings.raEndpointHint')} />
                }
              >
                <Input
                  value={f.ra_endpoint_url}
                  onChange={(e) => set('ra_endpoint_url', e.target.value)}
                  dir="ltr"
                  placeholder="https://rolesanywhere.us-east-1.amazonaws.com"
                />
              </Field>
            </div>
            {arnAccountsDiffer(f) && (
              <p className="text-xs text-amber-700 dark:text-amber-300">
                {t('assistant.settings.arnAccounts')}
              </p>
            )}
          </div>
        )}

        {f.auth_mode === 'access_key' && (
          <div className="space-y-2">
            <p className="text-xs text-amber-700 dark:text-amber-300">
              {t('assistant.settings.accessKeyAdvice')}
            </p>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <Field
                label={t('assistant.settings.accessKeyId')}
                hint={<Hint err={errs.access_key_id} />}
              >
                <Input
                  value={f.access_key_id}
                  onChange={(e) => set('access_key_id', e.target.value)}
                  dir="ltr"
                  autoComplete="off"
                  aria-invalid={!!errs.access_key_id}
                />
              </Field>
              <SecretField
                label={t('assistant.settings.secretKey')}
                value={f.secret_access_key}
                isSet={s.secret_access_key_set}
                clear={f.clear_secret}
                onValue={(v) => set('secret_access_key', v)}
                onClear={(v) => set('clear_secret', v)}
              />
              <Field
                label={t('assistant.settings.keyCreated')}
                hint={
                  accessKeyAge !== null && accessKeyAge < -90 ? (
                    <span className="text-amber-700">
                      {t('assistant.settings.keyOld', { days: -accessKeyAge })}
                    </span>
                  ) : (
                    t('assistant.settings.keyCreatedHint')
                  )
                }
              >
                <Input
                  type="date"
                  value={f.access_key_created_at.slice(0, 10)}
                  onChange={(e) => set('access_key_created_at', e.target.value)}
                  dir="ltr"
                />
              </Field>
            </div>
            {s.secret_access_key_unreadable && (
              <p className="text-xs text-red-600">{t('assistant.settings.secretUnreadable')}</p>
            )}
            <SessionTokenField
              value={f.session_token}
              isSet={!!s.session_token_set}
              clear={f.clear_session_token}
              err={errs.session_token}
              onValue={(v) => set('session_token', v)}
              onClear={(v) => set('clear_session_token', v)}
            />
            {s.session_token_unreadable && (
              <p className="text-xs text-red-600" data-testid="session-token-unreadable">
                {t('assistant.settings.sessionTokenUnreadable')}
              </p>
            )}
          </div>
        )}

        {f.auth_mode === 'default_chain' && (
          <p className="text-xs text-slate-600 dark:text-slate-400">
            {t('assistant.settings.defaultChainInfo')}
          </p>
        )}

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
            label={t('assistant.settings.proxyMode')}
            hint={t(`assistant.settings.proxyHint.${f.proxy_mode}`)}
          >
            <Select
              value={f.proxy_mode}
              onChange={(e) => set('proxy_mode', e.target.value as ConnForm['proxy_mode'])}
            >
              {PROXY_MODES.map((m) => (
                <option key={m} value={m}>
                  {t(`assistant.settings.proxy.${m}`)}
                </option>
              ))}
            </Select>
          </Field>
          {f.proxy_mode === 'custom' && (
            <Field label={t('assistant.settings.proxyUrl')} hint={<Hint err={errs.proxy_url} />}>
              <Input
                value={f.proxy_url}
                onChange={(e) => set('proxy_url', e.target.value)}
                dir="ltr"
                placeholder="http://proxy:8080"
              />
            </Field>
          )}
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
            <p className="text-xs text-amber-700 dark:text-amber-300">
              {missing.map((m) => t(`assistant.settings.missing.${m}`)).join(' · ')}
            </p>
          )}
          <ErrorBox error={test.error} />
          {testResult && <TestResultView r={testResult} />}
          <div className="flex flex-wrap items-center gap-2" data-testid="assistant-switch">
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
