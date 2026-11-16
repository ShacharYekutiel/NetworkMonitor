import { useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { isApiError } from '../../api/client';
import { endpoints4, qk4 } from '../../api/endpoints4';
import type { AwsCertInfo, AwsConnection } from '../../api/types';
import { certTone, daysUntil } from '../../lib/assistant';
import {
  certreqCommand,
  cnSourceIdentitySafe,
  looksLikeDisplayName,
  looksLikePrivateKey,
} from '../../lib/aws';
import { formatDateTime } from '../../lib/format';
import { CopyIcon } from '../icons';
import { cardAnchor } from '../../lib/settingsIndex';
import { useToast } from '../toast';
import {
  Button,
  buttonClass,
  ConfirmDialog,
  cx,
  ErrorBox,
  Field,
  Input,
  Select,
  Textarea,
} from '../ui';

const ISSUED_ACCEPT = '.cer,.crt,.pem,.der,.p7b,.p7c';
const UPLOAD_ACCEPT = '.pem,.crt,.cer,.der,.p7b,.p7c,.pfx,.p12';

function useCopy() {
  const { t } = useTranslation();
  const toast = useToast();
  return (text: string) => {
    void navigator.clipboard?.writeText(text);
    toast.push('success', t('assistant.copied'));
  };
}

/** Subject CN, issuer CN and the resulting source identity — what the trust policies compare. */
function IdentityBox({ c }: { c: AwsCertInfo }) {
  const { t } = useTranslation();
  const copy = useCopy();
  const rows: [string, string | null, string][] = [
    ['subjectCn', c.subject_cn, 'cert-cn'],
    ['issuerCn', c.issuer_cn, 'cert-issuer-cn'],
    ['sourceIdentity', c.source_identity, 'cert-source-identity'],
  ];
  return (
    <div
      className="space-y-1.5 rounded-md bg-sky-50 p-2 ring-1 ring-inset ring-sky-200 dark:bg-sky-950/40 dark:ring-sky-900"
      data-testid="cert-identity"
    >
      <dl className="grid grid-cols-[auto_1fr_auto] items-center gap-x-3 gap-y-1">
        {rows.map(([k, v, testId]) => (
          <div key={k} className="contents">
            <dt className="text-xs font-medium text-slate-600 dark:text-slate-300">
              {t(`aws.cert.${k}`)}
            </dt>
            <dd
              dir="ltr"
              className="break-all text-start font-mono text-sm font-semibold"
              data-testid={testId}
            >
              {v ?? '—'}
            </dd>
            <dd>
              {v && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => copy(v)}
                  aria-label={`${t('assistant.copy')} ${t(`aws.cert.${k}`)}`}
                >
                  <CopyIcon size={12} />
                </Button>
              )}
            </dd>
          </div>
        ))}
      </dl>
      <p className="text-[11px] text-slate-600 dark:text-slate-400">
        {t('aws.cert.identityIntro')}
      </p>
    </div>
  );
}

function CertDetails({ c, onDelete }: { c: AwsCertInfo; onDelete?: () => void }) {
  const { t } = useTranslation();
  const days = daysUntil(c.not_after);
  const tone = certTone(days);
  return (
    <div className="space-y-2 text-xs" data-testid="cert-info">
      <IdentityBox c={c} />
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
        <dt className="text-slate-500">{t('aws.cert.subject')}</dt>
        <dd dir="ltr" className="break-all text-start">
          {c.subject}
        </dd>
        <dt className="text-slate-500">{t('aws.cert.issuer')}</dt>
        <dd dir="ltr" className="break-all text-start">
          {c.issuer}
        </dd>
        <dt className="text-slate-500">{t('aws.cert.serial')}</dt>
        <dd dir="ltr" className="break-all text-start font-mono">
          {c.serial_hex}
        </dd>
        <dt className="text-slate-500">{t('aws.cert.validity')}</dt>
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
              ? t('aws.cert.expired')
              : t('aws.cert.daysLeft', { count: days ?? 0 })}
            )
          </span>
        </dd>
        <dt className="text-slate-500">{t('aws.cert.key')}</dt>
        <dd>
          {c.key_type.toUpperCase()} {c.key_bits}
          {c.key_unreadable && (
            <span className="ms-2 text-red-600">{t('aws.cert.keyUnreadable')}</span>
          )}
        </dd>
        {c.chain.length > 0 && (
          <>
            <dt className="text-slate-500">{t('aws.cert.chain')}</dt>
            <dd>
              {c.chain.map((x, i) => (
                <span key={i} className="block break-all text-start" dir="ltr">
                  {x.subject} ({formatDateTime(x.not_after)})
                </span>
              ))}
            </dd>
          </>
        )}
        <dt className="text-slate-500">{t('aws.cert.source')}</dt>
        <dd>
          {c.source === 'files'
            ? t('aws.cert.fromFiles')
            : t('aws.cert.uploaded', { time: formatDateTime(c.uploaded_at) })}
        </dd>
      </dl>
      {(c.warnings ?? []).map((w) => (
        <p key={w} className="text-amber-700 dark:text-amber-300" data-warning={w}>
          {t(`aws.warning.${w}`, { defaultValue: w })}
        </p>
      ))}
      {onDelete && c.source === 'uploaded' && (
        <Button size="sm" variant="ghost" onClick={onDelete}>
          {t('aws.cert.delete')}
        </Button>
      )}
    </div>
  );
}

/** Certificate errors in words; `key_mismatch` in CSR mode names the request and the certificate. */
function CertError({ error }: { error: unknown }) {
  const { t } = useTranslation();
  if (!error) return null;
  if (
    !isApiError(error) ||
    !['invalid_certificate', 'invalid_private_key', 'key_mismatch'].includes(error.code)
  )
    return <ErrorBox error={error} />;
  const d = (error.details?.[0] ?? {}) as Record<string, unknown>;
  const mismatch = error.code === 'key_mismatch' && typeof d.csr_subject === 'string';
  return (
    <div
      role="alert"
      className="space-y-1 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 ring-1 ring-inset ring-red-200 dark:bg-red-950/40 dark:text-red-300 dark:ring-red-900"
      data-testid="cert-error"
      data-code={error.code}
    >
      <p>
        {mismatch
          ? t('aws.cert.keyMismatch', {
              csr: `⁨${String(d.csr_subject)}⁩`,
              time: formatDateTime(String(d.csr_created_at ?? '')),
              cert: `⁨${String(d.cert_subject ?? '?')}⁩`,
            })
          : t(`aws.cert.err.${error.code}`)}
      </p>
      {!mismatch && error.message && (
        <p dir="ltr" className="text-start font-mono text-[11px]">
          {error.message}
        </p>
      )}
    </div>
  );
}

/** The AD CS command with the template *name* (the owner's third mistake: the display name). */
function CertreqPanel() {
  const { t } = useTranslation();
  const copy = useCopy();
  const [template, setTemplate] = useState('');
  const cmd = certreqCommand(template);
  return (
    <div
      className="space-y-1.5 rounded-md bg-slate-50 p-2 dark:bg-slate-800/60"
      data-testid="certreq"
    >
      <p className="text-xs font-medium">{t('aws.cert.certreqTitle')}</p>
      <Field
        label={t('aws.cert.templateName')}
        hint={
          looksLikeDisplayName(template) ? (
            <span className="text-amber-700 dark:text-amber-300" data-testid="template-spaces">
              {t('aws.cert.templateSpaces')}
            </span>
          ) : undefined
        }
      >
        <Input
          value={template}
          onChange={(e) => setTemplate(e.target.value)}
          dir="ltr"
          placeholder="NetMonitorClientAuth"
          data-testid="template-name"
        />
      </Field>
      <div className="flex items-start gap-2">
        <code
          dir="ltr"
          className="block flex-1 break-all rounded bg-white px-2 py-1 text-start font-mono text-[11px] ring-1 ring-inset ring-slate-200 dark:bg-slate-950 dark:ring-slate-700"
          data-testid="certreq-command"
        >
          {cmd}
        </code>
        <Button size="sm" onClick={() => copy(cmd)} aria-label={t('assistant.copy')}>
          <CopyIcon size={12} />
        </Button>
      </div>
      <p className="text-[11px] text-slate-600 dark:text-slate-400">{t('aws.cert.templateHint')}</p>
    </div>
  );
}

/**
 * [4] Roles Anywhere certificate of one AWS connection (moved from the assistant's 3b
 * `CertificateCard`; architecture § 15.1.10): *Upload certificate and key* or *Generate a CSR*.
 * While a CSR is pending the only path is one file field for the issued certificate (the key
 * stays on the server); *This certificate has its own key* reveals the key fields.
 */
export function RolesAnywhereCertificateCard({
  c,
  connections,
  locked,
}: {
  c: AwsConnection;
  connections: AwsConnection[];
  /** an ON consumer uses this connection: the certificate cannot be deleted */
  locked: boolean;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const pending = c.csr?.pem ? c.csr : null;
  const [mode, setMode] = useState<'upload' | 'csr'>(pending ? 'csr' : 'upload');
  const [ownKey, setOwnKey] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [fileIsKey, setFileIsKey] = useState(false);
  const [key, setKey] = useState<File | null>(null);
  const [password, setPassword] = useState('');
  const [cn, setCn] = useState('netmonitor.example.net');
  const [keyType, setKeyType] = useState<'rsa' | 'ec'>('rsa');
  const [copyFrom, setCopyFrom] = useState('');
  const [deleting, setDeleting] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const keyRef = useRef<HTMLInputElement>(null);
  const csrMode = !!pending && !ownKey;
  const isPfx = !!file && /\.(pfx|p12)$/i.test(file.name);
  const refresh = () => void qc.invalidateQueries({ queryKey: qk4.connections });
  const reset = () => {
    setFile(null);
    setFileIsKey(false);
    setKey(null);
    setPassword('');
    if (fileRef.current) fileRef.current.value = '';
    if (keyRef.current) keyRef.current.value = '';
  };
  const upload = useMutation({
    mutationFn: () =>
      endpoints4.uploadCert(c.id, file!, csrMode ? null : key, csrMode ? '' : password),
    onSuccess: () => {
      toast.push('success', t('aws.cert.uploadedOk'));
      reset();
      setOwnKey(false);
      refresh();
    },
  });
  const makeCsr = useMutation({
    mutationFn: () => endpoints4.createCsr(c.id, { common_name: cn.trim(), key_type: keyType }),
    onSuccess: refresh,
  });
  const discardCsr = useMutation({
    mutationFn: () => endpoints4.deleteCsr(c.id),
    onSuccess: refresh,
  });
  const del = useMutation({
    mutationFn: () => endpoints4.deleteCert(c.id),
    onSuccess: () => {
      setDeleting(false);
      refresh();
    },
  });
  const copyCert = useMutation({
    mutationFn: () => endpoints4.copyCert(c.id, Number(copyFrom)),
    onSuccess: () => {
      toast.push('success', t('aws.cert.copiedOk'));
      setCopyFrom('');
      refresh();
    },
  });
  const onFile = (f: File | null) => {
    setFile(f);
    setFileIsKey(false);
    upload.reset();
    if (!f) return;
    if (looksLikePrivateKey(f.name)) {
      setFileIsKey(true);
      return;
    }
    // a key saved as .pem / .cer is recognised by its content
    void f
      .slice(0, 256)
      .text()
      .then((head) => setFileIsKey(looksLikePrivateKey(f.name, head)))
      .catch(() => undefined);
  };
  const fromFiles = c.certificate?.source === 'files' || c.certificate_host_files;
  const sources = connections.filter((x) => x.id !== c.id && x.certificate?.source === 'uploaded');
  const keyRefused = csrMode && fileIsKey;
  const canUpload = !!file && !keyRefused && (csrMode || isPfx || !!key);

  return (
    <div
      className="space-y-3 rounded-md border border-slate-200 p-3 dark:border-slate-700"
      data-anchor={cardAnchor('awsCertificate')}
      data-testid="cert-card"
      data-mode={csrMode ? 'csr-pending' : mode}
    >
      <h4 className="text-sm font-semibold">{t('aws.cert.title')}</h4>
      {c.certificate ? (
        <CertDetails
          c={c.certificate}
          onDelete={locked || fromFiles ? undefined : () => setDeleting(true)}
        />
      ) : (
        <p className="text-xs text-amber-700 dark:text-amber-300">{t('aws.cert.none')}</p>
      )}
      {fromFiles ? (
        <p className="text-xs text-slate-500">{t('aws.cert.filesReadOnly')}</p>
      ) : (
        <>
          {!pending && (
            <div role="radiogroup" className="flex flex-wrap gap-4 text-sm">
              {(['upload', 'csr'] as const).map((m) => (
                <label key={m} className="inline-flex items-center gap-1.5">
                  <input
                    type="radio"
                    name={`cert-mode-${c.id}`}
                    checked={mode === m}
                    onChange={() => setMode(m)}
                  />
                  {t(`aws.cert.mode.${m}`)}
                </label>
              ))}
            </div>
          )}

          {mode === 'csr' && !pending && (
            <div className="space-y-2">
              <p className="text-xs text-slate-500">{t('aws.cert.csrIntro')}</p>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                <Field
                  label={t('aws.cert.cn')}
                  className="sm:col-span-2"
                  hint={
                    cnSourceIdentitySafe(cn) ? (
                      t('aws.cert.cnHint')
                    ) : (
                      <span className="text-amber-700 dark:text-amber-300" data-testid="cn-unsafe">
                        {t('aws.cert.cnUnsafe')}
                      </span>
                    )
                  }
                >
                  <Input
                    value={cn}
                    onChange={(e) => setCn(e.target.value)}
                    dir="ltr"
                    maxLength={64}
                    data-testid="csr-cn"
                  />
                </Field>
                <Field label={t('aws.cert.keyType')}>
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
                {t('aws.cert.generateCsr')}
              </Button>
              <ErrorBox error={makeCsr.error} />
            </div>
          )}

          {pending && (
            <div className="space-y-2" data-testid="csr-pending">
              <p className="text-xs text-slate-500">{t('aws.cert.csrIntro')}</p>
              <p className="text-xs">
                {t('aws.cert.csrPending', {
                  subject: `⁨${pending.subject ?? ''}⁩`,
                  time: formatDateTime(pending.created_at),
                })}
              </p>
              {pending.key_unreadable && (
                <p className="text-xs text-red-600">{t('aws.cert.keyUnreadable')}</p>
              )}
              <Textarea
                readOnly
                value={pending.pem ?? ''}
                rows={5}
                dir="ltr"
                className="font-mono text-[11px]"
                data-testid="csr-pem"
              />
              <div className="flex flex-wrap gap-2">
                <a
                  className={buttonClass('secondary', 'sm')}
                  href={endpoints4.csrDownloadUrl(c.id)}
                  download
                >
                  {t('aws.cert.downloadCsr')}
                </a>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => discardCsr.mutate()}
                  busy={discardCsr.isPending}
                >
                  {t('aws.cert.discardCsr')}
                </Button>
              </div>
              <ErrorBox error={discardCsr.error} />
              <CertreqPanel />
            </div>
          )}

          {(mode === 'upload' || pending) && (
            <div className="space-y-2" data-testid="cert-upload-form">
              {csrMode && <p className="text-xs font-medium">{t('aws.cert.uploadIssued')}</p>}
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                <Field
                  label={csrMode ? t('aws.cert.issuedFile') : t('aws.cert.file')}
                  className={csrMode ? 'sm:col-span-3' : undefined}
                  hint={
                    keyRefused ? (
                      <span className="text-red-600" data-testid="not-a-key">
                        {t('aws.cert.notAKey')}
                      </span>
                    ) : csrMode ? (
                      t('aws.cert.keyStays')
                    ) : (
                      t('aws.cert.fileHint')
                    )
                  }
                >
                  <input
                    ref={fileRef}
                    type="file"
                    accept={csrMode ? ISSUED_ACCEPT : UPLOAD_ACCEPT}
                    className="block w-full text-xs"
                    onChange={(e) => onFile(e.target.files?.[0] ?? null)}
                    data-testid="cert-file"
                  />
                </Field>
                {!csrMode && !isPfx && (
                  <Field label={t('aws.cert.keyFile')} hint={t('aws.cert.keyHint')}>
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
                {!csrMode && (
                  <Field label={t('aws.cert.password')} hint={t('aws.cert.passwordHint')}>
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
              <div className="flex flex-wrap items-center gap-3">
                <Button
                  size="sm"
                  onClick={() => upload.mutate()}
                  busy={upload.isPending}
                  disabled={!canUpload}
                  data-testid="cert-upload"
                >
                  {csrMode ? t('aws.cert.uploadIssued') : t('aws.cert.upload')}
                </Button>
                {pending && (
                  <button
                    type="button"
                    className="text-xs text-sky-700 hover:underline dark:text-sky-400"
                    onClick={() => {
                      setOwnKey((v) => !v);
                      reset();
                    }}
                    data-testid="own-key"
                  >
                    {ownKey ? t('aws.cert.csrKey') : t('aws.cert.ownKey')}
                  </button>
                )}
              </div>
              <CertError error={upload.error} />
            </div>
          )}

          {sources.length > 0 && (
            <div className="flex flex-wrap items-end gap-2" data-testid="cert-copy">
              <Field label={t('aws.cert.copyFrom')}>
                <Select value={copyFrom} onChange={(e) => setCopyFrom(e.target.value)}>
                  <option value="">{t('aws.cert.copyPick')}</option>
                  {sources.map((x) => (
                    <option key={x.id} value={String(x.id)}>
                      {x.name}
                    </option>
                  ))}
                </Select>
              </Field>
              <Button
                size="sm"
                onClick={() => copyCert.mutate()}
                busy={copyCert.isPending}
                disabled={!copyFrom}
              >
                {t('aws.cert.copy')}
              </Button>
              <ErrorBox error={copyCert.error} className="basis-full" />
            </div>
          )}
        </>
      )}
      <p className="text-xs text-slate-500">{t('aws.cert.requirements')}</p>
      <ConfirmDialog
        open={deleting}
        title={t('aws.cert.delete')}
        message={t('aws.cert.deleteConfirm')}
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
