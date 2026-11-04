import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import { Breadcrumbs } from '../components/Breadcrumbs';
import { DeviceFormModal, GroupFormModal } from '../components/EntityForms';
import { EditIcon, PlusIcon, TrashIcon } from '../components/icons';
import { StatusBadge, StatusBar, StatusCountPills, StatusDot } from '../components/Status';
import { useToast } from '../components/toast';
import {
  Button,
  Card,
  ConfirmDialog,
  Empty,
  ErrorBox,
  Loading,
  Ltr,
  PageHeader,
  Table,
  td,
  th,
} from '../components/ui';
import { useAuth } from '../hooks/auth';
import { useTree } from '../hooks/queries';
import { findGroup } from '../lib/tree';

export function GroupPage() {
  const { id: idParam } = useParams();
  const id = Number(idParam);
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const toast = useToast();
  const group = useQuery({ queryKey: qk.group(id), queryFn: () => endpoints.group(id) });
  const devices = useQuery({
    queryKey: qk.devices({ group_id: id }),
    queryFn: () => endpoints.devices({ group_id: id }),
  });
  const tree = useTree();
  const node = tree.data ? findGroup(tree.data.groups, id) : null;

  const [editing, setEditing] = useState(false);
  const [addSub, setAddSub] = useState(false);
  const [addDevice, setAddDevice] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const del = useMutation({
    mutationFn: () => endpoints.deleteGroup(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.tree });
      void qc.invalidateQueries({ queryKey: qk.dashboard });
      qc.removeQueries({ queryKey: qk.group(id) });
      toast.push('success', t('common.deleted'));
      navigate(group.data?.parent_id ? `/groups/${group.data.parent_id}` : '/');
    },
  });

  if (group.isLoading) return <Loading />;
  if (group.error || !group.data) return <ErrorBox error={group.error} />;
  const g = group.data;

  return (
    <div className="space-y-4">
      <PageHeader
        breadcrumbs={<Breadcrumbs groupId={g.parent_id} />}
        title={
          <>
            <StatusDot status={g.status} className="h-3 w-3" /> {g.name}
          </>
        }
        subtitle={g.description || undefined}
        actions={
          isAdmin && (
            <>
              <Button onClick={() => setAddDevice(true)} variant="primary">
                <PlusIcon size={14} /> {t('devices.add')}
              </Button>
              <Button onClick={() => setAddSub(true)}>
                <PlusIcon size={14} /> {t('groups.addSub')}
              </Button>
              <Button onClick={() => setEditing(true)}>
                <EditIcon size={14} /> {t('common.edit')}
              </Button>
              <Button variant="danger" onClick={() => setDeleting(true)}>
                <TrashIcon size={14} /> {t('common.delete')}
              </Button>
            </>
          )
        }
      />
      <Card>
        <div className="flex flex-wrap items-center gap-4">
          <StatusBadge status={g.status} />
          <span className="text-sm text-slate-500">
            {t('groups.sensorCount', { count: g.status_counts.total })}
          </span>
          <StatusCountPills counts={g.status_counts} />
          <span className="text-xs text-slate-500">
            {t('snmp.label')}:{' '}
            {g.snmp_credentials ? g.snmp_credentials.version : t('snmp.inherited')}
          </span>
        </div>
        <div className="mt-3">
          <StatusBar counts={g.status_counts} />
        </div>
      </Card>

      {node && node.children.length > 0 && (
        <Card title={t('groups.subgroups')} bodyClassName="p-0">
          <Table>
            <thead>
              <tr>
                <th className={th}>{t('common.status')}</th>
                <th className={th}>{t('common.name')}</th>
                <th className={th}>{t('groups.sensors')}</th>
              </tr>
            </thead>
            <tbody>
              {node.children.map((c) => (
                <tr key={c.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
                  <td className={td}>
                    <StatusBadge status={c.status} />
                  </td>
                  <td className={td}>
                    <Link
                      to={`/groups/${c.id}`}
                      className="font-medium text-sky-700 hover:underline dark:text-sky-400"
                    >
                      {c.name}
                    </Link>
                  </td>
                  <td className={td}>
                    <StatusCountPills counts={c.status_counts} />
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Card>
      )}

      <Card title={t('groups.devices')} bodyClassName="p-0">
        {devices.isLoading && <Loading />}
        <ErrorBox error={devices.error} />
        {devices.data && !devices.data.items.length && <Empty>{t('devices.none')}</Empty>}
        {devices.data && devices.data.items.length > 0 && (
          <Table>
            <thead>
              <tr>
                <th className={th}>{t('common.status')}</th>
                <th className={th}>{t('common.name')}</th>
                <th className={th}>{t('devices.host')}</th>
                <th className={th}>{t('groups.sensors')}</th>
              </tr>
            </thead>
            <tbody>
              {devices.data.items.map((d) => (
                <tr key={d.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
                  <td className={td}>
                    <StatusBadge status={d.status} />
                  </td>
                  <td className={td}>
                    <Link
                      to={`/devices/${d.id}`}
                      className="font-medium text-sky-700 hover:underline dark:text-sky-400"
                    >
                      {d.name}
                    </Link>
                  </td>
                  <td className={`${td} text-slate-500`}>
                    <Ltr>{d.host}</Ltr>
                  </td>
                  <td className={td}>
                    <StatusCountPills counts={d.status_counts} />
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      <GroupFormModal open={editing} onClose={() => setEditing(false)} group={g} />
      <GroupFormModal open={addSub} onClose={() => setAddSub(false)} parentId={g.id} />
      <DeviceFormModal
        open={addDevice}
        onClose={() => setAddDevice(false)}
        groupId={g.id}
        onCreated={(d) => navigate(`/devices/${d.id}`)}
      />
      <ConfirmDialog
        open={deleting}
        title={t('groups.deleteTitle')}
        message={t('groups.deleteConfirm', { name: g.name, count: g.status_counts.total })}
        onConfirm={() => del.mutate()}
        onClose={() => setDeleting(false)}
        busy={del.isPending}
        error={del.error}
      />
    </div>
  );
}
