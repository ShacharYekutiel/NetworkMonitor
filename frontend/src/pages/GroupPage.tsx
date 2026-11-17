import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { StatusCounts, TreeGroup } from '../api/types';
import { Breadcrumbs } from '../components/Breadcrumbs';
import { DeviceFormModal, GroupFormModal } from '../components/EntityForms';
import { EditIcon, FolderIcon, PlusIcon, TrashIcon } from '../components/icons';
import { DeviceTileIcon, EmptyState } from '../components/kit';
import { StatusBadge, StatusDot } from '../components/Status';
import { AddMaintenanceButton, MaintenanceBadge, useScheduleText } from '../components/Maintenance';
import { useToast } from '../components/toast';
import {
  Button,
  Card,
  ConfirmDialog,
  ErrorBox,
  GroupList,
  GroupTitle,
  KeyValue,
  ListRow,
  Loading,
  Ltr,
  PageHeader,
  TAG,
  TAG_TONE,
  cx,
} from '../components/ui';
import { useAuth } from '../hooks/auth';
import { useTree } from '../hooks/queries';
import { findGroup } from '../lib/tree';

/** Devices in a group and all of its subgroups. */
function deviceCount(g: TreeGroup): number {
  return g.devices.length + g.children.reduce((n, c) => n + deviceCount(c), 0);
}

const PROBLEM_TAGS = [
  ['down', TAG_TONE.down],
  ['warning', TAG_TONE.warning],
  ['unreachable', TAG_TONE.neutral],
] as const;

/** "1 down", "2 warnings" tags of a row (prototype group rows); nothing when all is well. */
function ProblemTags({ counts }: { counts: StatusCounts }) {
  const { t } = useTranslation();
  return (
    <>
      {PROBLEM_TAGS.filter(([k]) => counts[k] > 0).map(([k, tone]) => (
        <span key={k} className={cx(TAG, tone)}>
          {t(`groups.tag.${k}`, { count: counts[k] })}
        </span>
      ))}
    </>
  );
}

export function GroupPage() {
  const { id: idParam } = useParams();
  const id = Number(idParam);
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const toast = useToast();
  const schedule = useScheduleText();
  const group = useQuery({ queryKey: qk.group(id), queryFn: () => endpoints.group(id) });
  const devices = useQuery({
    queryKey: qk.devices({ group_id: id }),
    queryFn: () => endpoints.devices({ group_id: id }),
  });
  const windowsQuery = { scope: 'group', group_id: id };
  const windows = useQuery({
    queryKey: qk.maintenanceWindows(windowsQuery),
    queryFn: () => endpoints.maintenanceWindows(windowsQuery),
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
  const dep = g.dependency;
  const ownWindows = (windows.data?.items ?? []).filter((w) => w.enabled && w.group_id === g.id);

  const dependsOn =
    dep?.mode === 'device' && dep.device_id !== null ? (
      <Link
        className="font-display font-medium text-ink hover:underline"
        to={`/devices/${dep.device_id}`}
      >
        <Ltr>{dep.device_name}</Ltr>
      </Link>
    ) : (
      t(dep?.mode === 'none' ? 'dependency.modes.none' : 'dependency.modes.inheritParent')
    );

  return (
    <div>
      <PageHeader
        breadcrumbs={<Breadcrumbs groupId={g.parent_id} />}
        title={
          <>
            <span className="truncate">{g.name}</span>
            <StatusBadge status={g.status} />
          </>
        }
        subtitle={
          <span className="inline-flex flex-wrap items-center gap-x-1.5">
            {g.description && <span>{g.description} ·</span>}
            {node && <span>{t('dashboard.deviceCount', { count: deviceCount(node) })} ·</span>}
            <span>{t('dashboard.sensorCount', { count: g.status_counts.total })}</span>
            {dep?.mode === 'device' && dep.device_name && (
              <span>
                · {t('groups.dependsOn')} <Ltr className="font-display">{dep.device_name}</Ltr>
              </span>
            )}
          </span>
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

      {/* prototype .two: the lists at the start, the dependency card at the end */}
      <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
        <div className="grid min-w-0 gap-[26px]">
          {node && node.children.length > 0 && (
            <section aria-labelledby="grp-sub">
              <GroupTitle id="grp-sub">{t('groups.subgroups')}</GroupTitle>
              <GroupList>
                {node.children.map((c) => (
                  <ListRow
                    key={c.id}
                    testId="subgroup-row"
                    to={`/groups/${c.id}`}
                    icon={<FolderIcon size={18} strokeWidth={1.8} />}
                    title={c.name}
                    subtitle={`${t('dashboard.deviceCount', { count: deviceCount(c) })} · ${t('dashboard.sensorCount', { count: c.status_counts.total })}`}
                    end={
                      <span className="flex shrink-0 flex-wrap items-center gap-1.5">
                        <ProblemTags counts={c.status_counts} />
                        <StatusDot status={c.status} className="ms-1 h-2.5 w-2.5" />
                      </span>
                    }
                  />
                ))}
              </GroupList>
            </section>
          )}

          <section aria-labelledby="grp-dev">
            <GroupTitle id="grp-dev">{t('groups.devices')}</GroupTitle>
            {devices.isLoading && <Loading />}
            <ErrorBox error={devices.error} />
            {devices.data && !devices.data.items.length && (
              <Card>
                <EmptyState
                  icon={FolderIcon}
                  title={t('devices.none')}
                  actions={
                    isAdmin && (
                      <Button variant="primary" onClick={() => setAddDevice(true)}>
                        <PlusIcon size={14} /> {t('devices.add')}
                      </Button>
                    )
                  }
                />
              </Card>
            )}
            {devices.data && devices.data.items.length > 0 && (
              <GroupList>
                {devices.data.items.map((d) => {
                  const model = [d.identity?.vendor, d.identity?.model].filter(Boolean).join(' ');
                  return (
                    <ListRow
                      key={d.id}
                      testId="device-row"
                      to={`/devices/${d.id}`}
                      icon={<DeviceTileIcon cls={d.effective_class} name={d.name} />}
                      title={
                        <bdi dir="ltr" className="font-display">
                          {d.name}
                        </bdi>
                      }
                      subtitle={
                        <bdi dir="ltr" className="font-display">
                          {model ? `${model} · ${d.host}` : d.host}
                        </bdi>
                      }
                      end={
                        <span className="flex shrink-0 flex-wrap items-center gap-1.5">
                          {d.in_maintenance && (
                            <MaintenanceBadge
                              compact
                              endsAt={d.maintenance?.ends_at}
                              name={d.maintenance?.name}
                            />
                          )}
                          <ProblemTags counts={d.status_counts} />
                          <StatusDot status={d.status} className="ms-1 h-2.5 w-2.5" />
                        </span>
                      }
                    />
                  );
                })}
              </GroupList>
            )}
          </section>
        </div>

        <Card
          title={t('dependency.title')}
          className="xl:mt-[50px]"
          actions={
            isAdmin && (
              <Button size="sm" onClick={() => setEditing(true)}>
                <EditIcon size={13} /> {t('common.edit')}
              </Button>
            )
          }
        >
          <p className="m-0 mb-4 text-[13.5px] text-ink-muted">{t('dependency.groupHint')}</p>
          <KeyValue
            items={[
              { k: t('groups.dependsOn'), v: dependsOn, testId: 'group-dependency' },
              ...(dep?.mode === 'device'
                ? [{ k: t('dependency.sensor'), v: dep.sensor_name ?? t('dependency.sensorAuto') }]
                : []),
              {
                k: t('groups.maintenance'),
                v: ownWindows.length ? (
                  <span className="grid gap-1">
                    {ownWindows.map((w) => (
                      <span key={w.id}>{schedule(w)}</span>
                    ))}
                  </span>
                ) : (
                  '—'
                ),
              },
              {
                k: t('snmp.label'),
                v: g.snmp_credentials ? g.snmp_credentials.version : t('snmp.inherited'),
              },
            ]}
          />
        </Card>
      </div>

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
