import { useState, type FormEvent } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { Language } from '../api/types';
import { setLanguage } from '../i18n';
import { useToast } from '../components/toast';
import {
  Button,
  Card,
  ConfirmDialog,
  ErrorBox,
  Field,
  GroupList,
  GroupTitle,
  Input,
  Notice,
  PageHeader,
  SegmentedControl,
} from '../components/ui';
import { ActionRow, Avatar } from '../components/kit';
import { KeyIcon, LogoutIcon } from '../components/icons';
import { useAuth } from '../hooks/auth';
import { useTheme, type ThemeChoice } from '../hooks/theme';
import { ChatAccountsCard } from '../components/chatack/ChatAccounts';

/** Theme and language of this browser / user (prototype: segmented control + language in the profile card). */
function Preferences() {
  const { t, i18n } = useTranslation();
  const { choice, setChoice } = useTheme();
  const { user, setUser } = useAuth();
  const lang = (i18n.language === 'he' ? 'he' : 'en') as Language;
  const chooseLang = (l: Language) => {
    if (l === lang) return;
    setLanguage(l);
    if (user && user.language !== l)
      endpoints
        .updateMe({ language: l })
        .then((r) => setUser(r.user))
        .catch(() => undefined);
  };
  return (
    <div className="grid w-full gap-4">
      <div className="grid gap-1.5">
        <span className="text-[13.5px] text-ink-muted">{t('theme.label')}</span>
        <SegmentedControl<ThemeChoice>
          label={t('theme.label')}
          value={choice}
          onChange={setChoice}
          className="justify-self-start"
          options={(['system', 'light', 'dark'] as const).map((v) => ({
            value: v,
            label: t(`theme.${v}`),
          }))}
        />
      </div>
      <div className="grid gap-1.5">
        <span className="text-[13.5px] text-ink-muted">{t('common.language')}</span>
        <SegmentedControl<Language>
          label={t('common.language')}
          value={lang}
          onChange={chooseLang}
          className="justify-self-start"
          options={[
            { value: 'he', label: <span lang="he">עברית</span> },
            { value: 'en', label: <span lang="en">English</span> },
          ]}
        />
        <span className="text-xs text-ink-subtle">{t('account.languageHint')}</span>
      </div>
    </div>
  );
}

export function AccountPage() {
  const { t } = useTranslation();
  const toast = useToast();
  const { user, setUser, logout } = useAuth();
  const [endingAll, setEndingAll] = useState(false);
  const [confirmAll, setConfirmAll] = useState(false);
  const [endingOne, setEndingOne] = useState(false);
  const [email, setEmail] = useState(user?.email ?? '');
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const tokens = useQuery({ queryKey: qk.tokens(false), queryFn: () => endpoints.tokens(false) });

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
  const source = t(
    `user.source.${user.source === 'ldap' || user.source === 'oidc' ? user.source : 'local'}`,
  );
  const activeTokens = tokens.data?.items.filter((x) => x.state === 'active').length;
  return (
    <div>
      <PageHeader title={t('account.title')} />
      {ldap && (
        <Notice className="mb-6">
          {user.source === 'oidc' ? t('account.oidcManaged') : t('account.ldapManaged')}
        </Notice>
      )}
      {/* prototype .two.even: who I am + preferences | chat accounts, tokens, security, sessions */}
      <div className="grid grid-cols-1 items-start gap-6 lg:grid-cols-2">
        <div className="grid min-w-0 gap-6">
          <Card bodyClassName="px-[22px] py-6">
            <div className="grid gap-5" data-testid="account-profile">
              <div className="flex items-center gap-4">
                <Avatar name={user.username} size="lg" />
                <div className="grid min-w-0">
                  <b className="truncate text-[22px] font-semibold">
                    <bdi dir="ltr" className="font-display">
                      {user.username}
                    </bdi>
                  </b>
                  <span className="text-ink-muted">
                    {user.display_name && (
                      <>
                        <bdi dir="auto">{user.display_name}</bdi>
                        {' · '}
                      </>
                    )}
                    {t(`role.${user.role}`)} · {source}
                  </span>
                </div>
              </div>
              <Preferences />
            </div>
          </Card>
          <Card title={t('account.profile')}>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                profile.mutate();
              }}
              className="grid gap-3"
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
              <ErrorBox error={profile.error} />
              <div className="flex justify-start">
                <Button type="submit" variant="primary" busy={profile.isPending} disabled={ldap}>
                  {t('common.save')}
                </Button>
              </div>
            </form>
          </Card>
        </div>

        <div className="grid min-w-0 gap-6">
          <ChatAccountsCard />
          <section aria-labelledby="acc-tokens">
            <GroupTitle id="acc-tokens">{t('tokens.title')}</GroupTitle>
            <GroupList label={t('tokens.title')}>
              <ActionRow
                icon={<KeyIcon size={17} />}
                to="/tokens"
                title={t('account.myTokens')}
                subtitle={
                  activeTokens === undefined
                    ? t('tokens.intro')
                    : t('account.activeTokens', { count: activeTokens })
                }
              />
            </GroupList>
          </section>
          {!ldap && (
            <Card title={t('account.changePassword')}>
              <form onSubmit={submitPw} className="grid gap-3">
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
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <Field
                    label={t('account.newPassword')}
                    hint={t('validation.passwordLength')}
                    required
                  >
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
                    hint={
                      confirm && confirm !== next ? (
                        <span className="text-status-down">{t('validation.passwordMismatch')}</span>
                      ) : undefined
                    }
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
                </div>
                <ErrorBox error={pw.error} />
                <div className="flex justify-start">
                  <Button type="submit" variant="primary" busy={pw.isPending} disabled={!pwOk}>
                    {t('account.changePassword')}
                  </Button>
                </div>
              </form>
            </Card>
          )}
          <Card title={t('account.sessions')}>
            <div className="grid gap-4" data-testid="account-sessions">
              <p className="m-0 text-[14.5px] text-ink-muted">{t('account.sessionsHint')}</p>
              {user.source !== 'local' && (
                <p className="m-0 text-xs text-ink-subtle">{t('account.sessionsDirectoryHint')}</p>
              )}
              <div className="flex flex-wrap gap-2.5">
                <Button
                  busy={endingOne}
                  onClick={() => {
                    setEndingOne(true);
                    void logout(false);
                  }}
                  data-testid="logout-here"
                >
                  <LogoutIcon size={14} className="rtl:rotate-180" /> {t('account.logoutHere')}
                </Button>
                <Button
                  variant="danger"
                  onClick={() => setConfirmAll(true)}
                  data-testid="logout-everywhere"
                >
                  {t('account.logoutEverywhere')}
                </Button>
              </div>
            </div>
          </Card>
        </div>
      </div>
      <ConfirmDialog
        open={confirmAll}
        title={t('account.logoutEverywhere')}
        message={t('account.logoutEverywhereConfirm')}
        confirmLabel={
          <span data-testid="logout-everywhere-confirm">{t('account.logoutEverywhereGo')}</span>
        }
        onConfirm={() => {
          setEndingAll(true);
          void logout(true);
        }}
        onClose={() => setConfirmAll(false)}
        busy={endingAll}
      />
    </div>
  );
}
