import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { ApiToken } from '../api/types';
import { formatDateTime, formatRelative } from '../lib/format';
import { sortTokens } from '../lib/tokens';
import { ConfirmDialog, cx, Empty, ErrorBox, Loading, Button, Table, td, th } from './ui';

const STATE_CLS: Record<ApiToken['state'], string> = {
  active: 'bg-green-100 text-green-800 dark:bg-green-900/50 dark:text-green-200',
  expired: 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300',
  revoked: 'bg-red-100 text-red-800 dark:bg-red-900/50 dark:text-red-200',
};

/** [2b-3] Token list (own, or every user's for admins with `all`) with revoke. */
export function TokensTable({ all = false }: { all?: boolean }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: qk.tokens(all), queryFn: () => endpoints.tokens(all) });
  const [revoking, setRevoking] = useState<ApiToken | null>(null);
  const revoke = useMutation({
    mutationFn: (x: ApiToken) => endpoints.revokeToken(x.id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['tokens'] });
      setRevoking(null);
    },
  });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} className="m-3" />;
  const items = sortTokens(q.data?.items ?? []);
  if (!items.length) return <Empty>{t('tokens.none')}</Empty>;
  return (
    <>
      <Table>
        <thead>
          <tr>
            <th className={th}>{t('common.name')}</th>
            {all && <th className={th}>{t('users.username')}</th>}
            <th className={th}>{t('tokens.prefix')}</th>
            <th className={th}>{t('users.role')}</th>
            <th className={th}>{t('tokens.state')}</th>
            <th className={th}>{t('tokens.expires')}</th>
            <th className={th}>{t('tokens.lastUsed')}</th>
            <th className={th} />
          </tr>
        </thead>
        <tbody>
          {items.map((x) => (
            <tr key={x.id} className={x.state === 'active' ? undefined : 'opacity-60'}>
              <td className={`${td} font-medium`}>{x.name}</td>
              {all && (
                <td className={td}>
                  <bdi dir="ltr">{x.username}</bdi>
                </td>
              )}
              <td className={`${td} font-mono text-xs`}>
                <bdi dir="ltr">{x.prefix}…</bdi>
              </td>
              <td className={td}>{t(`role.${x.role}`)}</td>
              <td className={td}>
                <span
                  className={cx(
                    'rounded px-1.5 py-0.5 text-[11px] font-medium',
                    STATE_CLS[x.state],
                  )}
                >
                  {t(`tokens.states.${x.state}`)}
                </span>
              </td>
              <td
                className={`${td} whitespace-nowrap text-xs`}
                title={formatDateTime(x.expires_at)}
              >
                {formatRelative(x.expires_at)}
              </td>
              <td className={`${td} whitespace-nowrap text-xs text-slate-500`}>
                {x.last_used_at ? (
                  <span title={formatDateTime(x.last_used_at)}>
                    {formatRelative(x.last_used_at)}
                    {x.last_used_ip && (
                      <bdi dir="ltr" className="ms-1">
                        ({x.last_used_ip})
                      </bdi>
                    )}
                  </span>
                ) : (
                  t('tokens.never')
                )}
              </td>
              <td className={`${td} text-end`}>
                {x.state === 'active' && (
                  <Button size="sm" variant="ghost" onClick={() => setRevoking(x)}>
                    {t('tokens.revoke')}
                  </Button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </Table>
      <ConfirmDialog
        open={!!revoking}
        title={t('tokens.revokeTitle')}
        message={t('tokens.revokeConfirm', { name: revoking?.name ?? '' })}
        confirmLabel={t('tokens.revoke')}
        onConfirm={() => revoking && revoke.mutate(revoking)}
        onClose={() => {
          setRevoking(null);
          revoke.reset();
        }}
        busy={revoke.isPending}
        error={revoke.error}
      />
    </>
  );
}
