import { useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router';
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
import { SensorTable, useSensorColumns } from '../components/SensorTable';
import { ColumnsMenu } from '../components/kitTable';
import { StatusBadge, StatusBar, StatusCountPills, StatusDot } from '../components/Status';
import { useToast } from '../components/toast';
import {
  Button,
  Card,
  ConfirmDialog,
  ErrorBox,
  Loading,
  Ltr,
  PageHeader,
  SEG_TRACK,
  tabClass,
} from '../components/ui';
import { useAuth } from '../hooks/auth';
import { errorMessage } from '../lib/errors';
import { EffectiveDependencyText, ParentDownBanner } from '../components/Dependency';
import { AddMaintenanceButton, MaintenanceBadge } from '../components/Maintenance';
import { IdentityView } from '../components/discovery/Fingerprint';
import { useDeviceTemplates } from '../components/Templates';
import { FingerprintIcon } from '../components/icons';
import type { DeviceIdentity, Sensor } from '../api/types';
import { BulkImpactModal } from '../components/SensorImpact';
import { BulkDeleteModal } from '../components/SensorBulkDelete';
import { BulkSeverityModal } from '../components/severity/BulkSeverity';
import { BulkNoiseModal, NoiseOverrideCard } from '../components/noise/NoiseOverride';
import { useEntityMenu } from '../components/treemenu/TreeContextMenu';
import { l10n } from '../lib/templates';
import { PortsTab } from '../components/ports/PortsTab';
import { PortIcon } from '../components/icons';
import { cx } from '../components/ui';
import { useSensorTypes } from '../hooks/queries';
import { useFeature, useFeatures } from '../hooks/features';
import { ClockIcon, LogsIcon } from '../components/icons';
import { TimelineTab } from '../components/timeline/TimelineTab';
import { SyslogView } from '../components/logs/SyslogView';
import { TrapsView } from '../components/logs/TrapsView';
import { ConfigTab } from '../components/configbackup/ConfigTab';
import { NewDevicesBadge } from '../components/newdevices/NewDevicesBadge';
import { SnmpSourceLine } from '../components/SnmpSource';

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
        <div className="inline-flex gap-0.5 rounded-[11px] bg-chip p-[3px]" role="group">
          {(['syslog', 'traps'] as const).map((x) => (
            <button
              key={x}
              type="button"
              aria-pressed={k === x}
              onClick={() => setKind(x)}
              className={cx(
                'h-7 rounded-[8px] px-3 text-[13px] font-medium transition-colors',
                k === x
                  ? 'bg-surface text-ink shadow-[0_1px_3px_rgba(0,0,0,0.12)]'
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

/** Device "Sensors" card; [1.3] admins multi-select rows for the bulk "Set impact…" action. */
export function DeviceSensorsCard({
  deviceId,
  sensors,
  loading,
  error,
  isAdmin,
}: {
  deviceId: number;
  sensors: Sensor[] | undefined;
  loading: boolean;
  error: unknown;
  isAdmin: boolean;
}) {
  const { t } = useTranslation();
  const [picked, setPicked] = useState<Set<number>>(() => new Set());
  const [bulk, setBulk] = useState(false);
  const [bulkDelete, setBulkDelete] = useState(false);
  const [bulkSeverity, setBulkSeverity] = useState(false);
  const [bulkNoise, setBulkNoise] = useState(false); // [if-noise]
  const severityOn = useFeature('alert_severity'); // [severity]
  // Only sensors still in the list count (sensors may be deleted while selected).
  const pickedSensors = (sensors ?? []).filter((s) => picked.has(s.id));
  const columns = useSensorColumns({
    sensors: sensors ?? [],
    selectable: isAdmin,
    withActions: isAdmin,
  });
  const ids = pickedSensors.map((s) => s.id);
  return (
    <div>
      {sensors && sensors.length > 0 && (
        <div className="mx-1 mb-2.5 flex flex-wrap items-center justify-between gap-2">
          <p className="m-0 min-w-0 flex-1 text-[13.5px] text-ink-muted">
            {isAdmin && sensors.length > 1 ? t('impact.selectHint') : null}
          </p>
          <ColumnsMenu
            layout={columns}
            labelOf={(id) => columns.columns.find((c) => c.id === id)?.label ?? id}
            testId="sensor-columns"
          />
        </div>
      )}
      <Card title={t('devices.sensors')} bodyClassName="p-0">
        {loading && <Loading />}
        <ErrorBox error={error} />
        {sensors && (
          <SensorTable
            sensors={sensors}
            layout={columns}
            actions={isAdmin ? (s) => <SensorRowActions sensor={s} /> : undefined}
            selection={isAdmin ? { selected: picked, onChange: setPicked } : undefined}
          />
        )}
      </Card>
      {/* prototype .bulkbar: a floating ink pill while rows are selected */}
      {isAdmin && ids.length > 0 && (
        <div
          className="sticky bottom-[18px] z-20 mt-3.5 flex flex-wrap items-center gap-2.5 rounded-full bg-accent py-2.5 pe-2.5 ps-[18px] text-accent-on shadow-[0_14px_40px_rgba(0,0,0,0.2)] motion-safe:animate-[nm-pop-in_.3s_cubic-bezier(.32,.72,0,1)]"
          data-testid="sensor-bulk-bar"
          role="region"
          aria-label={t('impact.bulkBar')}
        >
          <span className="min-w-0 flex-1 font-medium" aria-live="polite">
            {t('impact.selected', { count: ids.length })}
          </span>
          <button
            type="button"
            onClick={() => setBulk(true)}
            className="h-[34px] rounded-full bg-accent-on px-3.5 text-[13.5px] font-medium text-accent"
          >
            {t('impact.setImpact')}
          </button>
          {severityOn && (
            <button
              type="button"
              onClick={() => setBulkSeverity(true)}
              data-testid="sensor-bulk-severity"
              className="h-[34px] rounded-full bg-accent-on px-3.5 text-[13.5px] font-medium text-accent"
            >
              {t('severity.bulk.action')}
            </button>
          )}
          <button
            type="button"
            onClick={() => setBulkNoise(true)}
            data-testid="sensor-bulk-noise"
            className="h-[34px] rounded-full bg-accent-on px-3.5 text-[13.5px] font-medium text-accent"
          >
            {t('noiseOv.bulk.action')}
          </button>
          <button
            type="button"
            onClick={() => setBulkDelete(true)}
            data-testid="sensor-bulk-delete"
            className="inline-flex h-[34px] items-center gap-1.5 rounded-full bg-status-down px-3.5 text-[13.5px] font-medium text-on-status"
          >
            <TrashIcon size={14} />
            {t('sensorBulk.delete')}
          </button>
          <button
            type="button"
            onClick={() => setPicked(new Set())}
            className="h-[34px] rounded-full bg-[color-mix(in_srgb,var(--on-accent)_16%,transparent)] px-3.5 text-[13.5px] font-medium"
          >
            {t('impact.clearSelection')}
          </button>
        </div>
      )}
      {isAdmin && (
        <BulkImpactModal
          open={bulk}
          onClose={() => setBulk(false)}
          sensorIds={ids}
          deviceId={deviceId}
          onDone={() => setPicked(new Set())}
        />
      )}
      {isAdmin && severityOn && (
        <BulkSeverityModal
          open={bulkSeverity}
          onClose={() => setBulkSeverity(false)}
          sensorIds={ids}
          deviceId={deviceId}
          onDone={() => setPicked(new Set())}
        />
      )}
      {isAdmin && (
        <BulkNoiseModal
          open={bulkNoise}
          onClose={() => setBulkNoise(false)}
          sensorIds={ids}
          deviceId={deviceId}
          onDone={() => setPicked(new Set())}
        />
      )}
      {isAdmin && (
        <BulkDeleteModal
          open={bulkDelete}
          onClose={() => setBulkDelete(false)}
          sensors={pickedSensors}
          deviceId={deviceId}
          onDone={() => setPicked(new Set())}
        />
      )}
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
  const menu = useEntityMenu();

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
  // [3b] assistant links use `tab=configuration` (architecture § 14.3.7)
  const rawTab = params.get('tab');
  const tabParam = (rawTab === 'configuration' ? 'config' : rawTab) as DeviceTab | null;
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
            <StatusDot status={d.status} className="h-3 w-3" />{' '}
            <bdi
              {...menu({ kind: 'device', id: d.id })}
              dir="ltr"
              className="[font-family:var(--font-display)]"
              data-testid="device-title"
            >
              {d.name}
            </bdi>
            {d.in_maintenance && (
              <MaintenanceBadge endsAt={d.maintenance?.ends_at} name={d.maintenance?.name} />
            )}
          </>
        }
        subtitle={
          <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1">
            {(identity?.vendor || identity?.model) && (
              <bdi dir="ltr">{[identity?.vendor, identity?.model].filter(Boolean).join(' ')}</bdi>
            )}
            {(identity?.vendor || identity?.model) && <span aria-hidden="true">·</span>}
            <Ltr className="num">{d.host}</Ltr>
            {d.description && <span>· {d.description}</span>}
            {d.status !== 'up' && <StatusBadge status={d.status} />}
          </span>
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
          <SnmpSourceLine device={d} sensors={sensors.data?.items ?? []} />
        </div>
        <div className="mt-3">
          <StatusBar counts={d.status_counts} />
        </div>
        <p className="mt-3 text-xs text-ink-muted">
          <span className="font-medium text-ink-muted">{t('dependency.parent')}:</span>{' '}
          <EffectiveDependencyText effective={d.effective_dependency ?? null} bare />
        </p>
      </Card>
      {/* [if-noise] the device (or its groups) override the event-log noise protection */}
      <NoiseOverrideCard
        effective={d.effective_event_noise}
        level="device"
        selfId={d.id}
        onEdit={isAdmin ? () => setEditing(true) : undefined}
      />
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
        <div role="tablist" aria-label={t('ports.tabs')} className={cx(SEG_TRACK, 'flex')}>
          {tabs.map((k) => (
            <button
              key={k}
              type="button"
              role="tab"
              aria-selected={tab === k}
              onClick={() => setTab(k)}
              className={tabClass(tab === k)}
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
          <DeviceSensorsCard
            deviceId={d.id}
            sensors={sensors.data?.items}
            loading={sensors.isLoading}
            error={sensors.error}
            isAdmin={isAdmin}
          />
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
