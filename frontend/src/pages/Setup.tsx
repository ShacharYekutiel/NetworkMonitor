import { useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { endpoints } from '../api/endpoints';
import { browserTimeZone } from '../lib/maintenance';
import { isApiError } from '../api/client';

type SetupBody = Parameters<typeof endpoints.setup>[0];

/**
 * POST /api/setup with the browser time zone (silently). If the server rejects the zone
 * (422 on the timezone field), retry once without it, so setup can always complete.
 */
export async function setupWithTimezone(
  body: Omit<SetupBody, 'timezone'>,
  call: (b: SetupBody) => ReturnType<typeof endpoints.setup> = endpoints.setup,
  tz: string | undefined = browserTimeZone(),
): ReturnType<typeof endpoints.setup> {
  if (!tz) return call(body);
  try {
    return await call({ ...body, timezone: tz });
  } catch (e) {
    const tzError =
      isApiError(e) &&
      e.status === 422 &&
      (e.details ?? []).some((d) => (d.loc ?? []).includes('timezone') || d.field === 'timezone');
    if (!tzError) throw e;
    return call(body);
  }
}
import type { Language } from '../api/types';
import { useAuth } from '../hooks/auth';
import { setLanguage } from '../i18n';
import { Button, ErrorBox, Field, Input, Select, cx } from '../components/ui';
import { AuthShell } from './AuthShell';

const USERNAME_RE = /^[A-Za-z0-9._-]{3,64}$/;

export function SetupPage() {
  const { t, i18n } = useTranslation();
  const { signedIn } = useAuth();
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [language, setLang] = useState<Language>(i18n.language === 'he' ? 'he' : 'en');
  const [username, setUsername] = useState('admin');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const usernameOk = USERNAME_RE.test(username);
  const pwOk = password.length >= 8 && password.length <= 128;
  const match = password === confirm;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (step === 0) {
      setStep(1);
      return;
    }
    if (!usernameOk || !pwOk || !match) return;
    setBusy(true);
    setError(null);
    try {
      const r = await setupWithTimezone({
        username,
        password,
        email: email.trim() || null,
        language,
      });
      signedIn(r.user);
      navigate('/', { replace: true });
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const steps = [t('setup.stepLanguage'), t('setup.stepAdmin')];

  return (
    <AuthShell title={t('setup.title')} subtitle={t('setup.subtitle')}>
      <ol className="mb-5 flex gap-2 text-xs">
        {steps.map((s, i) => (
          <li
            key={s}
            className={cx(
              'flex-1 rounded border-t-4 pt-1',
              i <= step
                ? 'border-sky-600 text-sky-700 dark:text-sky-400'
                : 'border-slate-200 text-slate-500 dark:border-slate-700',
            )}
          >
            {i + 1}. {s}
          </li>
        ))}
      </ol>
      <form onSubmit={submit} className="space-y-4">
        {step === 0 && (
          <>
            <Field label={t('setup.language')} hint={t('setup.languageHint')}>
              <Select
                value={language}
                onChange={(e) => {
                  const l = e.target.value as Language;
                  setLang(l);
                  setLanguage(l);
                }}
              >
                <option value="en">English</option>
                <option value="he">עברית</option>
              </Select>
            </Field>
            <Button type="submit" variant="primary" className="w-full">
              {t('common.next')}
            </Button>
          </>
        )}
        {step === 1 && (
          <>
            <Field
              label={t('setup.username')}
              hint={!usernameOk ? t('validation.username') : undefined}
              required
            >
              <Input
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                dir="ltr"
                autoFocus
                required
              />
            </Field>
            <Field label={t('setup.email')}>
              <Input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                dir="ltr"
              />
            </Field>
            <Field label={t('setup.password')} hint={t('validation.passwordLength')} required>
              <Input
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                dir="ltr"
                required
                minLength={8}
                maxLength={128}
              />
            </Field>
            <Field
              label={t('setup.confirmPassword')}
              hint={confirm && !match ? t('validation.passwordMismatch') : undefined}
              required
            >
              <Input
                type="password"
                autoComplete="new-password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                dir="ltr"
                required
              />
            </Field>
            <ErrorBox error={error} />
            <div className="flex gap-2">
              <Button onClick={() => setStep(0)}>{t('common.back')}</Button>
              <Button
                type="submit"
                variant="primary"
                className="flex-1"
                busy={busy}
                disabled={!usernameOk || !pwOk || !match}
              >
                {t('setup.submit')}
              </Button>
            </div>
          </>
        )}
      </form>
    </AuthShell>
  );
}
