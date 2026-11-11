import { useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
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
import { PortsTab } from '../components/ports/PortsTab';
import { PortIcon } from '../components/icons';
import { cx } from '../components/ui';
import { useSensorTypes } from '../hooks/queries';
import { useFeatures } from '../hooks/features';
import { ClockIcon, LogsIcon } from '../components/icons';
import { TimelineTab } from '../components/timeline/TimelineTab';
import { SyslogView } from '../components/logs/SyslogView';
import { TrapsView } from '../components/logs/TrapsView';
import { ConfigTab } from '../components/configbackup/ConfigTab';
import { NewDevicesBadge } from '../components/newdevices/NewDevicesBadge';

type DeviceTab = 'ports' | 'sensors' | 'logs' | 'timeline' | 'config';

/** [2b-3] Device "Logs" tab: syslog / traps tables pre-filtered by the device. */
function DeviceLogs({ deviceId }: { deviceId: number }) {
  const { t } = useTranslation();
  const { features } = useFeatures();
  const [kind, setKind] = useState<'syslog' | 'traps'>(features.syslog ? 'syslog' : 'traps');
  const k =
    kind === 'syslog' && !features.syslog
      ? 'traps'
      : kind === 'traps' && !features.traps
        ? 'syslog'
        : kind;
  return (
    <div className="space-y-3">
      {features.syslog && features.traps && (
        <div
          className="inline-flex gap-0.5 rounded-md border border-line-strong bg-surface-sunken p-0.5"
          role="group"
        >
          {(['syslog', 'traps'] as const).map((x) => (
            <button
              key={x}
              type="button"
              aria-pressed={k === x}
              onClick={() => setKind(x)}
              className={cx(
                'h-6 rounded-sm px-2.5 text-xs font-medium',
                k === x
                  ? 'bg-surface-raised text-ink ring-1 ring-line-strong'
                  : 'text-ink-muted hover:text-ink',
              )}
            >
              {t(`logs.tab.${x}`)}
            </button>
          ))}
        </div>
      )}
      {k === 'syslog' ? <SyslogView deviceId={deviceId} /> : <TrapsView deviceId={deviceId} />}
    </div>
  );
}

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
  const [params, setParams] = useSearchParams();
  const sensorTypes = useSensorTypes();
  const templates = useDeviceTemplates();
  const { features } = useFeatures();
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
  // [2b-2] Ports tab: every device with an SNMP sensor; default when the inventory is effective.
  const snmpTypes = new Set(
    sensorTypes.data?.filter((x) => x.requires_snmp).map((x) => x.type) ??
      sensors.data?.items.filter((x) => x.type.startsWith('snmp_')).map((x) => x.type) ??
      [],
  );
  const hasPorts = d.ports_effective || !!sensors.data?.items.some((x) => snmpTypes.has(x.type));
  const tabParam = params.get('tab') as DeviceTab | null;
  const tabs: DeviceTab[] = [
    ...(hasPorts ? (['ports'] as const) : []),
    'sensors',
    ...(features.traps || features.syslog ? (['logs'] as const) : []),
    ...(features.timeline ? (['timeline'] as const) : []),
    ...(features.config_backup ? (['config'] as const) : []),
  ];
  const tab: DeviceTab =
    tabParam && tabs.includes(tabParam)
      ? tabParam
      : hasPorts && d.ports_effective
        ? 'ports'
        : 'sensors';
  const setTab = (next: DeviceTab) =>
    setParams(
      (prev) => {
        const p = new URLSearchParams();
        p.set('tab', next);
        if (next === 'ports' && prev.get('view')) p.set('view', prev.get('view') as string);
        return p;
      },
      { replace: true },
    );
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
          {!d.enabled && <span className="text-ink-muted">{t('devices.disabledNote')}</span>}
          <StatusCountPills counts={d.status_counts} />
          <NewDevicesBadge device={d} />
          <span className="text-xs text-ink-muted">
            {t('snmp.label')}: {t(`snmp.source.${d.effective_snmp_source}`)}
            {d.snmp_credentials && ` (${d.snmp_credentials.version})`}
          </span>
        </div>
        <div className="mt-3">
          <StatusBar counts={d.status_counts} />
        </div>
        <p className="mt-3 text-xs text-ink-muted">
          <span className="font-medium text-ink-muted">{t('dependency.parent')}:</span>{' '}
          <EffectiveDependencyText effective={d.effective_dependency ?? null} bare />
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
            <p className="flex items-center gap-2 text-sm text-ink-muted">
              <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-accent border-t-transparent" />
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
      {tabs.length > 1 && (
        <div
          role="tablist"
          aria-label={t('ports.tabs')}
          className="flex overflow-x-auto border-b border-line"
        >
          {tabs.map((k) => (
            <button
              key={k}
              type="button"
              role="tab"
              aria-selected={tab === k}
              onClick={() => setTab(k)}
              className={cx(
                '-mb-px inline-flex items-center gap-2 whitespace-nowrap border-b-2 px-3.5 py-2.5 text-sm leading-4',
                tab === k
                  ? 'border-accent-ink font-semibold text-ink'
                  : 'border-transparent font-medium text-ink-muted hover:text-ink',
              )}
            >
              {k === 'ports' && <PortIcon size={15} />}
              {k === 'logs' && <LogsIcon size={15} />}
              {k === 'timeline' && <ClockIcon size={15} />}
              {k === 'config' && <LayersIcon size={15} />}
              {t(`devices.tab.${k}`)}
            </button>
          ))}
        </div>
      )}
      {tab === 'logs' && <DeviceLogs deviceId={d.id} />}
      {tab === 'timeline' && <TimelineTab deviceId={d.id} />}
      {tab === 'config' && <ConfigTab device={d} />}
      {tab === 'ports' && <PortsTab device={d} />}
      {tab === 'sensors' && (
        <>
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
        </>
      )}

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
