import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate, useLocation } from 'react-router-dom';
import { endpoints } from '../api/endpoints';
import { endpoints3 } from '../api/endpoints3';
import { parseError } from '../api/client';
import type { User } from '../api/types';
import { useAuth } from '../hooks/auth';
import { Button, buttonClass, cx, ErrorBox, Field, Input, Spinner } from '../components/ui';
import { AuthShell } from './AuthShell';
import { useFeatures } from '../hooks/features';
import { parseLogin } from '../lib/ldap';
import { readSkip, safeNext, shouldAutoKerberos, ssoErrorFromSearch, writeSkip } from '../lib/sso';

const store = () => (typeof sessionStorage === 'undefined' ? null : sessionStorage);

type KrbState = 'idle' | 'trying' | 'ntlm' | 'failed';

export function LoginPage() {
  const { t } = useTranslation();
  const { signedIn } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const { features, ldap, sso, loaded } = useFeatures();
  const ad = features.ldap && ldap ? ldap : null;
  // [2b-3] "OTHER\\user" can never sign in: say so before the round trip.
  const parsed = ad ? parseLogin(username, ad.netbios_domain, ad.domain) : null;
  const from = (location.state as { from?: string } | null)?.from;
  const ssoError = ssoErrorFromSearch(location.search);
  const [krb, setKrb] = useState<KrbState>('idle');
  const [krbMessage, setKrbMessage] = useState<string | null>(null);
  const autoTried = useRef(false);

  const done = useCallback(
    (u: User) => {
      writeSkip(store(), false);
      signedIn(u);
      navigate(from && from !== '/login' ? from : '/', { replace: true });
    },
    [signedIn, navigate, from],
  );

  /** [3] Kerberos: the browser answers the Negotiate challenge itself when the site is trusted. */
  const kerberos = useCallback(
    async (manual: boolean) => {
      setKrb('trying');
      setKrbMessage(null);
      try {
        const res = await endpoints3.kerberosSso();
        if (res.ok) {
          const body = (await res.json()) as { user: User };
          done(body.user);
          return;
        }
        const err = await parseError(res);
        if (err.code === 'sso_ntlm') {
          setKrb('ntlm');
          setKrbMessage(err.message);
        } else if (err.code === 'sso_challenge' && !manual) {
          // The browser did not offer a ticket: quietly fall back to the form.
          setKrb('idle');
        } else {
          setKrb('failed');
          setKrbMessage(null);
          if (err.code !== 'sso_challenge') setError(err);
        }
      } catch {
        setKrb(manual ? 'failed' : 'idle');
      }
    },
    [done],
  );

  useEffect(() => {
    if (!loaded || autoTried.current) return;
    if (shouldAutoKerberos({ kerberos: sso.kerberos, skip: readSkip(store()), ssoError })) {
      autoTried.current = true;
      void kerberos(false);
    }
  }, [loaded, sso.kerberos, ssoError, kerberos]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await endpoints.login(username.trim(), password);
      done(r.user);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const oidcHref = endpoints3.oidcStartUrl(safeNext(from));

  return (
    <AuthShell title={t('login.title')} subtitle={t('login.subtitle')}>
      {ssoError && (
        <div
          role="alert"
          className="mb-4 rounded-lg border-s-2 border-status-down bg-status-down-soft px-3.5 py-2.5 text-sm text-ink"
          data-testid="sso-error"
        >
          {t(`sso.error.${ssoError}`)}
        </div>
      )}
      {krb === 'trying' && (
        <p
          className="mb-4 flex items-center gap-2 text-sm text-ink-muted"
          role="status"
          data-testid="kerberos-trying"
        >
          <Spinner className="h-4 w-4" /> {t('sso.kerberosTrying')}
        </p>
      )}
      {krb === 'ntlm' && (
        <div className="mb-4 rounded-lg border-s-2 border-status-warning bg-status-warning-soft px-3.5 py-2.5 text-sm text-ink">
          <p>{t('sso.error.sso_ntlm')}</p>
          {krbMessage && (
            <p className="mt-1 text-xs" dir="auto">
              {krbMessage}
            </p>
          )}
        </div>
      )}
      {krb === 'failed' && !error && (
        <p className="mb-4 text-sm text-status-down">{t('sso.kerberosFailed')}</p>
      )}
      {(sso.kerberos || sso.oidc) && (
        <div className="mb-4 space-y-2">
          {sso.kerberos && (
            <Button
              className="h-10 w-full"
              busy={krb === 'trying'}
              onClick={() => void kerberos(true)}
              data-testid="kerberos-button"
            >
              {t('sso.kerberosButton')}
            </Button>
          )}
          {sso.oidc && (
            <a
              href={oidcHref}
              className={cx(buttonClass(), 'h-10 w-full no-underline hover:no-underline')}
              data-testid="oidc-button"
            >
              <svg width="14" height="14" viewBox="0 0 21 21" aria-hidden="true">
                <rect x="1" y="1" width="9" height="9" fill="#f25022" />
                <rect x="11" y="1" width="9" height="9" fill="#7fba00" />
                <rect x="1" y="11" width="9" height="9" fill="#00a4ef" />
                <rect x="11" y="11" width="9" height="9" fill="#ffb900" />
              </svg>
              {sso.oidc.button_label || t('sso.oidcButton')}
            </a>
          )}
          <div className="flex items-center gap-3 pt-2 text-xs text-ink-subtle">
            <span className="h-px flex-1 bg-line" />
            {t('sso.or')}
            <span className="h-px flex-1 bg-line" />
          </div>
        </div>
      )}
      <form onSubmit={submit} className="space-y-4">
        <Field label={t('login.username')}>
          <Input
            autoFocus
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
            maxLength={256}
            dir="ltr"
          />
        </Field>
        {ad && (
          <p className="-mt-2 text-xs text-ink-muted" data-testid="ldap-hint">
            {t('login.adHint', { netbios: ad.netbios_domain, domain: ad.domain })}
          </p>
        )}
        {parsed?.kind === 'foreign' && (
          <p className="-mt-2 text-xs text-status-warning">
            {t('login.foreignDomain', { domain: parsed.domain, netbios: ad?.netbios_domain ?? '' })}
          </p>
        )}
        <Field label={t('login.password')}>
          <Input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            maxLength={1024}
            dir="ltr"
          />
        </Field>
        <ErrorBox error={error} />
        <Button type="submit" variant="primary" className="h-10 w-full" busy={busy}>
          {t('login.submit')}
        </Button>
      </form>
    </AuthShell>
  );
}
