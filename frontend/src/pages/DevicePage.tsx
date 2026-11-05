import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import { Breadcrumbs } from '../components/Breadcrumbs';
import { DeviceFormModal } from '../components/EntityForms';
import { EventsTable } from '../components/EventList';
import {
  EditIcon,
  LayersIcon,
  PauseIcon,
  PlayIcon,
  PlusIcon,
  TrashIcon,
} from '../components/icons';
import { ApplyTemplateModal } from '../components/Templates';
import { AddSensorModal } from '../components/SensorForms';
import { SensorRowActions } from '../components/SensorRowActions';
import { SensorTable } from '../components/SensorTable';
import { StatusBadge, StatusBar, StatusCountPills, StatusDot } from '../components/Status';
import { useToast } from '../components/toast';
import { Button, Card, ConfirmDialog, ErrorBox, Loading, Ltr, PageHeader } from '../components/ui';
import { useAuth } from '../hooks/auth';
import { errorMessage } from '../lib/errors';
import { EffectiveDependencyText, ParentDownBanner } from '../components/Dependency';
import { AddMaintenanceButton, MaintenanceBadge } from '../components/Maintenance';
import { IdentityView } from '../components/discovery/Fingerprint';
import { useDeviceTemplates } from '../components/Templates';
import { FingerprintIcon } from '../components/icons';
import type { DeviceIdentity } from '../api/types';
import { l10n } from '../lib/templates';

export function DevicePage() {
  const { id: idParam } = useParams();
  const id = Number(idParam);
  const { t, i18n } = useTranslation();
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
  const [applying, setApplying] = useState(false);
  const [applyTpl, setApplyTpl] = useState<string | undefined>(undefined);
  const [identified, setIdentified] = useState<DeviceIdentity | null>(null);
  const templates = useDeviceTemplates();
  const identify = useMutation({
    mutationFn: () => endpoints.identifyDevice(id),
    onSuccess: (r) => {
      setIdentified(r);
      qc.setQueryData(qk.device(id), (old: typeof device.data) =>
        old ? { ...old, identity: r } : old,
      );
    },
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });

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
  const identity = identified ?? d.identity;
  const tplName = (tid: string) => {
    const tp = templates.data?.find((x) => x.id === tid);
    return tp ? l10n(tp.name, i18n.language) : tid;
  };

  return (
    <div className="space-y-4">
      <PageHeader
        breadcrumbs={<Breadcrumbs groupId={d.group_id} />}
        title={
          <>
            <StatusDot status={d.status} className="h-3 w-3" /> {d.name}
            {d.in_maintenance && (
              <MaintenanceBadge endsAt={d.maintenance?.ends_at} name={d.maintenance?.name} />
            )}
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
              <Button
                onClick={() => {
                  setApplyTpl(undefined);
                  setApplying(true);
                }}
              >
                <LayersIcon size={14} /> {t('templates.apply')}
              </Button>
              <Button busy={identify.isPending} onClick={() => identify.mutate()}>
                <FingerprintIcon size={14} /> {t('identify.action')}
              </Button>
              <AddMaintenanceButton defaults={{ scope: 'device', device_id: d.id, name: d.name }} />
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
      {d.effective_dependency?.parent_down && (
        <ParentDownBanner effective={d.effective_dependency} />
      )}
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
        <p className="mt-3 text-xs text-slate-600 dark:text-slate-300">
          <span className="font-medium text-slate-500">{t('dependency.parent')}:</span>{' '}
          <EffectiveDependencyText effective={d.effective_dependency ?? null} />
        </p>
      </Card>
      {(identity || identify.isPending) && (
        <Card
          title={t('identify.title')}
          actions={
            isAdmin &&
            identity && (
              <Button
                size="sm"
                variant="primary"
                onClick={() => {
                  setApplyTpl(identity.suggested_template);
                  setApplying(true);
                }}
              >
                <LayersIcon size={12} /> {t('identify.applySuggested')}
              </Button>
            )
          }
        >
          {identify.isPending ? (
            <p className="flex items-center gap-2 text-sm text-slate-500">
              <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-sky-600 border-t-transparent" />
              {t('identify.running')}
            </p>
          ) : (
            identity && (
              <IdentityView
                identity={identity}
                templateName={tplName(identity.suggested_template)}
              />
            )
          )}
        </Card>
      )}
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
      {isAdmin && (
        <ApplyTemplateModal
          open={applying}
          onClose={() => setApplying(false)}
          device={d}
          initialTemplate={applyTpl}
        />
      )}
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
