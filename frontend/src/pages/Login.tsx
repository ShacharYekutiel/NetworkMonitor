import { useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate, useLocation } from 'react-router-dom';
import { endpoints } from '../api/endpoints';
import { useAuth } from '../hooks/auth';
import { Button, ErrorBox, Field, Input } from '../components/ui';
import { AuthShell } from './AuthShell';
import { useFeatures } from '../hooks/features';
import { parseLogin } from '../lib/ldap';

export function LoginPage() {
  const { t } = useTranslation();
  const { signedIn } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const { features, ldap } = useFeatures();
  const ad = features.ldap && ldap ? ldap : null;
  // [2b-3] "OTHER\\user" can never sign in: say so before the round trip.
  const parsed = ad ? parseLogin(username, ad.netbios_domain, ad.domain) : null;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await endpoints.login(username.trim(), password);
      signedIn(r.user);
      const from = (location.state as { from?: string } | null)?.from;
      navigate(from && from !== '/login' ? from : '/', { replace: true });
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthShell title={t('login.title')} subtitle={t('login.subtitle')}>
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
          <p className="-mt-2 text-xs text-slate-500" data-testid="ldap-hint">
            {t('login.adHint', { netbios: ad.netbios_domain, domain: ad.domain })}
          </p>
        )}
        {parsed?.kind === 'foreign' && (
          <p className="-mt-2 text-xs text-amber-700 dark:text-amber-300">
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
        <Button type="submit" variant="primary" className="w-full" busy={busy}>
          {t('login.submit')}
        </Button>
      </form>
    </AuthShell>
  );
}
