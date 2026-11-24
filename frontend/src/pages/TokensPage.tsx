import { useState, type FormEvent } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints } from '../api/endpoints';
import type { Role } from '../api/types';
import { CheckIcon, CopyIcon, KeyIcon, PlusIcon } from '../components/icons';
import { Banner } from '../components/kit';
import { TokensTable } from '../components/TokensTable';
import { Button, ErrorBox, Field, Input, Modal, PageHeader, Select } from '../components/ui';
import { useAuth } from '../hooks/auth';
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
        <div className="grid gap-4" data-testid="token-once">
          <Banner tone="warn" icon={KeyIcon} title={t('tokens.showOnceTitle')}>
            {t('tokens.showOnce')}
          </Banner>
          <div className="grid gap-1.5">
            <span className="text-[13.5px] text-ink-muted">{t('tokens.yourToken')}</span>
            <div className="flex gap-2">
              <Input
                ref={setField}
                readOnly
                value={token}
                dir="ltr"
                aria-label={t('tokens.yourToken')}
                className="font-mono text-[13px]"
                onFocus={(e) => e.target.select()}
              />
              <Button
                variant="primary"
                onClick={async () => setCopied(await copyText(token, field))}
              >
                {copied ? <CheckIcon size={14} /> : <CopyIcon size={14} />}
                {copied ? t('tokens.copied') : t('tokens.copy')}
              </Button>
            </div>
          </div>
          <div className="grid gap-1.5">
            <span className="text-[13.5px] text-ink-muted">{t('tokens.usage')}</span>
            <pre
              dir="ltr"
              className="m-0 overflow-x-auto whitespace-pre rounded-[16px] bg-chip px-4 py-3.5 text-left font-mono text-[12.5px] leading-[1.65]"
            >
              {'curl -H "Authorization: Bearer nmt_…" \\\n     ' +
                window.location.origin +
                '/api/dashboard'}
            </pre>
          </div>
          <div className="flex justify-start gap-2.5">
            <Button variant="primary" onClick={close}>
              {t('tokens.done')}
            </Button>
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
                  <span className="text-status-down">{t('tokens.expiresErr')}</span>
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
  const [creating, setCreating] = useState(false);
  return (
    <div>
      <PageHeader
        title={t('tokens.title')}
        subtitle={t('tokens.intro')}
        actions={
          <Button variant="primary" onClick={() => setCreating(true)}>
            <PlusIcon size={14} /> {t('tokens.create')}
          </Button>
        }
      />
      <TokensTable />
      <CreateTokenModal open={creating} onClose={() => setCreating(false)} />
    </div>
  );
}
