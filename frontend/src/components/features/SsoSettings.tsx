import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints3, qk3 } from '../../api/endpoints3';
import type { Settings } from '../../api/types';
import type {
  KerberosSettings,
  OidcRoleSource,
  OidcSettings,
  OidcTestResult,
} from '../../api/types3';
import { formatDateTime } from '../../lib/format';
import {
  kerberosEnableState,
  oidcEnableState,
  oidcMissing,
  parseRealms,
  parseValues,
  realmsError,
} from '../../lib/sso';
import { SecretField } from '../SnmpEditor';
import { useToast } from '../toast';
import {
  Button,
  Card,
  Checkbox,
  cx,
  ErrorBox,
  Field,
  Input,
  Loading,
  Notice,
  Select,
  Table,
  Textarea,
  td,
  th,
} from '../ui';
import { InlineFeatureSwitch } from './FeatureToggle';

function StateBadge({ k }: { k: KerberosSettings }) {
  const { t } = useTranslation();
  return (
    <span
      className={cx(
        'rounded px-1.5 py-0.5 text-xs font-medium',
        k.state === 'ready' &&
          'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-200',
        k.state === 'error' && 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-200',
        (k.state === 'disabled' || k.state === 'unavailable') &&
          'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
      )}
    >
      {t(`kerberos.state.${k.state}`)}
    </span>
  );
}

function KerberosCard({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({ queryKey: qk3.kerberos, queryFn: endpoints3.kerberos });
  const [realms, setRealms] = useState('');
  const [auto, setAuto] = useState(true);
  const fileRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!q.data) return;
    setRealms(q.data.realms.join(', '));
    setAuto(q.data.auto_login);
  }, [q.data]);
  const set = (k: KerberosSettings) => qc.setQueryData(qk3.kerberos, k);
  const save = useMutation({
    mutationFn: () => endpoints3.updateKerberos({ realms: parseRealms(realms), auto_login: auto }),
    onSuccess: (k) => {
      set(k);
      void qc.invalidateQueries({ queryKey: ['publicSettings'] });
      toast.push('success', t('common.saved'));
    },
  });
  const upload = useMutation({
    mutationFn: (f: File) => endpoints3.uploadKeytab(f),
    onSuccess: (k) => {
      set(k);
      toast.push('success', t('kerberos.uploaded'));
      if (fileRef.current) fileRef.current.value = '';
    },
  });
  const del = useMutation({
    mutationFn: () => endpoints3.deleteKeytab(),
    onSuccess: (k) => {
      if (k) set(k);
      else void qc.invalidateQueries({ queryKey: qk3.kerberos });
      toast.push('success', t('common.deleted'));
    },
  });
  const realmErr = realmsError(parseRealms(realms));
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!realmErr) save.mutate();
  };
  const k = q.data;
  const kGate = kerberosEnableState({
    on: settings.feature_sso_kerberos_enabled === true,
    keytabEntries: k?.keytab_set ? k.keytab_entries.length : 0,
    ldapOn: settings.feature_ldap_enabled === true,
  });
  return (
    <Card title={t('kerberos.title')} actions={k && <StateBadge k={k} />}>
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} />
      {k && (
        <div className="space-y-4">
          <p className="text-sm text-slate-600 dark:text-slate-300">{t('kerberos.intro')}</p>
          {!k.platform_supported && <Notice>{t('kerberos.unsupported')}</Notice>}
          {k.error && (
            <p className="text-sm text-red-600" dir="auto">
              {k.error}
            </p>
          )}
          <form onSubmit={submit} className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field
              label={t('kerberos.realms')}
              hint={
                realmErr ? (
                  <span className="text-red-600">{t(realmErr)}</span>
                ) : (
                  t('kerberos.realmsHint')
                )
              }
            >
              <Input value={realms} onChange={(e) => setRealms(e.target.value)} dir="ltr" />
            </Field>
            <div className="flex items-end pb-1.5">
              <Checkbox label={t('kerberos.autoLogin')} checked={auto} onChange={setAuto} />
            </div>
            <div className="sm:col-span-2">
              <ErrorBox error={save.error} />
            </div>
            <div className="flex justify-end sm:col-span-2">
              <Button type="submit" variant="primary" busy={save.isPending} disabled={!!realmErr}>
                {t('common.save')}
              </Button>
            </div>
          </form>
          <div className="space-y-2 border-t border-slate-200 pt-3 dark:border-slate-800">
            <h3 className="text-sm font-semibold">{t('kerberos.keytab')}</h3>
            {k.keytab_set ? (
              <>
                <p className="text-xs text-slate-500">
                  {t('kerberos.uploadedAt', { date: formatDateTime(k.keytab_uploaded_at) })}
                </p>
                <Table>
                  <thead>
                    <tr>
                      <th className={th}>{t('kerberos.principal')}</th>
                      <th className={th}>KVNO</th>
                      <th className={th}>{t('kerberos.enctype')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {k.keytab_entries.map((e, i) => (
                      <tr key={i}>
                        <td className={td}>
                          <bdi dir="ltr" className="font-mono text-xs">
                            {e.principal}
                          </bdi>
                        </td>
                        <td className={`${td} tabular-nums`}>{e.kvno}</td>
                        <td className={td}>
                          <bdi dir="ltr" className="text-xs">
                            {e.enctype_name}
                          </bdi>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </Table>
              </>
            ) : (
              <p className="text-sm text-slate-500">{t('kerberos.noKeytab')}</p>
            )}
            {k.warnings.map((w) => (
              <p key={w} className="text-xs text-amber-700 dark:text-amber-300">
                {t(`kerberos.warning.${w}`)}
              </p>
            ))}
            <div className="flex flex-wrap items-center gap-2">
              <input
                ref={fileRef}
                type="file"
                accept=".keytab,application/octet-stream"
                className="text-sm"
                aria-label={t('kerberos.upload')}
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) upload.mutate(f);
                }}
              />
              {upload.isPending && (
                <span className="text-xs text-slate-500">{t('common.loading')}</span>
              )}
              {k.keytab_set && (
                <Button
                  size="sm"
                  variant="danger"
                  busy={del.isPending}
                  onClick={() => del.mutate()}
                >
                  {t('kerberos.deleteKeytab')}
                </Button>
              )}
            </div>
            <ErrorBox error={upload.error ?? del.error} />
            <p className="text-xs text-slate-500">{t('kerberos.testHint')}</p>
          </div>
          <div className="border-t border-slate-200 pt-3 dark:border-slate-800">
            <InlineFeatureSwitch
              name="sso_kerberos"
              settings={settings}
              blocked={kGate !== 'ok'}
              hint={
                kGate === 'ldap' ? t('features.enableNeedsLdap') : t('features.enableNeedsKeytab')
              }
            />
          </div>
        </div>
      )}
    </Card>
  );
}

function OidcTestView({ r }: { r: OidcTestResult }) {
  const { t } = useTranslation();
  return (
    <Notice tone="success">
      {t('oidc.testOk', { keys: r.jwks_keys, ms: r.latency_ms })}{' '}
      <bdi dir="ltr" className="font-mono text-xs">
        {r.issuer}
      </bdi>
    </Notice>
  );
}

function OidcCard({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({ queryKey: qk3.oidc, queryFn: endpoints3.oidc });
  const [tenant, setTenant] = useState('');
  const [client, setClient] = useState('');
  const [secret, setSecret] = useState('');
  const [clearSecret, setClearSecret] = useState(false);
  const [expires, setExpires] = useState('');
  const [source, setSource] = useState<OidcRoleSource>('app_roles');
  const [admins, setAdmins] = useState('');
  const [viewers, setViewers] = useState('');
  const [label, setLabel] = useState('');
  const [result, setResult] = useState<OidcTestResult | null>(null);
  const fill = (o: OidcSettings) => {
    setTenant(o.tenant_id);
    setClient(o.client_id);
    setSecret('');
    setClearSecret(false);
    setExpires(o.client_secret_expires_at?.slice(0, 10) ?? '');
    setSource(o.role_source);
    setAdmins(o.admin_values.join('\n'));
    setViewers(o.viewer_values.join('\n'));
    setLabel(o.button_label ?? '');
  };
  useEffect(() => {
    if (q.data) fill(q.data);
  }, [q.data]);
  const save = useMutation({
    mutationFn: () =>
      endpoints3.updateOidc({
        tenant_id: tenant.trim(),
        client_id: client.trim(),
        ...(clearSecret ? { client_secret: '' } : secret ? { client_secret: secret } : {}),
        client_secret_expires_at: expires || null,
        role_source: source,
        admin_values: parseValues(admins),
        viewer_values: parseValues(viewers),
        button_label: label.trim() || null,
      }),
    onSuccess: (o) => {
      qc.setQueryData(qk3.oidc, o);
      void qc.invalidateQueries({ queryKey: ['publicSettings'] });
      toast.push('success', t('common.saved'));
      setResult(null); // the stored settings changed: test them again
    },
  });
  const test = useMutation({
    mutationFn: endpoints3.testOidc,
    onMutate: () => setResult(null),
    onSuccess: setResult,
  });
  const o = q.data;
  const dirty =
    !!o &&
    (tenant !== o.tenant_id ||
      client !== o.client_id ||
      !!secret ||
      clearSecret ||
      expires !== (o.client_secret_expires_at?.slice(0, 10) ?? '') ||
      source !== o.role_source ||
      admins !== o.admin_values.join('\n') ||
      viewers !== o.viewer_values.join('\n') ||
      label !== (o.button_label ?? ''));
  const oGate = oidcEnableState({
    on: settings.feature_sso_oidc_enabled === true,
    dirty,
    tested: !!result,
  });
  const missing = oidcMissing({
    tenant_id: tenant,
    client_id: client,
    secretSet: clearSecret ? false : !!secret || !!o?.client_secret_set,
    admin_values: parseValues(admins),
    viewer_values: parseValues(viewers),
    role_source: source,
  });
  return (
    <Card title={t('oidc.title')}>
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} />
      {o && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
          className="space-y-3"
        >
          <p className="text-sm text-slate-600 dark:text-slate-300">{t('oidc.intro')}</p>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label={t('oidc.tenant')}>
              <Input
                value={tenant}
                onChange={(e) => setTenant(e.target.value)}
                dir="ltr"
                className="font-mono"
                placeholder="00000000-0000-0000-0000-000000000000"
              />
            </Field>
            <Field label={t('oidc.client')}>
              <Input
                value={client}
                onChange={(e) => setClient(e.target.value)}
                dir="ltr"
                className="font-mono"
              />
            </Field>
            <SecretField
              label={t('oidc.secret')}
              value={secret}
              isSet={o.client_secret_set}
              clear={clearSecret}
              onValue={setSecret}
              onClear={setClearSecret}
            />
            <Field label={t('oidc.secretExpires')} hint={t('oidc.secretExpiresHint')}>
              <Input
                type="date"
                value={expires}
                onChange={(e) => setExpires(e.target.value)}
                dir="ltr"
              />
            </Field>
            <Field label={t('oidc.roleSource')}>
              <Select value={source} onChange={(e) => setSource(e.target.value as OidcRoleSource)}>
                <option value="app_roles">{t('oidc.roleSources.app_roles')}</option>
                <option value="groups">{t('oidc.roleSources.groups')}</option>
              </Select>
            </Field>
            <Field label={t('oidc.buttonLabel')} hint={t('oidc.buttonLabelHint')}>
              <Input value={label} onChange={(e) => setLabel(e.target.value)} maxLength={64} />
            </Field>
            <Field label={t('oidc.adminValues')} hint={t(`oidc.valuesHint.${source}`)}>
              <Textarea
                rows={3}
                value={admins}
                onChange={(e) => setAdmins(e.target.value)}
                dir="ltr"
                className="font-mono text-xs"
              />
            </Field>
            <Field label={t('oidc.viewerValues')} hint={t(`oidc.valuesHint.${source}`)}>
              <Textarea
                rows={3}
                value={viewers}
                onChange={(e) => setViewers(e.target.value)}
                dir="ltr"
                className="font-mono text-xs"
              />
            </Field>
            <Field
              label={t('oidc.redirectUri')}
              hint={t('oidc.redirectUriHint')}
              className="sm:col-span-2"
            >
              <Input readOnly value={o.redirect_uri} dir="ltr" className="font-mono text-xs" />
            </Field>
          </div>
          {missing.length > 0 && (
            <ul className="text-xs text-amber-700 dark:text-amber-300" data-testid="oidc-missing">
              {missing.map((m) => (
                <li key={m}>{t(m)}</li>
              ))}
            </ul>
          )}
          <ErrorBox error={save.error ?? test.error} />
          {result && <OidcTestView r={result} />}
          <div className="flex flex-wrap items-start gap-x-4 gap-y-2">
            <Button busy={test.isPending} onClick={() => test.mutate()}>
              {t('oidc.test')}
            </Button>
            <div className="me-auto pt-1">
              <InlineFeatureSwitch
                name="sso_oidc"
                settings={settings}
                blocked={oGate !== 'ok'}
                hint={
                  oGate === 'save' ? t('features.enableNeedsSave') : t('features.enableNeedsTest')
                }
              />
            </div>
            <Button type="submit" variant="primary" busy={save.isPending}>
              {t('common.save')}
            </Button>
          </div>
        </form>
      )}
    </Card>
  );
}

/**
 * [3] Settings → Single sign-on. Configure-first: both forms are always shown (prepare while OFF);
 * each switch sits at the bottom of its form and unlocks for a validated configuration.
 */
export function SsoTab({ settings }: { settings: Settings }) {
  return (
    <div className="space-y-6">
      <KerberosCard settings={settings} />
      <OidcCard settings={settings} />
    </div>
  );
}
