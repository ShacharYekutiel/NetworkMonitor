import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { TFunction } from 'i18next';
import { useTranslation } from 'react-i18next';
import { endpoints3b, qk3b } from '../../api/endpoints3b';
import { endpoints4, qk4 } from '../../api/endpoints4';
import type { AwsAccess, AwsConnection, AwsTestResult } from '../../api/types';
import { formatDateTime } from '../../lib/format';
import { cardAnchor } from '../../lib/settingsIndex';
import { PlusIcon } from '../icons';
import {
  Button,
  Card,
  ConfirmDialog,
  cx,
  Empty,
  ErrorBox,
  Loading,
  Modal,
  Table,
  td,
  th,
} from '../ui';
import { AwsConnectionForm, AwsTestResultView } from './AwsConnectionForm';

function DefaultBadge() {
  const { t } = useTranslation();
  return (
    <span
      className="ms-1.5 rounded bg-sky-100 px-1.5 py-0.5 text-[10px] font-medium text-sky-800 dark:bg-sky-900 dark:text-sky-200"
      data-testid="default-badge"
    >
      {t('aws.connections.defaultBadge')}
    </span>
  );
}

/** Why *Delete* is not possible right now (null: it is). */
function deleteBlock(c: AwsConnection, count: number, t: TFunction): string | null {
  if (c.used_by.length > 0)
    return t('aws.connections.deleteUsed', {
      list: c.used_by
        .map((u) => t(`aws.connections.consumer.${u.kind}`, { defaultValue: u.name }))
        .join(', '),
    });
  if (c.is_default && count > 1) return t('aws.connections.deleteDefault');
  return null;
}

/**
 * *Set as default*: while the assistant is ON and uses "the default connection" (`connection_id:
 * null`), the new default is tested first (with the assistant's hop) and changed only after a
 * successful Test — the configure-first rule (coordinator decision for 4-0a).
 */
function SetDefaultDialog({
  c,
  implicitAccess,
  onClose,
}: {
  c: AwsConnection;
  /** the access of an ON consumer that uses the default implicitly; null when there is none */
  implicitAccess: AwsAccess | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [result, setResult] = useState<AwsTestResult | null>(null);
  const done = (list: AwsConnection[]) => {
    qc.setQueryData(qk4.connections, list);
    void qc.invalidateQueries({ queryKey: qk3b.settings });
    onClose();
  };
  const run = useMutation({
    mutationFn: async () => {
      if (!implicitAccess) return endpoints4.setDefault(c.id);
      setResult(null);
      const r = await endpoints4.test({
        connection_id: c.id,
        connection: null,
        access: implicitAccess.mode === 'direct' ? null : implicitAccess,
      });
      setResult(r);
      if (!r.ok || !r.test_id) return null;
      return endpoints4.setDefault(c.id, r.test_id);
    },
    onSuccess: (list) => {
      if (list) done(list);
    },
  });
  return (
    <Modal
      open
      title={t('aws.connections.setDefaultTitle', { name: c.name })}
      onClose={onClose}
      testId="set-default-dialog"
      footer={
        <>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button
            variant="primary"
            busy={run.isPending}
            onClick={() => run.mutate()}
            data-testid="set-default-confirm"
          >
            {implicitAccess
              ? t('aws.connections.testAndSetDefault')
              : t('aws.connections.setDefault')}
          </Button>
        </>
      }
    >
      <div className="space-y-3 text-sm">
        <p>
          {implicitAccess
            ? t('aws.connections.setDefaultImplicit', { name: c.name })
            : t('aws.connections.setDefaultPlain', { name: c.name })}
        </p>
        <ErrorBox error={run.error} />
        {result && <AwsTestResultView r={result} />}
      </div>
    </Modal>
  );
}

/**
 * [4] Settings → AWS: the shared AWS connections (architecture § 15.1.10) — name, *Default*,
 * authentication, account and ARN of the last test, last test, used by; *Add connection*,
 * *Edit*, *Set as default*, *Delete* (disabled with the reason while used). The editor opens
 * inline under the table.
 */
export function AwsConnectionsCard({ assistantOn }: { assistantOn: boolean }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: qk4.connections, queryFn: endpoints4.connections });
  // the assistant's access decides whether a new default must be tested first
  const asst = useQuery({ queryKey: qk3b.settings, queryFn: endpoints3b.settings });
  const [editing, setEditing] = useState<number | 'new' | null>(null);
  const [defaulting, setDefaulting] = useState<AwsConnection | null>(null);
  const [deleting, setDeleting] = useState<AwsConnection | null>(null);
  const del = useMutation({
    mutationFn: (id: number) => endpoints4.deleteConnection(id),
    onSuccess: (_r, id) => {
      setDeleting(null);
      if (editing === id) setEditing(null);
      void qc.invalidateQueries({ queryKey: qk4.connections });
    },
  });
  const list = q.data ?? [];
  const current = typeof editing === 'number' ? (list.find((c) => c.id === editing) ?? null) : null;
  const implicitAccess =
    assistantOn && asst.data && asst.data.connection_id === null ? asst.data.access : null;

  return (
    <div className="space-y-4">
      <Card
        title={t('aws.connections.title')}
        anchor={cardAnchor('awsConnections')}
        actions={
          <Button
            size="sm"
            variant="primary"
            onClick={() => setEditing('new')}
            data-testid="aws-add"
          >
            <PlusIcon size={12} /> {t('aws.connections.add')}
          </Button>
        }
        bodyClassName="p-0"
      >
        <p className="px-4 pt-3 text-xs text-slate-500">{t('aws.tabIntro')}</p>
        {q.isLoading && <Loading />}
        <ErrorBox error={q.error} className="m-4" />
        {q.data && list.length === 0 && <Empty>{t('aws.connections.empty')}</Empty>}
        {list.length > 0 && (
          <Table>
            <thead>
              <tr>
                <th className={th}>{t('aws.connections.col.name')}</th>
                <th className={th}>{t('aws.connections.col.auth')}</th>
                <th className={th}>{t('aws.connections.col.identity')}</th>
                <th className={th}>{t('aws.connections.col.lastTest')}</th>
                <th className={th}>{t('aws.connections.col.usedBy')}</th>
                <th className={th} />
              </tr>
            </thead>
            <tbody>
              {list.map((c) => {
                const block = deleteBlock(c, list.length, t);
                const chain = c.chains.find((x) => x.consumer === 'assistant');
                return (
                  <tr
                    key={c.id}
                    data-connection={c.name}
                    data-default={c.is_default}
                    className={cx(editing === c.id && 'bg-sky-50/60 dark:bg-sky-950/30')}
                  >
                    <td className={td}>
                      <span className="font-medium">{c.name}</span>
                      {c.is_default && <DefaultBadge />}
                      {c.warnings.length > 0 && (
                        <ul className="mt-0.5 space-y-0.5 text-[11px] text-amber-700 dark:text-amber-300">
                          {c.warnings.map((w) => (
                            <li key={w} data-warning={w}>
                              {t(`aws.warning.${w}`, { defaultValue: w })}
                            </li>
                          ))}
                        </ul>
                      )}
                    </td>
                    <td className={td}>
                      <span className="block">{t(`aws.auth.${c.auth_mode}`)}</span>
                      <span className="text-xs text-slate-500" dir="ltr">
                        {c.region}
                      </span>
                    </td>
                    <td className={cx(td, 'max-w-[22rem]')}>
                      {c.last_test?.account ? (
                        <>
                          <span className="block font-mono text-xs" dir="ltr">
                            {c.last_test.account}
                          </span>
                          <span
                            className="block break-all text-start font-mono text-[11px] text-slate-500"
                            dir="ltr"
                          >
                            {c.last_test.arn}
                          </span>
                        </>
                      ) : (
                        <span className="text-slate-400">—</span>
                      )}
                    </td>
                    <td className={cx(td, 'text-xs')} data-testid="last-test">
                      {!c.last_test ? (
                        <span className="text-slate-400">{t('aws.connections.never')}</span>
                      ) : c.last_test.ok ? (
                        <span className="text-green-700 dark:text-green-400">
                          {t('aws.connections.lastOk', { time: formatDateTime(c.last_test.at) })}
                        </span>
                      ) : (
                        <span className="text-red-600">
                          {t('aws.connections.lastFail', {
                            code: c.last_test.error_code ?? '',
                            time: formatDateTime(c.last_test.at),
                          })}
                        </span>
                      )}
                    </td>
                    <td className={cx(td, 'text-xs')}>
                      {c.used_by.length === 0 ? (
                        <span className="text-slate-400">—</span>
                      ) : (
                        c.used_by.map((u) => (
                          <span key={`${u.kind}-${u.id ?? ''}`} className="block">
                            {t(`aws.connections.consumer.${u.kind}`, { defaultValue: u.name })} ·{' '}
                            {t(`aws.connections.accessShort.${u.access_mode}`)}
                            {chain && (
                              <span
                                className={cx(
                                  'ms-1',
                                  chain.state === 'ok' && 'text-green-700 dark:text-green-400',
                                  chain.state === 'error' && 'text-red-600',
                                  chain.state === 'idle' && 'text-slate-400',
                                )}
                              >
                                ({t(`aws.connections.chainState.${chain.state}`)})
                              </span>
                            )}
                          </span>
                        ))
                      )}
                    </td>
                    <td className={cx(td, 'text-end')}>
                      <div className="flex justify-end gap-1 whitespace-nowrap">
                        <Button size="sm" onClick={() => setEditing(c.id)} data-testid="aws-edit">
                          {t('common.edit')}
                        </Button>
                        {!c.is_default && (
                          <Button
                            size="sm"
                            onClick={() => setDefaulting(c)}
                            data-testid="aws-set-default"
                          >
                            {t('aws.connections.setDefault')}
                          </Button>
                        )}
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={!!block}
                          title={block ?? undefined}
                          onClick={() => setDeleting(c)}
                          data-testid="aws-delete"
                        >
                          {t('common.delete')}
                        </Button>
                      </div>
                      {block && (
                        <p
                          className="mt-1 max-w-[16rem] text-start text-[11px] text-slate-500 ms-auto"
                          data-testid="delete-reason"
                        >
                          {block}
                        </p>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        )}
      </Card>
      {editing !== null && (editing === 'new' || current) && (
        <AwsConnectionForm
          key={editing}
          connection={current}
          connections={list}
          assistantOn={assistantOn}
          onClose={() => setEditing(null)}
          onCreated={(c) => {
            qc.setQueryData<AwsConnection[]>(qk4.connections, (old) => [
              ...(old ?? []).filter((x) => x.id !== c.id),
              c,
            ]);
            setEditing(c.id);
          }}
        />
      )}
      {defaulting && (
        <SetDefaultDialog
          c={defaulting}
          implicitAccess={implicitAccess}
          onClose={() => setDefaulting(null)}
        />
      )}
      <ConfirmDialog
        open={!!deleting}
        title={t('common.delete')}
        message={deleting ? t('aws.connections.deleteConfirm', { name: deleting.name }) : ''}
        onConfirm={() => deleting && del.mutate(deleting.id)}
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
