import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import { Breadcrumbs } from '../components/Breadcrumbs';
import { DeviceFormModal, GroupFormModal } from '../components/EntityForms';
import { EditIcon, FolderIcon, PlusIcon, ServerIcon, TrashIcon } from '../components/icons';
import { StatusBadge, StatusBar, StatusCountPills, StatusTiles } from '../components/Status';
import { AddMaintenanceButton, MaintenanceBadge } from '../components/Maintenance';
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
  cx,
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
    <div className="space-y-5">
      <PageHeader
        breadcrumbs={<Breadcrumbs groupId={g.parent_id} />}
        title={
          <>
            <FolderIcon size={22} className="shrink-0 text-ink-subtle" />
            <span className="truncate">{g.name}</span>
            <StatusBadge status={g.status} />
          </>
        }
        subtitle={
          <>
            {g.description && <span>{g.description} · </span>}
            {t('groups.sensorCount', { count: g.status_counts.total })}
          </>
        }
        meta={
          <>
            {g.dependency && (
              <span>
                {t('dependency.title')}:{' '}
                {g.dependency.mode === 'device' && g.dependency.device_id !== null ? (
                  <Link
                    className="font-medium text-ink hover:underline"
                    to={`/devices/${g.dependency.device_id}`}
                  >
                    {g.dependency.device_name}
                  </Link>
                ) : (
                  <b className="font-medium text-ink">
                    {t(
                      g.dependency.mode === 'none'
                        ? 'dependency.modes.none'
                        : 'dependency.modes.inheritParent',
                    )}
                  </b>
                )}
              </span>
            )}
            <span>
              {t('snmp.label')}:{' '}
              <b className="font-medium text-ink">
                {g.snmp_credentials ? g.snmp_credentials.version : t('snmp.inherited')}
              </b>
            </span>
          </>
        }
        actions={
          isAdmin && (
            <>
              <Button onClick={() => setAddDevice(true)} variant="primary">
                <PlusIcon size={14} /> {t('devices.add')}
              </Button>
              <Button onClick={() => setAddSub(true)}>
                <PlusIcon size={14} /> {t('groups.addSub')}
              </Button>
              <AddMaintenanceButton defaults={{ scope: 'group', group_id: g.id, name: g.name }} />
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
      <StatusTiles counts={g.status_counts} />

      {node && node.children.length > 0 && (
        <Card title={t('groups.subgroups')} bodyClassName="p-0">
          <Table>
            <thead>
              <tr>
                <th className={th}>{t('common.status')}</th>
                <th className={th}>{t('common.name')}</th>
                <th className={cx(th, 'w-[40%]')}>{t('groups.sensors')}</th>
                <th className={th} />
              </tr>
            </thead>
            <tbody>
              {node.children.map((c) => (
                <tr key={c.id} className="hover:bg-surface-hover">
                  <td className={td}>
                    <StatusBadge status={c.status} />
                  </td>
                  <td className={td}>
                    <span className="flex items-center gap-2">
                      <FolderIcon size={14} className="shrink-0 text-ink-subtle" />
                      <Link
                        to={`/groups/${c.id}`}
                        className="font-semibold text-accent-ink hover:underline"
                      >
                        {c.name}
                      </Link>
                    </span>
                  </td>
                  <td className={td}>
                    <StatusBar counts={c.status_counts} thin />
                  </td>
                  <td className={cx(td, 'w-[1%] whitespace-nowrap text-end [&>span]:flex-nowrap')}>
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
                <tr key={d.id} className="hover:bg-surface-hover">
                  <td className={td}>
                    <StatusBadge status={d.status} />
                  </td>
                  <td className={td}>
                    <span className="inline-flex items-center gap-2">
                      <ServerIcon size={14} className="shrink-0 text-ink-subtle" />
                      <Link
                        to={`/devices/${d.id}`}
                        className="font-semibold text-accent-ink hover:underline"
                      >
                        {d.name}
                      </Link>
                    </span>
                    {d.in_maintenance && (
                      <span className="ms-1.5">
                        <MaintenanceBadge
                          compact
                          endsAt={d.maintenance?.ends_at}
                          name={d.maintenance?.name}
                        />
                      </span>
                    )}
                  </td>
                  <td className={`${td} font-mono text-[12.5px] text-ink-muted`}>
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
