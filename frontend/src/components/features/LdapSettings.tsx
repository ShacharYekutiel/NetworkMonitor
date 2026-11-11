import { useEffect, useState, type FormEvent } from 'react';
import { useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints } from '../../api/endpoints';
import type {
  LdapSettingsInput,
  LdapTestResult,
  LdapUserTestResult,
  Settings,
} from '../../api/types';
import { formatDateTime } from '../../lib/format';
import {
  baseDnFromDomain,
  ldapEnableState,
  ldapForm,
  ldapKey,
  ldapFormErrors,
  ldapInput,
  type LdapForm,
} from '../../lib/ldap';
import { CheckIcon, XIcon } from '../icons';
import { SecretField } from '../SnmpEditor';
import {
  Button,
  Card,
  Checkbox,
  ErrorBox,
  Field,
  Input,
  Notice,
  Select,
  Table,
  Textarea,
  td,
  th,
} from '../ui';
import { useSaveSettings } from './common';
import { InlineFeatureSwitch } from './FeatureToggle';

function Ok({ ok }: { ok: boolean }) {
  const { t } = useTranslation();
  return ok ? (
    <CheckIcon size={14} className="text-status-up" aria-label={t('ldap.ok')} />
  ) : (
    <XIcon size={14} className="text-status-down" aria-label={t('ldap.failed')} />
  );
}

export function LdapTestView({ r }: { r: LdapTestResult }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-3" data-testid="ldap-test-result">
      <Notice tone={r.ok ? 'success' : 'info'}>
        <bdi dir="auto">{r.message}</bdi>
        {r.discovered_via_dns && <span className="ms-2 text-xs">({t('ldap.viaDns')})</span>}
      </Notice>
      <Table>
        <thead>
          <tr>
            <th className={th}>{t('ldap.server')}</th>
            <th className={th}>{t('ldap.result')}</th>
            <th className={th}>{t('ldap.latency')}</th>
            <th className={th}>{t('ldap.certificate')}</th>
            <th className={th}>AD</th>
          </tr>
        </thead>
        <tbody>
          {r.servers.map((s) => (
            <tr key={s.server}>
              <td className={td}>
                <bdi dir="ltr">{s.server}</bdi>
                <span className="ms-1 text-xs text-ink-muted">({s.security})</span>
              </td>
              <td className={td}>
                <span className="inline-flex items-center gap-1">
                  <Ok ok={s.ok} />
                  {s.error && (
                    <bdi dir="ltr" className="text-xs text-status-down">
                      {s.error}
                    </bdi>
                  )}
                </span>
              </td>
              <td className={`${td} tabular-nums`}>
                {s.latency_ms === null ? '—' : `${s.latency_ms} ms`}
              </td>
              <td className={`${td} text-xs`}>
                {s.tls ? (
                  <div dir="ltr" className="text-left">
                    <div>{s.tls.subject}</div>
                    <div className="text-ink-muted">
                      {s.tls.issuer} · {formatDateTime(s.tls.not_after)}
                    </div>
                    <div className={s.tls.verified ? 'text-status-up' : 'text-status-down'}>
                      {s.tls.verified ? t('ldap.verified') : t('ldap.notVerified')}
                    </div>
                  </div>
                ) : (
                  '—'
                )}
              </td>
              <td className={td}>{s.ok ? <Ok ok={s.active_directory} /> : '—'}</td>
            </tr>
          ))}
        </tbody>
      </Table>
      <ul className="space-y-1 text-sm">
        <li className="flex items-center gap-2">
          <Ok ok={r.service_bind.ok} /> {t('ldap.serviceBind')}
          {r.service_bind.error && (
            <bdi dir="ltr" className="text-xs text-status-down">
              {r.service_bind.error}
            </bdi>
          )}
        </li>
        <li className="flex items-center gap-2">
          <Ok ok={r.base_dn.ok} /> {t('ldap.baseDn')}
          {r.base_dn.error && (
            <bdi dir="ltr" className="text-xs text-status-down">
              {r.base_dn.error}
            </bdi>
          )}
        </li>
        {r.admin_group && (
          <li className="flex items-center gap-2">
            <Ok ok={r.admin_group.found} /> {t('ldap.adminGroup')}:{' '}
            <bdi dir="ltr" className="text-xs">
              {r.admin_group.dn}
            </bdi>
          </li>
        )}
        {r.viewer_group && (
          <li className="flex items-center gap-2">
            <Ok ok={r.viewer_group.found} /> {t('ldap.viewerGroup')}:{' '}
            <bdi dir="ltr" className="text-xs">
              {r.viewer_group.dn}
            </bdi>
          </li>
        )}
      </ul>
    </div>
  );
}

export function LdapUserTestView({ r }: { r: LdapUserTestResult }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-3" data-testid="ldap-user-test-result">
      <Notice tone={r.ok ? 'success' : 'info'}>
        {r.ok
          ? t('ldap.userOk', { role: r.role ? t(`role.${r.role}`) : '—' })
          : t('ldap.userFailed', {
              reason: r.reason ? t(`ldap.reason.${r.reason}`, { defaultValue: r.reason }) : '—',
            })}
      </Notice>
      <ol className="space-y-1 text-sm">
        {r.steps.map((s, i) => (
          <li key={i} className="flex flex-wrap items-center gap-2">
            <Ok ok={s.ok} />
            <span className="font-medium">{t(`ldap.step.${s.step}`)}</span>
            {s.server && (
              <bdi dir="ltr" className="text-xs text-ink-muted">
                {s.server}
              </bdi>
            )}
            {s.detail && (
              <bdi dir="auto" className="text-xs text-ink-muted">
                {s.detail}
              </bdi>
            )}
          </li>
        ))}
      </ol>
      {r.user && (
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
          <dt className="text-ink-muted">{t('ldap.user.dn')}</dt>
          <dd>
            <bdi dir="ltr" className="break-all text-xs">
              {r.user.dn}
            </bdi>
          </dd>
          <dt className="text-ink-muted">sAMAccountName</dt>
          <dd>
            <bdi dir="ltr">{r.user.sam_account_name}</bdi>
          </dd>
          <dt className="text-ink-muted">UPN</dt>
          <dd>
            <bdi dir="ltr">{r.user.user_principal_name ?? '—'}</bdi>
          </dd>
          <dt className="text-ink-muted">{t('ldap.user.displayName')}</dt>
          <dd>
            <bdi dir="auto">{r.user.display_name ?? '—'}</bdi>
          </dd>
          <dt className="text-ink-muted">{t('users.email')}</dt>
          <dd>
            <bdi dir="ltr">{r.user.mail ?? '—'}</bdi>
          </dd>
          <dt className="text-ink-muted">{t('ldap.user.state')}</dt>
          <dd>
            {r.user.disabled
              ? t('ldap.user.disabled')
              : r.user.locked
                ? t('ldap.user.locked')
                : t('ldap.user.active')}
          </dd>
        </dl>
      )}
    </div>
  );
}

/** [2b-3] Settings → Active Directory: connection, service account (write-only password), groups, tests. */
export function LdapCard({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const stored = settings.ldap;
  const [f, setF] = useState<LdapForm>(() => ldapForm(stored));
  const [testUser, setTestUser] = useState('');
  const [testPw, setTestPw] = useState('');
  useEffect(() => setF(ldapForm(stored)), [stored]);
  const set = (p: Partial<LdapForm>) => setF((x) => ({ ...x, ...p }));
  const errs = ldapFormErrors(f);
  const invalid = Object.keys(errs).length > 0;
  const save = useSaveSettings();
  // configure-first: the switch unlocks after a successful test of the stored settings
  const [verified, setVerified] = useState<string | null>(null);
  const [testedFull, setTestedFull] = useState<string | null>(null);
  const test = useMutation({
    mutationFn: (input: LdapSettingsInput) => endpoints.ldapTest(input),
    onSuccess: (r, input) => {
      setTestedFull(r.ok ? JSON.stringify(input) : null);
      setVerified(r.ok ? ldapKey(input) : null);
    },
    onError: () => setVerified(null),
  });
  const enable = ldapEnableState({
    on: settings.feature_ldap_enabled === true,
    form: f,
    stored,
    verifiedKey: verified,
  });
  const testU = useMutation({
    mutationFn: () => endpoints.ldapTestUser(testUser.trim(), testPw, ldapInput(f)),
    onSettled: () => setTestPw(''),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!invalid) {
      const input = ldapInput(f);
      save.mutate(
        { ldap: input },
        {
          onSuccess: (r) => {
            set({ password: '', clearPassword: false });
            // the saved values are exactly the tested ones -> the stored settings passed the test
            const savedKey = ldapKey(ldapInput(ldapForm(r.ldap)));
            setVerified(testedFull === JSON.stringify(input) ? savedKey : null);
          },
        },
      );
    }
  };
  const hint = (k: keyof LdapForm, ok?: string) =>
    errs[k] ? <span className="text-status-down">{t(errs[k] as string)}</span> : ok;
  const domainHint = f.domain.trim()
    ? `${t('ldap.baseDnDefault')}: ${baseDnFromDomain(f.domain)}`
    : undefined;
  return (
    <div className="space-y-6">
      <Card title={t('ldap.title')}>
        <form onSubmit={submit} className="space-y-4" data-testid="ldap-settings">
          <p className="text-xs text-ink-muted">{t('ldap.intro')}</p>
          {stored?.bind_password_unreadable && (
            <div className="rounded-md bg-status-down-soft px-3 py-2 text-sm text-status-down">
              {t('ldap.passwordUnreadable')}
            </div>
          )}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Field label={t('ldap.domain')} hint={t('ldap.domainHint')}>
              <Input
                value={f.domain}
                onChange={(e) => set({ domain: e.target.value })}
                dir="ltr"
                placeholder="mnidom.net"
              />
            </Field>
            <Field label={t('ldap.netbios')} hint={t('ldap.netbiosHint')}>
              <Input
                value={f.netbios_domain}
                onChange={(e) => set({ netbios_domain: e.target.value })}
                dir="ltr"
                placeholder="MNIDOM"
              />
            </Field>
            <Field label={t('ldap.timeout')} hint={hint('timeout_s')}>
              <Input
                type="number"
                min={1}
                max={30}
                value={f.timeout_s}
                onChange={(e) => set({ timeout_s: e.target.value })}
                dir="ltr"
              />
            </Field>
            <Field
              label={t('ldap.servers')}
              hint={hint('servers', t('ldap.serversHint'))}
              className="sm:col-span-2"
            >
              <Textarea
                rows={2}
                value={f.servers}
                onChange={(e) => set({ servers: e.target.value })}
                dir="ltr"
                className="font-mono"
                placeholder="dc1.mnidom.net&#10;dc2.mnidom.net:636"
              />
            </Field>
            <div className="space-y-2">
              <Field label={t('ldap.security')} hint={t(`ldap.securityHint.${f.security}`)}>
                <Select
                  value={f.security}
                  onChange={(e) => set({ security: e.target.value as LdapForm['security'] })}
                >
                  <option value="ldaps">LDAPS (636)</option>
                  <option value="starttls">StartTLS (389)</option>
                </Select>
              </Field>
              <Checkbox
                label={t('ldap.tlsVerify')}
                checked={f.tls_verify}
                onChange={(v) => set({ tls_verify: v })}
              />
            </div>
            <Field
              label={t('ldap.baseDn')}
              hint={hint('base_dn', domainHint)}
              className="sm:col-span-3"
            >
              <Input
                value={f.base_dn}
                onChange={(e) => set({ base_dn: e.target.value })}
                dir="ltr"
                placeholder="DC=mnidom,DC=net"
              />
            </Field>
            <Field label={t('ldap.bindDn')} hint={t('ldap.bindDnHint')} className="sm:col-span-2">
              <Input
                value={f.bind_dn}
                onChange={(e) => set({ bind_dn: e.target.value })}
                dir="ltr"
                autoComplete="off"
                placeholder="svc-netmon@mnidom.net"
              />
            </Field>
            <SecretField
              label={t('ldap.bindPassword')}
              value={f.password}
              isSet={!!stored?.bind_password_set}
              clear={f.clearPassword}
              onValue={(v) => set({ password: v })}
              onClear={(v) => set({ clearPassword: v })}
              emptyHint={t('ldap.bindPasswordHint')}
            />
            <Field
              label={t('ldap.adminGroup')}
              hint={hint('admin_group_dn')}
              className="sm:col-span-3"
            >
              <Input
                value={f.admin_group_dn}
                onChange={(e) => set({ admin_group_dn: e.target.value })}
                dir="ltr"
                placeholder="CN=NetMon-Admins,OU=Groups,DC=mnidom,DC=net"
              />
            </Field>
            <Field
              label={t('ldap.viewerGroup')}
              hint={hint('viewer_group_dn', t('ldap.groupsHint'))}
              className="sm:col-span-3"
            >
              <Input
                value={f.viewer_group_dn}
                onChange={(e) => set({ viewer_group_dn: e.target.value })}
                dir="ltr"
                placeholder="CN=NetMon-Viewers,OU=Groups,DC=mnidom,DC=net"
              />
            </Field>
          </div>
          {!f.tls_verify && <Notice>{t('ldap.tlsVerifyOff')}</Notice>}
          <ErrorBox error={save.error} />
          <div className="flex flex-wrap items-start gap-x-4 gap-y-2">
            <Button
              busy={test.isPending}
              disabled={invalid}
              onClick={() => test.mutate(ldapInput(f))}
            >
              {t('ldap.testConnection')}
            </Button>
            <div className="me-auto pt-1">
              <InlineFeatureSwitch
                name="ldap"
                settings={settings}
                blocked={enable !== 'ok'}
                hint={
                  enable === 'save' ? t('features.enableNeedsSave') : t('features.enableNeedsTest')
                }
              />
            </div>
            <Button type="submit" variant="primary" busy={save.isPending} disabled={invalid}>
              {t('common.save')}
            </Button>
          </div>
          <p className="text-xs text-ink-muted">{t('ldap.testHint')}</p>
          <ErrorBox error={test.error} />
          {test.data && <LdapTestView r={test.data} />}
        </form>
      </Card>
      <Card title={t('ldap.testUserTitle')}>
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (testUser.trim() && testPw) testU.mutate();
          }}
        >
          <p className="text-xs text-ink-muted">{t('ldap.testUserHint')}</p>
          <div className="flex flex-wrap items-end gap-2">
            <Field label={t('login.username')} className="w-56">
              <Input
                value={testUser}
                onChange={(e) => setTestUser(e.target.value)}
                dir="ltr"
                autoComplete="off"
                placeholder="MNIDOM\jdoe"
              />
            </Field>
            <Field label={t('login.password')} className="w-56">
              <Input
                type="password"
                value={testPw}
                onChange={(e) => setTestPw(e.target.value)}
                dir="ltr"
                autoComplete="new-password"
              />
            </Field>
            <Button
              type="submit"
              busy={testU.isPending}
              disabled={!testUser.trim() || !testPw || invalid}
            >
              {t('ldap.testUser')}
            </Button>
          </div>
          <ErrorBox error={testU.error} />
          {testU.data && <LdapUserTestView r={testU.data} />}
        </form>
      </Card>
    </div>
  );
}

/** Configure-first: the form is always shown; the switch is at its bottom, next to Test connection. */
export function LdapTab({ settings }: { settings: Settings }) {
  return <LdapCard settings={settings} />;
}
