// [2b-1] Topology map: devices and LLDP/CDP/manual links (all users view, admins edit).
import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type {
  Dependency,
  LayoutPosition,
  LinkEnd,
  TopologyGraph,
  TopologyLink,
  TopologyLinkInput,
  TopologyNeighborNode,
} from '../api/types';
import { DevicePicker, useDeviceNames } from '../components/Dependency';
import { DeviceFormModal, GroupSelect } from '../components/EntityForms';
import { LinkIcon, PlusIcon, RefreshIcon, TrashIcon, WrenchIcon, XIcon } from '../components/icons';
import { StatusBadge, StatusDot } from '../components/Status';
import { TopologyChart } from '../components/TopologyChart';
import { useToast } from '../components/toast';
import {
  Button,
  Card,
  Checkbox,
  ConfirmDialog,
  cx,
  Empty,
  ErrorBox,
  Field,
  FieldGroup,
  Input,
  Loading,
  Ltr,
  Modal,
  PageHeader,
  Select,
  Table,
  td,
  th,
} from '../components/ui';
import { useAuth } from '../hooks/auth';
import { useLocalState } from '../hooks/useLocalState';
import { useTheme } from '../hooks/theme';
import { errorMessage } from '../lib/errors';
import { formatDateTime, formatRelative, formatValue } from '../lib/format';
import { STATUS_HEX } from '../lib/status';
import {
  defaultSelection,
  linkState,
  NEUTRAL_EDGE,
  suggestionRows,
  suggestionsToBulk,
  type GraphNodeDatum,
} from '../lib/topology';

type Selection = { kind: 'link'; id: number } | { kind: 'neighbor'; id: number } | null;

function RefreshLine({ g }: { g: TopologyGraph }) {
  const { t } = useTranslation();
  const r = g.last_refresh;
  if (g.refresh_running || r?.status === 'running')
    return (
      <span className="inline-flex items-center gap-2 text-sky-700 dark:text-sky-300">
        <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-current border-t-transparent" />
        {t('topology.refreshRunning')}
        {r && r.devices_total > 0 && (
          <span className="tabular-nums">
            ({r.devices_ok + r.devices_failed}/{r.devices_total})
          </span>
        )}
      </span>
    );
  if (!r) return <span>{t('topology.neverRefreshed')}</span>;
  return (
    <span title={formatDateTime(r.finished_at ?? r.started_at)}>
      {t('topology.lastRefresh', { when: formatRelative(r.finished_at ?? r.started_at) })} ·{' '}
      {t(`topology.refreshStatus.${r.status}`)} ·{' '}
      {t('topology.refreshCounts', {
        ok: r.devices_ok,
        total: r.devices_total,
        links: r.links_found,
      })}
      {r.devices_failed > 0 && (
        <span className="text-red-600 dark:text-red-400">
          {' '}
          · {t('topology.refreshFailed', { count: r.devices_failed })}
        </span>
      )}
      {r.error && <span className="text-red-600 dark:text-red-400"> · {r.error}</span>}
    </span>
  );
}

function Legend() {
  const { t } = useTranslation();
  const { dark } = useTheme();
  const item = (color: string, label: string, dashed?: boolean) => (
    <span className="inline-flex items-center gap-1.5">
      <span
        className="inline-block w-5 border-t-[3px]"
        style={{
          borderColor: color,
          borderStyle: dashed ? 'dashed' : 'solid',
          opacity: dashed ? 0.5 : 1,
        }}
      />
      {label}
    </span>
  );
  return (
    <div className="space-y-2 text-xs text-slate-600 dark:text-slate-300">
      <p className="text-slate-500">{t('topology.hint')}</p>
      <div className="flex flex-wrap gap-x-3 gap-y-1">
        {(['up', 'warning', 'down', 'unreachable', 'unknown', 'paused'] as const).map((s) => (
          <span key={s} className="inline-flex items-center gap-1">
            <StatusDot status={s} /> {t(`status.${s}`)}
          </span>
        ))}
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2.5 w-2.5 rounded-full border border-dashed border-slate-400" />
          {t('topology.unmanaged')}
        </span>
        <span className="inline-flex items-center gap-1 text-sky-600 dark:text-sky-400">
          <WrenchIcon size={12} /> {t('maintenance.badge')}
        </span>
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1">
        {item(STATUS_HEX.up, t('topology.linkUp'))}
        {item(STATUS_HEX.down, t('topology.linkDown'))}
        {item(dark ? NEUTRAL_EDGE.dark : NEUTRAL_EDGE.light, t('topology.linkNoSensor'))}
        {item(dark ? NEUTRAL_EDGE.dark : NEUTRAL_EDGE.light, t('topology.linkStale'), true)}
      </div>
    </div>
  );
}

function EndView({ end, graph }: { end: LinkEnd; graph: TopologyGraph }) {
  const { t } = useTranslation();
  const node = graph.nodes.find((n) => n.key === end.node);
  return (
    <div className="min-w-0 space-y-0.5">
      <div className="truncate font-medium">
        {end.device_id !== null ? (
          <Link
            className="text-sky-700 hover:underline dark:text-sky-400"
            to={`/devices/${end.device_id}`}
          >
            {node?.name ?? `#${end.device_id}`}
          </Link>
        ) : (
          <span className="text-slate-500">{node?.name ?? end.node}</span>
        )}
      </div>
      <div className="text-xs">
        <span className="text-slate-500">{t('topology.port')}: </span>
        <Ltr className="font-mono">{end.port || '—'}</Ltr>
        {end.if_index !== null && (
          <span className="text-slate-500">
            {' '}
            (ifIndex <Ltr>{end.if_index}</Ltr>)
          </span>
        )}
      </div>
      {end.sensor_id !== null && (
        <div className="flex flex-wrap items-center gap-1 text-xs">
          <Link
            className="text-sky-700 hover:underline dark:text-sky-400"
            to={`/sensors/${end.sensor_id}`}
          >
            {t('topology.interfaceSensor')}
          </Link>
          {end.sensor_status && <StatusBadge status={end.sensor_status} />}
          {end.oper_status !== null && (
            <span className="text-slate-500">
              oper <Ltr>{end.oper_status === 1 ? 'up' : `${end.oper_status}`}</Ltr>
            </span>
          )}
        </div>
      )}
      {(end.in_bps !== null || end.out_bps !== null) && (
        <div className="text-xs tabular-nums text-slate-600 dark:text-slate-300">
          <Ltr>
            ↓ {formatValue(end.in_bps, 'bit/s')} ↑ {formatValue(end.out_bps, 'bit/s')}
          </Ltr>
        </div>
      )}
    </div>
  );
}

function LinkPanel({
  link,
  graph,
  onClose,
  onDelete,
}: {
  link: TopologyLink;
  graph: TopologyGraph;
  onClose: () => void;
  onDelete?: () => void;
}) {
  const { t } = useTranslation();
  const st = linkState(link);
  return (
    <div className="space-y-3 text-sm">
      <div className="flex items-start justify-between gap-2">
        <h3 className="flex items-center gap-1.5 font-semibold">
          <LinkIcon size={14} /> {t('topology.link')}
        </h3>
        <button
          type="button"
          onClick={onClose}
          className="rounded p-1 text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800"
          aria-label={t('common.close')}
        >
          <XIcon size={14} />
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-1.5 text-xs">
        <span className="rounded bg-slate-100 px-1.5 py-0.5 font-medium uppercase dark:bg-slate-800">
          {t(`topology.source.${link.source}`)}
        </span>
        {st && <StatusBadge status={st} />}
        {link.stale && (
          <span className="rounded bg-amber-100 px-1.5 py-0.5 font-medium text-amber-900 dark:bg-amber-900/50 dark:text-amber-100">
            {t('topology.stale', { count: link.missed_refreshes })}
          </span>
        )}
      </div>
      <div className="grid grid-cols-1 gap-3 rounded-md bg-slate-50 p-2.5 ring-1 ring-inset ring-slate-200 dark:bg-slate-800/60 dark:ring-slate-700">
        <EndView end={link.a} graph={graph} />
        <div className="border-t border-slate-200 dark:border-slate-700" />
        <EndView end={link.b} graph={graph} />
      </div>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-2 gap-y-0.5 text-xs">
        <dt className="text-slate-500">{t('topology.firstSeen')}</dt>
        <dd>{formatDateTime(link.first_seen)}</dd>
        <dt className="text-slate-500">{t('topology.lastSeen')}</dt>
        <dd>{formatDateTime(link.last_seen)}</dd>
      </dl>
      {onDelete && (
        <Button size="sm" variant="danger" onClick={onDelete}>
          <TrashIcon size={12} /> {t('topology.deleteLink')}
        </Button>
      )}
      {onDelete && link.source !== 'manual' && (
        <p className="text-xs text-slate-500">{t('topology.deleteDiscoveredHint')}</p>
      )}
    </div>
  );
}

function NeighborPanel({
  n,
  onClose,
  onAdd,
}: {
  n: TopologyNeighborNode;
  onClose: () => void;
  onAdd?: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="space-y-3 text-sm">
      <div className="flex items-start justify-between gap-2">
        <h3 dir="auto" className="min-w-0 break-words font-semibold">
          {n.name}
        </h3>
        <button
          type="button"
          onClick={onClose}
          className="rounded p-1 text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800"
          aria-label={t('common.close')}
        >
          <XIcon size={14} />
        </button>
      </div>
      <p className="text-xs text-slate-500">{t('topology.unmanagedHint')}</p>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-2 gap-y-1 text-xs">
        <dt className="text-slate-500">{t('topology.chassisId')}</dt>
        <dd className="min-w-0 break-all font-mono">
          <Ltr>{n.chassis_id}</Ltr>
        </dd>
        <dt className="text-slate-500">{t('topology.mgmtAddress')}</dt>
        <dd className="font-mono">
          <Ltr>{n.mgmt_address ?? '—'}</Ltr>
        </dd>
        {n.sys_descr && (
          <>
            <dt className="text-slate-500">sysDescr</dt>
            <dd className="min-w-0 break-words">
              <Ltr>{n.sys_descr}</Ltr>
            </dd>
          </>
        )}
      </dl>
      {onAdd && (
        <Button size="sm" variant="primary" onClick={onAdd}>
          <PlusIcon size={12} /> {t('topology.addAsDevice')}
        </Button>
      )}
    </div>
  );
}

/** Interface select of a device (SNMP walk); falls back to free text when unavailable. */
function PortInput({
  deviceId,
  ifIndex,
  port,
  onChange,
  label,
}: {
  deviceId: number | null;
  ifIndex: number | null;
  port: string;
  onChange: (ifIndex: number | null, port: string) => void;
  label: string;
}) {
  const { t } = useTranslation();
  const ifs = useQuery({
    queryKey: qk.snmpInterfaces(deviceId ?? 0),
    queryFn: () => endpoints.snmpInterfaces(deviceId as number),
    enabled: deviceId !== null,
    retry: false,
    staleTime: 60_000,
  });
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
      <Field
        label={t('topology.interface')}
        hint={ifs.error ? t('topology.noInterfaces') : undefined}
      >
        <Select
          value={ifIndex === null ? '' : String(ifIndex)}
          disabled={!ifs.data?.items.length}
          onChange={(e) => {
            const i = e.target.value ? Number(e.target.value) : null;
            const name = ifs.data?.items.find((x) => x.if_index === i)?.name;
            onChange(i, name ?? port);
          }}
        >
          <option value="">{t('topology.noInterface')}</option>
          {ifs.data?.items.map((x) => (
            <option key={x.if_index} value={x.if_index}>
              {x.name || x.descr} ({x.if_index})
            </option>
          ))}
        </Select>
      </Field>
      <Field label={label}>
        <Input
          value={port}
          onChange={(e) => onChange(ifIndex, e.target.value)}
          dir="ltr"
          maxLength={255}
        />
      </Field>
    </div>
  );
}

function AddLinkModal({
  open,
  onClose,
  graph,
}: {
  open: boolean;
  onClose: () => void;
  graph: TopologyGraph;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const [a, setA] = useState<number | null>(null);
  const [aIf, setAIf] = useState<number | null>(null);
  const [aPort, setAPort] = useState('');
  const [bKind, setBKind] = useState<'device' | 'neighbor'>('device');
  const [b, setB] = useState<number | null>(null);
  const [bIf, setBIf] = useState<number | null>(null);
  const [bPort, setBPort] = useState('');
  useEffect(() => {
    if (!open) return;
    setA(null);
    setAIf(null);
    setAPort('');
    setBKind('device');
    setB(null);
    setBIf(null);
    setBPort('');
  }, [open]);
  const neighbors = graph.nodes.filter((n): n is TopologyNeighborNode => n.kind === 'neighbor');
  const m = useMutation({
    mutationFn: (body: TopologyLinkInput) => endpoints.createTopologyLink(body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['topology'] });
      toast.push('success', t('common.saved'));
      onClose();
    },
  });
  const valid = a !== null && b !== null && !(bKind === 'device' && a === b);
  const submit = () => {
    if (!valid) return;
    const body: TopologyLinkInput = {
      a_device_id: a,
      a_if_index: aIf,
      b_device_id: bKind === 'device' ? b : null,
      b_neighbor_id: bKind === 'neighbor' ? b : null,
      b_if_index: bKind === 'device' ? bIf : null,
      b_port: bPort.trim(),
    };
    // a_port defaults to the ifName of a_if_index when omitted (api.md).
    if (aPort.trim() || aIf === null) body.a_port = aPort.trim();
    m.mutate(body);
  };
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t('topology.addLink')}
      wide
      footer={
        <>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button variant="primary" disabled={!valid} busy={m.isPending} onClick={submit}>
            {t('common.save')}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <fieldset className="min-w-0 space-y-2 rounded-md border border-slate-200 p-3 dark:border-slate-700">
          <legend className="px-1 text-xs font-semibold text-slate-600 dark:text-slate-400">
            {t('topology.endA')}
          </legend>
          <Field label={t('topology.device')} required>
            <DevicePicker
              value={a}
              onChange={(v) => {
                setA(v);
                setAIf(null);
              }}
            />
          </Field>
          <PortInput
            deviceId={a}
            ifIndex={aIf}
            port={aPort}
            label={t('topology.port')}
            onChange={(i, p) => {
              setAIf(i);
              setAPort(p);
            }}
          />
        </fieldset>
        <fieldset className="min-w-0 space-y-2 rounded-md border border-slate-200 p-3 dark:border-slate-700">
          <legend className="px-1 text-xs font-semibold text-slate-600 dark:text-slate-400">
            {t('topology.endB')}
          </legend>
          <FieldGroup label={t('topology.endBKind')}>
            <div className="flex gap-5">
              {(['device', 'neighbor'] as const).map((k) => (
                <label key={k} className="inline-flex items-center gap-2 text-sm">
                  <input
                    type="radio"
                    name="b-kind"
                    checked={bKind === k}
                    disabled={k === 'neighbor' && !neighbors.length}
                    onChange={() => {
                      setBKind(k);
                      setB(null);
                      setBIf(null);
                    }}
                  />
                  {t(`topology.bKind.${k}`)}
                </label>
              ))}
            </div>
          </FieldGroup>
          {bKind === 'device' ? (
            <>
              <Field label={t('topology.device')} required>
                <DevicePicker
                  value={b}
                  exclude={a !== null ? [a] : []}
                  onChange={(v) => {
                    setB(v);
                    setBIf(null);
                  }}
                />
              </Field>
              <PortInput
                deviceId={b}
                ifIndex={bIf}
                port={bPort}
                label={t('topology.port')}
                onChange={(i, p) => {
                  setBIf(i);
                  setBPort(p);
                }}
              />
            </>
          ) : (
            <>
              <Field label={t('topology.neighbor')} required>
                <Select
                  value={b === null ? '' : String(b)}
                  onChange={(e) => setB(e.target.value ? Number(e.target.value) : null)}
                >
                  <option value="">{t('topology.chooseNeighbor')}</option>
                  {neighbors.map((n) => (
                    <option key={n.neighbor_id} value={n.neighbor_id}>
                      {n.name} ({n.mgmt_address ?? n.chassis_id})
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label={t('topology.port')}>
                <Input
                  value={bPort}
                  onChange={(e) => setBPort(e.target.value)}
                  dir="ltr"
                  maxLength={255}
                />
              </Field>
            </>
          )}
        </fieldset>
        <ErrorBox error={m.error} />
      </div>
    </Modal>
  );
}

function currentText(
  t: (k: string, o?: Record<string, unknown>) => string,
  c: Dependency,
  effName: string | null,
): string {
  if (c.mode === 'device') return c.device_name ?? `#${c.device_id}`;
  if (c.mode === 'none') return t('dependency.modes.none');
  return effName
    ? t('topology.inheritEffective', { name: effName })
    : t('dependency.modes.inherit');
}

function SuggestModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const deviceName = useDeviceNames();
  const [root, setRoot] = useState<number | null>(null);
  const q = useQuery({
    queryKey: qk.dependencySuggestions(root),
    queryFn: () => endpoints.dependencySuggestions(root),
    enabled: open,
  });
  const rows = useMemo(() => suggestionRows(q.data?.items ?? []), [q.data]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  useEffect(() => setSelected(defaultSelection(rows)), [rows]);
  const apply = useMutation({
    mutationFn: () => endpoints.bulkDependencies(suggestionsToBulk(rows, selected)),
    onSuccess: (r) => {
      toast.push('success', t('topology.applied', { count: r.updated.length }));
      void qc.invalidateQueries({ queryKey: ['devices'] });
      void qc.invalidateQueries({ queryKey: ['device'] });
      void qc.invalidateQueries({ queryKey: qk.tree });
      void qc.invalidateQueries({ queryKey: ['dependencySuggestions'] });
      onClose();
    },
  });
  const toApply = suggestionsToBulk(rows, selected).length;
  const selectable = rows.filter((r) => r.suggested_parent_id !== null && r.changes);
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t('topology.suggestTitle')}
      wide
      footer={
        <>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button
            variant="primary"
            disabled={!toApply}
            busy={apply.isPending}
            onClick={() => apply.mutate()}
          >
            {t('topology.applySelected', { count: toApply })}
          </Button>
        </>
      }
    >
      <p className="mb-3 text-sm text-slate-600 dark:text-slate-300">{t('topology.suggestHint')}</p>
      <Field label={t('topology.root')} hint={t('topology.rootHint')} className="mb-3">
        <DevicePicker value={root} onChange={setRoot} allowNone />
      </Field>
      {q.data?.root_device_id != null && (
        <p className="mb-2 text-xs text-slate-500">
          {t('topology.rootUsed', {
            name: deviceName(q.data.root_device_id) ?? `#${q.data.root_device_id}`,
          })}
        </p>
      )}
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} />
      {q.data && !rows.length && <Empty>{t('topology.noSuggestions')}</Empty>}
      {rows.length > 0 && (
        <Table>
          <thead>
            <tr>
              <th className={th}>
                <input
                  type="checkbox"
                  className="h-4 w-4"
                  aria-label={t('common.selectAll')}
                  checked={
                    selectable.length > 0 && selectable.every((r) => selected.has(r.device_id))
                  }
                  onChange={(e) =>
                    setSelected(
                      e.target.checked ? new Set(selectable.map((r) => r.device_id)) : new Set(),
                    )
                  }
                />
              </th>
              <th className={th}>{t('topology.device')}</th>
              <th className={th}>{t('topology.current')}</th>
              <th className={th}>{t('topology.suggested')}</th>
              <th className={th}>{t('topology.hops')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.device_id} className={cx(!r.changes && 'text-slate-500')}>
                <td className={td}>
                  <input
                    type="checkbox"
                    className="h-4 w-4 disabled:opacity-40"
                    aria-label={r.device_name}
                    disabled={!r.changes || r.suggested_parent_id === null}
                    checked={selected.has(r.device_id)}
                    onChange={(e) =>
                      setSelected((cur) => {
                        const n = new Set(cur);
                        if (e.target.checked) n.add(r.device_id);
                        else n.delete(r.device_id);
                        return n;
                      })
                    }
                  />
                </td>
                <td className={`${td} font-medium`}>{r.device_name}</td>
                <td className={td}>
                  {currentText(
                    t,
                    r.current,
                    r.effective_parent_id !== null
                      ? (deviceName(r.effective_parent_id) ?? null)
                      : null,
                  )}
                </td>
                <td className={td}>
                  {r.suggested_parent_name ?? '—'}
                  {r.alreadyEffective && (
                    <span className="ms-1 text-xs text-slate-500">
                      ({t('topology.alreadyEffective')})
                    </span>
                  )}
                </td>
                <td className={`${td} tabular-nums`}>{r.hops}</td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      <ErrorBox className="mt-3" error={apply.error} context={{ deviceName }} />
    </Modal>
  );
}

export function TopologyPage() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const { dark } = useTheme();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const toast = useToast();
  const [groupId, setGroupId] = useLocalState<number | null>('netmon.topology.group', null);
  const [includeStale, setIncludeStale] = useLocalState('netmon.topology.stale', true);
  const [showTraffic, setShowTraffic] = useLocalState('netmon.topology.traffic', false);
  const [positions, setPositions] = useState<Record<string, [number, number]>>({});
  const [resetToken, setResetToken] = useState(0);
  const [sel, setSel] = useState<Selection>(null);
  const [addLink, setAddLink] = useState(false);
  const [suggest, setSuggest] = useState(false);
  const [delLink, setDelLink] = useState<TopologyLink | null>(null);
  const [delStale, setDelStale] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [addDevice, setAddDevice] = useState<TopologyNeighborNode | null>(null);

  const query = { group_id: groupId ?? undefined, include_stale: includeStale };
  const g = useQuery({
    queryKey: qk.topology(query),
    queryFn: () => endpoints.topology(query),
    placeholderData: keepPreviousData,
  });
  // A different filter is a different node set: start from the stored layout again.
  useEffect(() => setPositions({}), [groupId, includeStale]);

  const save = useMutation({
    mutationFn: (p: LayoutPosition[]) => endpoints.saveLayout(p),
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  const refresh = useMutation({
    mutationFn: endpoints.refreshTopology,
    onSuccess: (r) => {
      qc.setQueriesData<TopologyGraph>({ queryKey: ['topology'] }, (old) =>
        old ? { ...old, last_refresh: r, refresh_running: true } : old,
      );
      toast.push('success', t('topology.refreshStarted'));
    },
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  const reset = useMutation({
    mutationFn: endpoints.resetLayout,
    onSuccess: async () => {
      setPositions({});
      setResetToken((x) => x + 1);
      setResetting(false);
      await qc.invalidateQueries({ queryKey: ['topology'] });
    },
  });
  const removeLink = useMutation({
    mutationFn: (l: TopologyLink) => endpoints.deleteTopologyLink(l.id),
    onSuccess: () => {
      setDelLink(null);
      setSel(null);
      void qc.invalidateQueries({ queryKey: ['topology'] });
    },
  });
  const removeStale = useMutation({
    mutationFn: endpoints.deleteStaleLinks,
    onSuccess: (r) => {
      setDelStale(false);
      toast.push('success', t('topology.staleDeleted', { count: r.deleted }));
      void qc.invalidateQueries({ queryKey: ['topology'] });
    },
  });

  const data = g.data;
  const staleCount = data?.links.filter((l) => l.stale).length ?? 0;
  const selLink = sel?.kind === 'link' ? data?.links.find((l) => l.id === sel.id) : undefined;
  const selNeighbor =
    sel?.kind === 'neighbor'
      ? data?.nodes.find(
          (n): n is TopologyNeighborNode => n.kind === 'neighbor' && n.neighbor_id === sel.id,
        )
      : undefined;
  const running = !!data && (data.refresh_running || data.last_refresh?.status === 'running');

  const onNode = (n: GraphNodeDatum) => {
    if (n.kind === 'device') navigate(`/devices/${n.refId}`);
    else setSel({ kind: 'neighbor', id: n.refId });
  };

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('topology.title')}
        subtitle={data && <RefreshLine g={data} />}
        actions={
          isAdmin && (
            <>
              <Button
                variant="primary"
                busy={refresh.isPending}
                disabled={running}
                onClick={() => refresh.mutate()}
              >
                <RefreshIcon size={14} /> {t('topology.refresh')}
              </Button>
              <Button onClick={() => setSuggest(true)}>{t('topology.suggest')}</Button>
              <Button onClick={() => setAddLink(true)}>
                <LinkIcon size={14} /> {t('topology.addLink')}
              </Button>
            </>
          )
        }
      />
      <Card bodyClassName="p-3">
        <div className="flex flex-wrap items-end gap-3">
          <Field label={t('topology.group')} className="w-full sm:w-64">
            <GroupSelect value={groupId} onChange={setGroupId} allowRoot />
          </Field>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 pb-1.5">
            <Checkbox
              label={t('topology.includeStale')}
              checked={includeStale}
              onChange={setIncludeStale}
            />
            <Checkbox
              label={t('topology.showTraffic')}
              checked={showTraffic}
              onChange={setShowTraffic}
            />
          </div>
          {isAdmin && (
            <div className="flex flex-wrap gap-2 sm:ms-auto">
              <Button
                size="sm"
                disabled={!data?.nodes.length}
                busy={save.isPending}
                onClick={() =>
                  save.mutate(
                    Object.entries(positions).map(([node, [x, y]]) => ({ node, x, y })),
                    { onSuccess: () => toast.push('success', t('topology.layoutSaved')) },
                  )
                }
              >
                {t('topology.saveLayout')}
              </Button>
              <Button size="sm" onClick={() => setResetting(true)}>
                {t('topology.resetLayout')}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={!staleCount}
                onClick={() => setDelStale(true)}
              >
                <TrashIcon size={12} /> {t('topology.deleteStale', { count: staleCount })}
              </Button>
            </div>
          )}
        </div>
      </Card>
      {g.isLoading && <Loading />}
      <ErrorBox error={g.error} />
      {data && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_18rem]">
          <Card bodyClassName="p-0" className="overflow-hidden">
            {data.nodes.length ? (
              <TopologyChart
                graph={data}
                dark={dark}
                showTraffic={showTraffic}
                canSave={isAdmin}
                positions={positions}
                onPositions={setPositions}
                onSave={(p) => save.mutate(p)}
                onNodeClick={onNode}
                onLinkClick={(id) => setSel({ kind: 'link', id })}
                onBackgroundClick={() => setSel(null)}
                resetToken={resetToken}
              />
            ) : (
              <Empty>{t('topology.empty')}</Empty>
            )}
          </Card>
          <Card bodyClassName="p-3">
            {selLink ? (
              <LinkPanel
                link={selLink}
                graph={data}
                onClose={() => setSel(null)}
                onDelete={isAdmin ? () => setDelLink(selLink) : undefined}
              />
            ) : selNeighbor ? (
              <NeighborPanel
                n={selNeighbor}
                onClose={() => setSel(null)}
                onAdd={isAdmin ? () => setAddDevice(selNeighbor) : undefined}
              />
            ) : (
              <Legend />
            )}
          </Card>
        </div>
      )}
      {isAdmin && data && (
        <AddLinkModal open={addLink} onClose={() => setAddLink(false)} graph={data} />
      )}
      {isAdmin && <SuggestModal open={suggest} onClose={() => setSuggest(false)} />}
      {isAdmin && (
        <DeviceFormModal
          open={!!addDevice}
          onClose={() => setAddDevice(null)}
          initial={
            addDevice
              ? {
                  host: addDevice.mgmt_address ?? '',
                  name: addDevice.name,
                  description: addDevice.sys_descr ?? '',
                }
              : undefined
          }
          onCreated={(d) => navigate(`/devices/${d.id}`)}
        />
      )}
      <ConfirmDialog
        open={!!delLink}
        title={t('topology.deleteLink')}
        message={
          delLink
            ? t('topology.deleteLinkConfirm', {
                a: delLink.a.port || '?',
                b: delLink.b.port || '?',
              })
            : ''
        }
        onConfirm={() => delLink && removeLink.mutate(delLink)}
        onClose={() => setDelLink(null)}
        busy={removeLink.isPending}
        error={removeLink.error}
      />
      <ConfirmDialog
        open={delStale}
        title={t('topology.deleteStale', { count: staleCount })}
        message={t('topology.deleteStaleConfirm', { count: staleCount })}
        onConfirm={() => removeStale.mutate()}
        onClose={() => setDelStale(false)}
        busy={removeStale.isPending}
        error={removeStale.error}
      />
      <ConfirmDialog
        open={resetting}
        title={t('topology.resetLayout')}
        message={t('topology.resetConfirm')}
        confirmLabel={t('topology.resetLayout')}
        onConfirm={() => reset.mutate()}
        onClose={() => setResetting(false)}
        busy={reset.isPending}
        error={reset.error}
      />
    </div>
  );
}
