import { useEffect, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk, type UserInput } from '../api/endpoints';
import type { Language, Role, User } from '../api/types';
import { EditIcon, PlusIcon, TrashIcon } from '../components/icons';
import { useToast } from '../components/toast';
import {
  Button,
  Card,
  Checkbox,
  ConfirmDialog,
  ErrorBox,
  Field,
  Input,
  Loading,
  Modal,
  PageHeader,
  Select,
  Table,
  td,
  th,
} from '../components/ui';
import { useAuth } from '../hooks/auth';
import { formatDateTime } from '../lib/format';

const USERNAME_RE = /^[A-Za-z0-9._-]{3,64}$/;

function UserModal({ open, onClose, user }: { open: boolean; onClose: () => void; user?: User }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const { user: me, setUser } = useAuth();
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [role, setRole] = useState<Role>('viewer');
  const [language, setLanguage] = useState<Language>('en');
  const [active, setActive] = useState(true);
  const [password, setPassword] = useState('');

  useEffect(() => {
    if (!open) return;
    setUsername(user?.username ?? '');
    setEmail(user?.email ?? '');
    setRole(user?.role ?? 'viewer');
    setLanguage(user?.language ?? 'en');
    setActive(user?.is_active ?? true);
    setPassword('');
  }, [open, user]);

  const m = useMutation({
    mutationFn: (body: UserInput) =>
      user ? endpoints.updateUser(user.id, body) : endpoints.createUser(body),
    onSuccess: (u) => {
      void qc.invalidateQueries({ queryKey: qk.users });
      if (me && u.id === me.id) setUser(u);
      toast.push('success', t('common.saved'));
      onClose();
    },
  });

  const pwOk = user
    ? !password || (password.length >= 8 && password.length <= 128)
    : password.length >= 8 && password.length <= 128;
  const unameOk = user || USERNAME_RE.test(username);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!pwOk || !unameOk) return;
    const body: UserInput = { email: email.trim() || null, role, language };
    if (user) {
      body.is_active = active;
      if (password) body.password = password;
    } else {
      body.username = username.trim();
      body.password = password;
    }
    m.mutate(body);
  };

  return (
    <Modal open={open} onClose={onClose} title={user ? t('users.edit') : t('users.add')}>
      <form onSubmit={submit} className="space-y-3">
        <Field
          label={t('users.username')}
          required
          hint={!unameOk ? t('validation.username') : undefined}
        >
          <Input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            disabled={!!user}
            dir="ltr"
            required
          />
        </Field>
        <Field label={t('users.email')}>
          <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} dir="ltr" />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label={t('users.role')}>
            <Select value={role} onChange={(e) => setRole(e.target.value as Role)}>
              <option value="viewer">{t('role.viewer')}</option>
              <option value="admin">{t('role.admin')}</option>
            </Select>
          </Field>
          <Field label={t('users.language')}>
            <Select value={language} onChange={(e) => setLanguage(e.target.value as Language)}>
              <option value="en">English</option>
              <option value="he">עברית</option>
            </Select>
          </Field>
        </div>
        <Field
          label={user ? t('users.newPassword') : t('users.password')}
          hint={user ? t('users.passwordKeepHint') : t('validation.passwordLength')}
          required={!user}
        >
          <Input
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            dir="ltr"
            required={!user}
          />
        </Field>
        {user && <Checkbox label={t('users.active')} checked={active} onChange={setActive} />}
        <ErrorBox error={m.error} />
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="submit" variant="primary" busy={m.isPending} disabled={!pwOk || !unameOk}>
            {t('common.save')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function UsersPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const { user: me } = useAuth();
  const q = useQuery({ queryKey: qk.users, queryFn: endpoints.users });
  const [editing, setEditing] = useState<User | 'new' | null>(null);
  const [deleting, setDeleting] = useState<User | null>(null);
  const del = useMutation({
    mutationFn: (u: User) => endpoints.deleteUser(u.id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.users });
      setDeleting(null);
    },
  });

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('users.title')}
        actions={
          <Button variant="primary" onClick={() => setEditing('new')}>
            <PlusIcon size={14} /> {t('users.add')}
          </Button>
        }
      />
      <Card bodyClassName="p-0">
        {q.isLoading && <Loading />}
        <ErrorBox error={q.error} className="m-3" />
        {q.data && (
          <Table>
            <thead>
              <tr>
                <th className={th}>{t('users.username')}</th>
                <th className={th}>{t('users.email')}</th>
                <th className={th}>{t('users.role')}</th>
                <th className={th}>{t('users.language')}</th>
                <th className={th}>{t('users.active')}</th>
                <th className={th}>{t('users.lastLogin')}</th>
                <th className={th} />
              </tr>
            </thead>
            <tbody>
              {q.data.items.map((u) => (
                <tr key={u.id} className={u.is_active ? undefined : 'opacity-60'}>
                  <td className={`${td} font-medium`}>
                    {u.username}
                    {me?.id === u.id && (
                      <span className="ms-1 text-xs text-slate-500">({t('users.you')})</span>
                    )}
                  </td>
                  <td className={td} dir="ltr">
                    {u.email ?? '—'}
                  </td>
                  <td className={td}>{t(`role.${u.role}`)}</td>
                  <td className={td}>{u.language === 'he' ? 'עברית' : 'English'}</td>
                  <td className={td}>{u.is_active ? t('common.yes') : t('common.no')}</td>
                  <td className={`${td} whitespace-nowrap text-slate-500`}>
                    {formatDateTime(u.last_login_at)}
                  </td>
                  <td className={`${td} whitespace-nowrap text-end`}>
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label={t('common.edit')}
                      onClick={() => setEditing(u)}
                    >
                      <EditIcon size={14} />
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label={t('common.delete')}
                      disabled={me?.id === u.id}
                      onClick={() => setDeleting(u)}
                    >
                      <TrashIcon size={14} />
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <UserModal
        open={editing !== null}
        onClose={() => setEditing(null)}
        user={editing && editing !== 'new' ? editing : undefined}
      />
      <ConfirmDialog
        open={!!deleting}
        title={t('users.deleteTitle')}
        message={t('common.deleteConfirm', { name: deleting?.username })}
        onConfirm={() => deleting && del.mutate(deleting)}
        onClose={() => {
          setDeleting(null);
          del.reset();
        }}
        busy={del.isPending}
        error={del.error}
      />
    </div>
  );
}
