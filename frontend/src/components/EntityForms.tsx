import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk, type DeviceInput, type GroupInput } from '../api/endpoints';
import type { Device, Group, SensorType } from '../api/types';
import { useTree } from '../hooks/queries';
import { snmpFormFrom, snmpFormToInput } from '../lib/snmp';
import { descendantGroupIds, findGroup, flattenGroups } from '../lib/tree';
import { SnmpEditor } from './SnmpEditor';
import { useToast } from './toast';
import { Button, Checkbox, ErrorBox, Field, Input, Modal, Select, Textarea } from './ui';

function GroupSelect({
  value,
  onChange,
  exclude = [],
  allowRoot,
}: {
  value: number | null;
  onChange: (v: number | null) => void;
  exclude?: number[];
  allowRoot?: boolean;
}) {
  const { t } = useTranslation();
  const { data } = useTree();
  const opts = flattenGroups(data?.groups ?? []).filter((x) => !exclude.includes(x.group.id));
  return (
    <Select
      value={value === null ? '' : String(value)}
      onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))}
    >
      {allowRoot && <option value="">{t('groups.noParent')}</option>}
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

  useEffect(() => {
    if (!open) return;
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
    m.mutate({
      name: name.trim(),
      parent_id: parent,
      description,
      sort_order: Number(sortOrder) || 0,
      snmp_credentials: snmpFormToInput(snmp),
    });
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
        <ErrorBox error={m.error} />
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="submit" variant="primary" busy={m.isPending}>
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
}: {
  open: boolean;
  onClose: () => void;
  device?: Device;
  groupId?: number;
  onCreated?: (d: Device) => void;
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

  useEffect(() => {
    if (!open) return;
    setGroup(device?.group_id ?? groupId ?? tree?.groups[0]?.id ?? null);
    setName(device?.name ?? '');
    setHost(device?.host ?? '');
    setDescription(device?.description ?? '');
    setEnabled(device?.enabled ?? true);
    setSortOrder(String(device?.sort_order ?? 0));
    setSnmp(snmpFormFrom(device?.snmp_credentials ?? null));
    setAuto(['ping']);
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
    };
    if (!device) body.auto_sensors = auto;
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
        {!device && (
          <fieldset className="rounded-md border border-slate-200 p-3 dark:border-slate-700">
            <legend className="px-1 text-xs font-semibold text-slate-600 dark:text-slate-400">
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
        <ErrorBox error={m.error} />
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="submit" variant="primary" busy={m.isPending} disabled={group === null}>
            {t('common.save')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
