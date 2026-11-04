import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import { Breadcrumbs } from '../components/Breadcrumbs';
import { DeviceFormModal } from '../components/EntityForms';
import { EventsTable } from '../components/EventList';
import { EditIcon, PauseIcon, PlayIcon, PlusIcon, TrashIcon } from '../components/icons';
import { AddSensorModal } from '../components/SensorForms';
import { SensorRowActions } from '../components/SensorRowActions';
import { SensorTable } from '../components/SensorTable';
import { StatusBadge, StatusBar, StatusCountPills, StatusDot } from '../components/Status';
import { useToast } from '../components/toast';
import { Button, Card, ConfirmDialog, ErrorBox, Loading, Ltr, PageHeader } from '../components/ui';
import { useAuth } from '../hooks/auth';
import { errorMessage } from '../lib/errors';

export function DevicePage() {
  const { id: idParam } = useParams();
  const id = Number(idParam);
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const toast = useToast();

  const device = useQuery({ queryKey: qk.device(id), queryFn: () => endpoints.device(id) });
  const sensors = useQuery({
    queryKey: qk.sensors({ device_id: id }),
    queryFn: () => endpoints.sensors({ device_id: id }),
  });
  const events = useQuery({
    queryKey: qk.events({ device_id: id, page_size: 20 }),
    queryFn: () => endpoints.events({ device_id: id, page_size: 20 }),
  });

  const [editing, setEditing] = useState(false);
  const [adding, setAdding] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const toggle = useMutation({
    mutationFn: (enabled: boolean) => endpoints.updateDevice(id, { enabled }),
    onSuccess: (d) => {
      qc.setQueryData(qk.device(id), d);
      void qc.invalidateQueries({ queryKey: ['sensors'] });
      void qc.invalidateQueries({ queryKey: qk.tree });
    },
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  const del = useMutation({
    mutationFn: () => endpoints.deleteDevice(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.tree });
      void qc.invalidateQueries({ queryKey: qk.dashboard });
      qc.removeQueries({ queryKey: qk.device(id) });
      toast.push('success', t('common.deleted'));
      navigate(device.data ? `/groups/${device.data.group_id}` : '/');
    },
  });

  if (device.isLoading) return <Loading />;
  if (device.error || !device.data) return <ErrorBox error={device.error} />;
  const d = device.data;

  return (
    <div className="space-y-4">
      <PageHeader
        breadcrumbs={<Breadcrumbs groupId={d.group_id} />}
        title={
          <>
            <StatusDot status={d.status} className="h-3 w-3" /> {d.name}
          </>
        }
        subtitle={
          <>
            <Ltr>{d.host}</Ltr>
            {d.description && <span className="ms-2">· {d.description}</span>}
          </>
        }
        actions={
          isAdmin && (
            <>
              <Button variant="primary" onClick={() => setAdding(true)}>
                <PlusIcon size={14} /> {t('sensors.add')}
              </Button>
              <Button busy={toggle.isPending} onClick={() => toggle.mutate(!d.enabled)}>
                {d.enabled ? (
                  <PauseIcon size={14} />
                ) : (
                  <PlayIcon size={14} className="rtl:rotate-180" />
                )}
                {d.enabled ? t('devices.pause') : t('devices.resume')}
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
        <div className="flex flex-wrap items-center gap-4 text-sm">
          <StatusBadge status={d.status} />
          {!d.enabled && <span className="text-slate-500">{t('devices.disabledNote')}</span>}
          <StatusCountPills counts={d.status_counts} />
          <span className="text-xs text-slate-500">
            {t('snmp.label')}: {t(`snmp.source.${d.effective_snmp_source}`)}
            {d.snmp_credentials && ` (${d.snmp_credentials.version})`}
          </span>
        </div>
        <div className="mt-3">
          <StatusBar counts={d.status_counts} />
        </div>
      </Card>
      <Card title={t('devices.sensors')} bodyClassName="p-0">
        {sensors.isLoading && <Loading />}
        <ErrorBox error={sensors.error} />
        {sensors.data && (
          <SensorTable
            sensors={sensors.data.items}
            actions={isAdmin ? (s) => <SensorRowActions sensor={s} /> : undefined}
          />
        )}
      </Card>
      <Card title={t('devices.recentEvents')} bodyClassName="p-0">
        {events.data ? <EventsTable events={events.data.items} /> : <Loading />}
      </Card>

      <DeviceFormModal open={editing} onClose={() => setEditing(false)} device={d} />
      {isAdmin && <AddSensorModal open={adding} onClose={() => setAdding(false)} device={d} />}
      <ConfirmDialog
        open={deleting}
        title={t('devices.deleteTitle')}
        message={t('devices.deleteConfirm', { name: d.name })}
        onConfirm={() => del.mutate()}
        onClose={() => setDeleting(false)}
        busy={del.isPending}
        error={del.error}
      />
    </div>
  );
}
