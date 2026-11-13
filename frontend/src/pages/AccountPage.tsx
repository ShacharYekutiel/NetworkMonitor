import { useState, type FormEvent } from 'react';
import { useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints } from '../api/endpoints';
import { useToast } from '../components/toast';
import { Button, Card, ErrorBox, Field, Input, Notice, PageHeader } from '../components/ui';
import { useAuth } from '../hooks/auth';
import { ChatAccountsCard } from '../components/chatack/ChatAccounts';

export function AccountPage() {
  const { t } = useTranslation();
  const toast = useToast();
  const { user, setUser, logout } = useAuth();
  const [endingAll, setEndingAll] = useState(false);
  const [email, setEmail] = useState(user?.email ?? '');
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');

  const profile = useMutation({
    mutationFn: () => endpoints.updateMe({ email: email.trim() || null }),
    onSuccess: (r) => {
      setUser(r.user);
      toast.push('success', t('common.saved'));
    },
  });
  const pw = useMutation({
    mutationFn: () => endpoints.changePassword(current, next),
    onSuccess: () => {
      setCurrent('');
      setNext('');
      setConfirm('');
      toast.push('success', t('account.passwordChanged'));
    },
  });

  const pwOk = next.length >= 8 && next.length <= 128 && next === confirm && current.length > 0;

  const submitPw = (e: FormEvent) => {
    e.preventDefault();
    if (pwOk) pw.mutate();
  };

  if (!user) return null;
  // [3] oidc accounts have no local password either
  const ldap = user.source === 'ldap' || user.source === 'oidc';
  return (
    <div className="max-w-xl space-y-6">
      <PageHeader
        title={t('account.title')}
        subtitle={`${user.display_name ? `${user.display_name} · ` : ''}${user.username} · ${t(`role.${user.role}`)} · ${t(`user.source.${user.source === 'ldap' || user.source === 'oidc' ? user.source : 'local'}`)}`}
      />
      {ldap && (
        <Notice>
          {user.source === 'oidc' ? t('account.oidcManaged') : t('account.ldapManaged')}
        </Notice>
      )}
      <Card title={t('account.profile')}>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            profile.mutate();
          }}
          className="space-y-3"
        >
          <Field label={t('users.email')}>
            <Input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              dir="ltr"
              disabled={ldap}
            />
          </Field>
          <p className="text-xs text-ink-muted">{t('account.languageHint')}</p>
          <ErrorBox error={profile.error} />
          <div className="flex justify-end">
            <Button type="submit" variant="primary" busy={profile.isPending} disabled={ldap}>
              {t('common.save')}
            </Button>
          </div>
        </form>
      </Card>
      {!ldap && (
        <Card title={t('account.changePassword')}>
          <form onSubmit={submitPw} className="space-y-3">
            <Field label={t('account.currentPassword')} required>
              <Input
                type="password"
                autoComplete="current-password"
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
                dir="ltr"
                required
              />
            </Field>
            <Field label={t('account.newPassword')} hint={t('validation.passwordLength')} required>
              <Input
                type="password"
                autoComplete="new-password"
                value={next}
                onChange={(e) => setNext(e.target.value)}
                dir="ltr"
                required
                minLength={8}
                maxLength={128}
              />
            </Field>
            <Field
              label={t('account.confirmPassword')}
              hint={confirm && confirm !== next ? t('validation.passwordMismatch') : undefined}
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
            <ErrorBox error={pw.error} />
            <div className="flex justify-end">
              <Button type="submit" variant="primary" busy={pw.isPending} disabled={!pwOk}>
                {t('account.changePassword')}
              </Button>
            </div>
          </form>
        </Card>
      )}
      <Card title={t('account.sessions')}>
        <p className="text-sm text-ink-muted">{t('account.sessionsHint')}</p>
        <div className="mt-3 flex justify-end">
          <Button
            busy={endingAll}
            onClick={() => {
              setEndingAll(true);
              void logout(true);
            }}
            data-testid="logout-everywhere"
          >
            {t('account.logoutEverywhere')}
          </Button>
        </div>
      </Card>
      <ChatAccountsCard />
    </div>
  );
}
