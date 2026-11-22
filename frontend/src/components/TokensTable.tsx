import { useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { ApiToken } from '../api/types';
import { formatDateTime, formatRelative } from '../lib/format';
import { daysLeft, sortTokens } from '../lib/tokens';
import { KeyIcon } from './icons';
import { EmptyState } from './kit';
import { ConfirmDialog, cx, ErrorBox, Loading, Button, TAG, TAG_TONE } from './ui';
import { ColumnsMenu, DataTable, labelOf, useDataTable, type DataColumn } from './kitTable';

const STATE_TONE: Record<ApiToken['state'], keyof typeof TAG_TONE> = {
  active: 'up',
  expired: 'neutral',
  revoked: 'down',
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
  const columns: DataColumn<ApiToken>[] = [
    {
      id: 'name',
      label: t('common.name'),
      minWidth: 120,
      className: 'font-medium',
      title: (x) => x.name,
      cell: (x) => <bdi dir="auto">{x.name}</bdi>,
    },
    ...(all
      ? [
          {
            id: 'user',
            label: t('users.username'),
            width: 150,
            title: (x: ApiToken) => x.username,
            cell: (x: ApiToken) => <bdi dir="ltr">{x.username}</bdi>,
          },
        ]
      : []),
    {
      id: 'prefix',
      label: t('tokens.prefix'),
      width: 130,
      className: 'font-mono text-[13px] text-ink-muted',
      cell: (x) => <bdi dir="ltr">{x.prefix}…</bdi>,
    },
    { id: 'role', label: t('users.role'), width: 110, cell: (x) => t(`role.${x.role}`) },
    {
      id: 'state',
      label: t('tokens.state'),
      width: 190,
      cell: (x) => (
        <>
          <span className={cx(TAG, TAG_TONE[STATE_TONE[x.state]])}>
            {t(`tokens.states.${x.state}`)}
          </span>
          {x.state === 'active' && daysLeft(x.expires_at) < 14 && (
            <span className={cx(TAG, TAG_TONE.warning, 'ms-1')}>
              {t('tokens.daysLeft', { count: Math.max(0, daysLeft(x.expires_at)) })}
            </span>
          )}
        </>
      ),
    },
    {
      id: 'expires',
      label: t('tokens.expires'),
      width: 140,
      className: 'text-[13px]',
      title: (x) => formatDateTime(x.expires_at),
      cell: (x) => formatRelative(x.expires_at),
    },
    {
      id: 'lastUsed',
      label: t('tokens.lastUsed'),
      minWidth: 140,
      className: 'text-[13px] text-ink-muted',
      title: (x) => (x.last_used_at ? formatDateTime(x.last_used_at) : undefined),
      cell: (x) =>
        x.last_used_at ? (
          <>
            {formatRelative(x.last_used_at)}
            {x.last_used_ip && (
              <bdi dir="ltr" className="ms-1">
                ({x.last_used_ip})
              </bdi>
            )}
          </>
        ) : (
          t('tokens.never')
        ),
    },
    {
      id: 'actions',
      label: t('tableCols.actions'),
      pin: 'end',
      fixed: true,
      width: 128,
      cell: (x) =>
        x.state === 'active' && (
          <Button
            size="sm"
            variant="ghost"
            className="text-status-down"
            onClick={() => setRevoking(x)}
          >
            {t('tokens.revoke')}
          </Button>
        ),
    },
  ];
  const layout = useDataTable(all ? 'tokens-all' : 'tokens', columns);
  const card = (body: ReactNode) => (
    <div className="min-w-0 overflow-hidden rounded-xl bg-surface shadow-card">{body}</div>
  );
  if (q.isLoading) return card(<Loading />);
  if (q.error) return card(<ErrorBox error={q.error} className="m-3" />);
  const items = sortTokens(q.data?.items ?? []);
  if (!items.length)
    return card(
      <EmptyState icon={KeyIcon} title={t('tokens.none')}>
        {all ? t('tokens.noneAllHint') : t('tokens.noneHint')}
      </EmptyState>,
    );
  return (
    <>
      <div className="mb-2.5 flex justify-end">
        <ColumnsMenu
          layout={layout}
          labelOf={(id) => labelOf(columns, id)}
          testId="token-columns"
        />
      </div>
      {card(
        <DataTable
          layout={layout}
          columns={layout.columns}
          rows={items}
          rowKey={(x) => x.id}
          rowProps={(x) => ({
            className: x.state === 'active' ? undefined : 'opacity-60',
            'data-testid': 'token-row',
          })}
        />,
      )}
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
