import { useState, type FormEvent } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints } from '../api/endpoints';
import type { Role } from '../api/types';
import { PlusIcon } from '../components/icons';
import { TokensTable } from '../components/TokensTable';
import {
  Button,
  Card,
  ErrorBox,
  Field,
  Input,
  Modal,
  Notice,
  PageHeader,
  Select,
} from '../components/ui';
import { useAuth } from '../hooks/auth';
import { FeatureOffNotice, useFeatures } from '../hooks/features';
import { expiresDaysOk, tokenNameOk, tokenRoles } from '../lib/tokens';

/** Copies text; falls back to selecting the field when the Clipboard API is unavailable (http). */
async function copyText(text: string, fallback: HTMLInputElement | null): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    if (!fallback) return false;
    fallback.select();
    try {
      return document.execCommand('copy');
    } catch {
      return false;
    }
  }
}

function CreateTokenModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const { user } = useAuth();
  const [name, setName] = useState('');
  const [role, setRole] = useState<Role>('viewer');
  const [days, setDays] = useState('90');
  const [token, setToken] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [field, setField] = useState<HTMLInputElement | null>(null);
  const m = useMutation({
    mutationFn: () =>
      endpoints.createToken({ name: name.trim(), role, expires_days: Number(days) }),
    onSuccess: (r) => {
      setToken(r.token);
      void qc.invalidateQueries({ queryKey: ['tokens'] });
    },
  });
  const close = () => {
    setName('');
    setRole('viewer');
    setDays('90');
    setToken(null);
    setCopied(false);
    m.reset();
    onClose();
  };
  const ok = tokenNameOk(name) && expiresDaysOk(days);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (ok) m.mutate();
  };
  return (
    <Modal
      open={open}
      onClose={close}
      title={token ? t('tokens.createdTitle') : t('tokens.create')}
    >
      {token ? (
        <div className="space-y-3" data-testid="token-once">
          <Notice>{t('tokens.showOnce')}</Notice>
          <div className="flex gap-2">
            <Input
              ref={setField}
              readOnly
              value={token}
              dir="ltr"
              className="font-mono text-xs"
              onFocus={(e) => e.target.select()}
            />
            <Button variant="primary" onClick={async () => setCopied(await copyText(token, field))}>
              {copied ? t('tokens.copied') : t('tokens.copy')}
            </Button>
          </div>
          <p className="text-xs text-slate-500">{t('tokens.usage')}</p>
          <pre
            dir="ltr"
            className="overflow-x-auto rounded bg-slate-100 p-2 text-left text-xs dark:bg-slate-800"
          >
            {'curl -H "Authorization: Bearer nmt_…" ' + window.location.origin + '/api/dashboard'}
          </pre>
          <div className="flex justify-end">
            <Button onClick={close}>{t('common.close')}</Button>
          </div>
        </div>
      ) : (
        <form onSubmit={submit} className="space-y-3">
          <Field label={t('common.name')} required hint={t('tokens.nameHint')}>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={128}
              autoFocus
            />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label={t('users.role')} hint={t('tokens.roleHint')}>
              <Select value={role} onChange={(e) => setRole(e.target.value as Role)}>
                {tokenRoles(user?.role ?? 'viewer').map((r) => (
                  <option key={r} value={r}>
                    {t(`role.${r}`)}
                  </option>
                ))}
              </Select>
            </Field>
            <Field
              label={t('tokens.expiresDays')}
              hint={
                expiresDaysOk(days) ? undefined : (
                  <span className="text-red-600">{t('tokens.expiresErr')}</span>
                )
              }
            >
              <Input
                type="number"
                min={1}
                max={365}
                value={days}
                onChange={(e) => setDays(e.target.value)}
                dir="ltr"
              />
            </Field>
          </div>
          <ErrorBox error={m.error} />
          <div className="flex justify-end gap-2">
            <Button onClick={close}>{t('common.cancel')}</Button>
            <Button type="submit" variant="primary" busy={m.isPending} disabled={!ok}>
              {t('tokens.create')}
            </Button>
          </div>
        </form>
      )}
    </Modal>
  );
}

/** [2b-3] Personal API tokens (account menu). */
export function TokensPage() {
  const { t } = useTranslation();
  const { features, loaded } = useFeatures();
  const [creating, setCreating] = useState(false);
  return (
    <div className="space-y-4">
      <PageHeader
        title={t('tokens.title')}
        subtitle={t('tokens.intro')}
        actions={
          features.api_tokens && (
            <Button variant="primary" onClick={() => setCreating(true)}>
              <PlusIcon size={14} /> {t('tokens.create')}
            </Button>
          )
        }
      />
      {loaded && !features.api_tokens ? (
        <FeatureOffNotice feature="api_tokens" />
      ) : (
        loaded && (
          <Card bodyClassName="p-0">
            <TokensTable />
          </Card>
        )
      )}
      <CreateTokenModal open={creating} onClose={() => setCreating(false)} />
    </div>
  );
}
