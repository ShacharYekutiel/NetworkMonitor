import { useFeatures } from '../hooks/features';
import { isIp, parseList } from '../lib/logs';
import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk, type DeviceInput, type GroupInput } from '../api/endpoints';
import { INVENTORY_MODES } from '../api/types';
import type { Device, DependencyInput, Group, InventoryMode, SensorType } from '../api/types';
import { useTree } from '../hooks/queries';
import { snmpFormFrom, snmpFormToInput } from '../lib/snmp';
import { descendantGroupIds, findGroup, flattenGroups } from '../lib/tree';
import { SnmpEditor } from './SnmpEditor';
import { SshEditor } from './configbackup/SshEditor';
import { sshFormErrors, sshFormFrom, sshFormToInput } from '../lib/configBackup';
import type { BackupMode } from '../api/types3';
import {
  DependencyEditor,
  dependencyInputFrom,
  dependencyToInput,
  useDeviceNames,
} from './Dependency';
import { useToast } from './toast';
import { Button, Checkbox, ErrorBox, Field, Input, Modal, Select, Textarea } from './ui';

export function GroupSelect({
  value,
  onChange,
  exclude = [],
  allowRoot,
  rootLabel,
}: {
  value: number | null;
  onChange: (v: number | null) => void;
  exclude?: number[];
  allowRoot?: boolean;
  /** label of the empty option (default: "none — top level") */
  rootLabel?: string;
}) {
  const { t } = useTranslation();
  const { data } = useTree();
  const opts = flattenGroups(data?.groups ?? []).filter((x) => !exclude.includes(x.group.id));
  return (
    <Select
      value={value === null ? '' : String(value)}
      onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))}
    >
      {allowRoot && <option value="">{rootLabel ?? t('groups.noParent')}</option>}
      {opts.map(({ group, depth }) => (
        <option key={group.id} value={group.id}>
          {'  '.repeat(depth)}
          {group.name}
        </option>
      ))}
    </Select>
  );
}

export function GroupFormModal({
  open,
  onClose,
  group,
  parentId,
}: {
  open: boolean;
  onClose: () => void;
  group?: Group;
  parentId?: number | null;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const { data: tree } = useTree();
  const [name, setName] = useState('');
  const [parent, setParent] = useState<number | null>(null);
  const [description, setDescription] = useState('');
  const [sortOrder, setSortOrder] = useState('0');
  const [snmp, setSnmp] = useState(snmpFormFrom(null));
  const [dep, setDep] = useState<DependencyInput>(dependencyInputFrom(null));
  const [ssh, setSsh] = useState(sshFormFrom(null));
  const deviceName = useDeviceNames();

  useEffect(() => {
    if (!open) return;
    setSsh(sshFormFrom(group?.ssh_credentials));
    setDep(dependencyInputFrom(group?.dependency));
    setName(group?.name ?? '');
    setParent(group ? group.parent_id : (parentId ?? null));
    setDescription(group?.description ?? '');
    setSortOrder(String(group?.sort_order ?? 0));
    setSnmp(snmpFormFrom(group?.snmp_credentials ?? null));
  }, [open, group, parentId]);

  const exclude = useMemo(() => {
    if (!group || !tree) return [];
    const g = findGroup(tree.groups, group.id);
    return g ? descendantGroupIds(g) : [group.id];
  }, [group, tree]);

  const m = useMutation({
    mutationFn: (body: GroupInput) =>
      group ? endpoints.updateGroup(group.id, body) : endpoints.createGroup(body),
    onSuccess: (g) => {
      qc.setQueryData(qk.group(g.id), g);
      void qc.invalidateQueries({ queryKey: qk.tree });
      void qc.invalidateQueries({ queryKey: ['groups'] });
      toast.push('success', t('common.saved'));
      onClose();
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (sshFormErrors(ssh, group?.ssh_credentials).length) return;
    const body: GroupInput = {
      name: name.trim(),
      parent_id: parent,
      description,
      sort_order: Number(sortOrder) || 0,
      snmp_credentials: snmpFormToInput(snmp),
      dependency: dependencyToInput(dep),
    };
    // [3] sent when set or when an existing value is removed (inherit)
    if (ssh.own || group?.ssh_credentials) body.ssh_credentials = sshFormToInput(ssh);
    m.mutate(body);
  };

  return (
    <Modal open={open} onClose={onClose} title={group ? t('groups.edit') : t('groups.add')} wide>
      <form onSubmit={submit} className="space-y-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label={t('common.name')} required>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              maxLength={128}
              autoFocus
            />
          </Field>
          <Field label={t('groups.parent')}>
            <GroupSelect value={parent} onChange={setParent} exclude={exclude} allowRoot />
          </Field>
          <Field label={t('common.description')} className="sm:col-span-2">
            <Textarea
              rows={2}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </Field>
          <Field label={t('common.sortOrder')}>
            <Input
              type="number"
              value={sortOrder}
              onChange={(e) => setSortOrder(e.target.value)}
              dir="ltr"
            />
          </Field>
        </div>
        <SnmpEditor
          value={snmp}
          onChange={setSnmp}
          existing={group?.snmp_credentials ?? null}
          inheritLabel={t('snmp.inheritParent')}
        />
        <SshEditor
          value={ssh}
          onChange={setSsh}
          existing={group?.ssh_credentials}
          inheritLabel={t('ssh.inheritParent')}
        />
        <DependencyEditor value={dep} onChange={setDep} forGroup />
        <ErrorBox error={m.error} context={{ deviceName }} />
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button
            type="submit"
            variant="primary"
            busy={m.isPending}
            disabled={dep.mode === 'device' && dep.device_id === null}
          >
            {t('common.save')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

const AUTO_SENSORS: SensorType[] = ['ping', 'snmp_system', 'snmp_cpu', 'snmp_memory'];

export function DeviceFormModal({
  open,
  onClose,
  device,
  groupId,
  onCreated,
  initial,
}: {
  open: boolean;
  onClose: () => void;
  device?: Device;
  groupId?: number;
  onCreated?: (d: Device) => void;
  /** [2b-1] prefill for a new device (e.g. "Add as device" from the topology map) */
  initial?: { name?: string; host?: string; description?: string };
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const { data: tree } = useTree();
  const [group, setGroup] = useState<number | null>(null);
  const [name, setName] = useState('');
  const [host, setHost] = useState('');
  const [description, setDescription] = useState('');
  const [enabled, setEnabled] = useState(true);
  const [sortOrder, setSortOrder] = useState('0');
  const [snmp, setSnmp] = useState(snmpFormFrom(null));
  const [auto, setAuto] = useState<SensorType[]>(['ping']);
  const [dep, setDep] = useState<DependencyInput>(dependencyInputFrom(null));
  const [portInv, setPortInv] = useState<InventoryMode>('auto');
  const [arp, setArp] = useState<InventoryMode>('auto');
  const [sources, setSources] = useState('');
  const [ssh, setSsh] = useState(sshFormFrom(null));
  const [backupMode, setBackupMode] = useState<BackupMode>('auto');
  const [driver, setDriver] = useState('');
  const { features } = useFeatures();
  const showSources = features.traps || features.syslog;
  const srcList = parseList(sources);
  const srcErr = srcList.length > 16 || srcList.some((x) => !isIp(x));
  const deviceName = useDeviceNames();

  useEffect(() => {
    if (!open) return;
    setGroup(device?.group_id ?? groupId ?? tree?.groups[0]?.id ?? null);
    setName(device?.name ?? initial?.name ?? '');
    setHost(device?.host ?? initial?.host ?? '');
    setDescription(device?.description ?? initial?.description ?? '');
    setDep(dependencyInputFrom(device?.dependency));
    setEnabled(device?.enabled ?? true);
    setSortOrder(String(device?.sort_order ?? 0));
    setSnmp(snmpFormFrom(device?.snmp_credentials ?? null));
    setAuto(['ping']);
    setPortInv(device?.port_inventory ?? 'auto');
    setArp(device?.arp_collect ?? 'auto');
    setSources((device?.source_addresses ?? []).join('\n'));
    setSsh(sshFormFrom(device?.ssh_credentials));
    setBackupMode(device?.config_backup ?? 'auto');
    setDriver(device?.config_backup_driver ?? '');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, device, groupId]);

  const m = useMutation({
    mutationFn: (body: DeviceInput) =>
      device ? endpoints.updateDevice(device.id, body) : endpoints.createDevice(body),
    onSuccess: (d) => {
      qc.setQueryData(qk.device(d.id), d);
      void qc.invalidateQueries({ queryKey: qk.tree });
      void qc.invalidateQueries({ queryKey: ['devices'] });
      void qc.invalidateQueries({ queryKey: ['sensors'] });
      toast.push('success', t('common.saved'));
      onClose();
      if (!device) onCreated?.(d);
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (group === null) return;
    const body: DeviceInput = {
      group_id: group,
      name: name.trim(),
      host: host.trim(),
      description,
      enabled,
      sort_order: Number(sortOrder) || 0,
      snmp_credentials: snmpFormToInput(snmp),
      dependency: dependencyToInput(dep),
    };
    // [2b-2] Sent only when changed (a change makes the device due for a port run at once).
    if (portInv !== (device?.port_inventory ?? 'auto')) body.port_inventory = portInv;
    if (arp !== (device?.arp_collect ?? 'auto')) body.arp_collect = arp;
    if (!device) body.auto_sensors = auto;
    // [3] SSH credentials / backup mode / driver: sent only when set or changed
    if (sshFormErrors(ssh, device?.ssh_credentials).length) return;
    if (ssh.own || device?.ssh_credentials) body.ssh_credentials = sshFormToInput(ssh);
    if (backupMode !== (device?.config_backup ?? 'auto')) body.config_backup = backupMode;
    if ((driver || null) !== (device?.config_backup_driver ?? null))
      body.config_backup_driver = driver || null;
    // [2b-3] sent only while traps/syslog are on (the field is hidden otherwise)
    if (showSources) {
      if (srcErr) return;
      body.source_addresses = srcList;
    }
    m.mutate(body);
  };

  return (
    <Modal open={open} onClose={onClose} title={device ? t('devices.edit') : t('devices.add')} wide>
      <form onSubmit={submit} className="space-y-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label={t('common.name')} required>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              maxLength={128}
              autoFocus
            />
          </Field>
          <Field label={t('devices.host')} hint={t('devices.hostHint')} required>
            <Input
              value={host}
              onChange={(e) => setHost(e.target.value)}
              required
              maxLength={255}
              dir="ltr"
            />
          </Field>
          <Field label={t('devices.group')} required>
            <GroupSelect value={group} onChange={setGroup} />
          </Field>
          <Field label={t('common.sortOrder')}>
            <Input
              type="number"
              value={sortOrder}
              onChange={(e) => setSortOrder(e.target.value)}
              dir="ltr"
            />
          </Field>
          <Field label={t('common.description')} className="sm:col-span-2">
            <Textarea
              rows={2}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </Field>
          <div className="sm:col-span-2">
            <Checkbox label={t('devices.enabled')} checked={enabled} onChange={setEnabled} />
          </div>
        </div>
        <SnmpEditor
          value={snmp}
          onChange={setSnmp}
          existing={device?.snmp_credentials ?? null}
          inheritLabel={t('snmp.inheritGroup')}
        />
        <SshEditor
          value={ssh}
          onChange={setSsh}
          existing={device?.ssh_credentials}
          inheritLabel={t('ssh.inheritGroup')}
          deviceId={device?.id}
          mode={backupMode}
          onMode={setBackupMode}
          driver={driver}
          onDriver={setDriver}
        />
        <DependencyEditor
          value={dep}
          onChange={setDep}
          selfDeviceId={device?.id}
          effective={device ? device.effective_dependency : undefined}
        />
        {showSources && (
          <Field
            label={t('devices.sourceAddresses')}
            hint={
              srcErr ? (
                <span className="text-status-down">{t('devices.sourceAddressesErr')}</span>
              ) : (
                t('devices.sourceAddressesHint')
              )
            }
          >
            <Textarea
              rows={2}
              value={sources}
              onChange={(e) => setSources(e.target.value)}
              dir="ltr"
              className="font-mono"
              placeholder="10.0.0.12"
            />
          </Field>
        )}
        {showSources && !!device?.source_address_conflicts?.length && (
          <ul className="space-y-0.5 rounded-md bg-status-warning-soft px-3 py-2 text-xs text-status-warning">
            {device.source_address_conflicts.map((c) => (
              <li key={`${c.address}-${c.device_id}`}>
                {t('devices.sourceConflict', { address: c.address, device: c.device_name })}
              </li>
            ))}
          </ul>
        )}
        <InventoryFields
          device={device}
          portInventory={portInv}
          arpCollect={arp}
          onPortInventory={setPortInv}
          onArpCollect={setArp}
        />
        {!device && (
          <fieldset className="rounded-md border border-line p-3">
            <legend className="px-1 text-xs font-semibold text-ink-muted">
              {t('devices.autoSensors')}
            </legend>
            <div className="flex flex-wrap gap-4">
              {AUTO_SENSORS.map((s) => (
                <Checkbox
                  key={s}
                  label={t(`sensorType.${s}`)}
                  checked={auto.includes(s)}
                  onChange={(v) => setAuto((a) => (v ? [...a, s] : a.filter((x) => x !== s)))}
                />
              ))}
            </div>
          </fieldset>
        )}
        <ErrorBox error={m.error} context={{ deviceName }} />
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button
            type="submit"
            variant="primary"
            busy={m.isPending}
            disabled={
              group === null ||
              (dep.mode === 'device' && dep.device_id === null) ||
              (showSources && srcErr)
            }
          >
            {t('common.save')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

/** [2b-2] Port inventory / ARP collection modes with the effective state of a saved device. */
function InventoryFields({
  device,
  portInventory,
  arpCollect,
  onPortInventory,
  onArpCollect,
}: {
  device?: Device;
  portInventory: InventoryMode;
  arpCollect: InventoryMode;
  onPortInventory: (m: InventoryMode) => void;
  onArpCollect: (m: InventoryMode) => void;
}) {
  const { t } = useTranslation();
  const effective = (
    mode: InventoryMode,
    saved: InventoryMode | undefined,
    on: boolean | undefined,
  ) => {
    // The effective state is known only for the saved mode.
    if (!device || saved !== mode || on === undefined) return t('device.inventoryHint');
    const state = on ? t('device.effectiveOn') : t('device.effectiveOff');
    return mode === 'auto'
      ? t('device.effectiveAuto', { state, cls: t(`deviceClass.${device.effective_class}`) })
      : state;
  };
  return (
    <fieldset className="rounded-md border border-line p-3">
      <legend className="px-1 text-xs font-semibold text-ink-muted">{t('device.inventory')}</legend>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field
          label={t('device.portInventory')}
          hint={effective(portInventory, device?.port_inventory, device?.ports_effective)}
        >
          <Select
            value={portInventory}
            onChange={(e) => onPortInventory(e.target.value as InventoryMode)}
          >
            {INVENTORY_MODES.map((m) => (
              <option key={m} value={m}>
                {t(`inventoryMode.${m}`)}
              </option>
            ))}
          </Select>
        </Field>
        <Field
          label={t('device.arpCollect')}
          hint={effective(arpCollect, device?.arp_collect, device?.arp_effective)}
        >
          <Select
            value={arpCollect}
            onChange={(e) => onArpCollect(e.target.value as InventoryMode)}
          >
            {INVENTORY_MODES.map((m) => (
              <option key={m} value={m}>
                {t(`inventoryMode.${m}`)}
              </option>
            ))}
          </Select>
        </Field>
      </div>
    </fieldset>
  );
}
