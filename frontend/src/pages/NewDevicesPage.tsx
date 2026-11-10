import { useMemo, useState, type FormEvent } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import { endpoints3, qk3, type NewDevicesQuery } from '../api/endpoints3';
import type { KnownMac, KnownMacState } from '../api/types3';
import { KNOWN_MAC_STATES } from '../api/types3';
import { LearningBar } from '../components/features/Phase3Settings';
import { Pagination } from '../components/Pagination';
import { TabBar } from '../components/TabBar';
import { useToast } from '../components/toast';
import {
  Button,
  Card,
  ConfirmDialog,
  cx,
  Empty,
  ErrorBox,
  Field,
  Input,
  Loading,
  Modal,
  PageHeader,
  Select,
  Table,
  Textarea,
  td,
  th,
} from '../components/ui';
import { useAuth } from '../hooks/auth';
import { FeatureOffNotice, useFeatures } from '../hooks/features';
import { useLiveMessages } from '../hooks/live';
import { useTree } from '../hooks/queries';
import { formatDateTime, formatRelative } from '../lib/format';
import { MAX_ACK_BATCH, normalizeMacPrefix, ouiOf, toggleSelection } from '../lib/newDevices';
import { flattenGroups } from '../lib/tree';

const TABS = ['detected', 'known', 'allowlist'] as const;
type NdTab = (typeof TABS)[number];

function StatusCard() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const toast = useToast();
  const [confirm, setConfirm] = useState(false);
  const q = useQuery({ queryKey: qk3.newDeviceStatus, queryFn: endpoints3.newDeviceStatus });
  const settings = useQuery({
    queryKey: qk.settings,
    queryFn: endpoints.settings,
    enabled: isAdmin,
  });
  const tree = useTree();
  const names = useMemo(() => {
    const m = new Map<number, string>();
    for (const { group } of flattenGroups(tree.data?.groups ?? [])) m.set(group.id, group.name);
    return m;
  }, [tree.data]);
  const restart = useMutation({
    mutationFn: endpoints3.restartLearning,
    onSuccess: (s) => {
      qc.setQueryData(qk3.newDeviceStatus, s);
      setConfirm(false);
      toast.push('success', t('newDevices.learningRestarted'));
    },
  });
  const s = q.data;
  return (
    <Card
      title={t('newDevices.statusTitle')}
      actions={
        isAdmin && (
          <Button size="sm" onClick={() => setConfirm(true)}>
            {t('newDevices.restartLearning')}
          </Button>
        )
      }
    >
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} />
      {s && (
        <div className="space-y-3 text-sm">
          {s.learning ? (
            <div className="space-y-1">
              <p>{t('newDevices.learningUntil', { date: formatDateTime(s.learning_until) })}</p>
              {settings.data?.newdev_learning_days ? (
                <LearningBar
                  learning={s.learning}
                  until={s.learning_until}
                  days={settings.data.newdev_learning_days}
                />
              ) : null}
              <p className="text-xs text-slate-500">{t('newDevices.learningHint')}</p>
            </div>
          ) : (
            <p className="text-green-700 dark:text-green-400">{t('newDevices.watching')}</p>
          )}
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-4">
            <div>
              <dt className="text-xs text-slate-500">{t('newDevices.switches')}</dt>
              <dd className="tabular-nums">
                {s.switches_baselined} / {s.switches_in_scope}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">{t('newDevices.knownCount')}</dt>
              <dd className="tabular-nums">{s.known}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">{t('newDevices.newCount')}</dt>
              <dd
                className={cx(
                  'tabular-nums',
                  s.new_unacknowledged > 0 && 'font-semibold text-rose-600',
                )}
              >
                {s.new_unacknowledged}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">{t('newDevices.lastDetection')}</dt>
              <dd>{s.last_detection_at ? formatRelative(s.last_detection_at) : '—'}</dd>
            </div>
          </dl>
          <p className="text-xs text-slate-500">
            {t('newDevices.scope')}:{' '}
            {s.scope_group_ids.length
              ? s.scope_group_ids.map((id) => names.get(id) ?? `#${id}`).join(', ')
              : t('newDevices.scopeAll')}
          </p>
          <p className="text-xs text-slate-500">{t('newDevices.excludeHint')}</p>
        </div>
      )}
      <ConfirmDialog
        open={confirm}
        title={t('newDevices.restartLearning')}
        message={t('newDevices.restartConfirm')}
        confirmLabel={t('newDevices.restartLearning')}
        onConfirm={() => restart.mutate()}
        onClose={() => setConfirm(false)}
        busy={restart.isPending}
        error={restart.error}
      />
    </Card>
  );
}

function AckModal({ macs, onClose }: { macs: string[]; onClose: (done: boolean) => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const [comment, setComment] = useState('');
  const m = useMutation({
    mutationFn: () => endpoints3.acknowledgeMacs(macs.slice(0, MAX_ACK_BATCH), comment.trim()),
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ['new-devices'] });
      void qc.invalidateQueries({ queryKey: qk3.newDeviceStatus });
      toast.push('success', t('newDevices.acknowledged', { n: r.acknowledged }));
      setComment('');
      onClose(true);
    },
  });
  return (
    <Modal
      open={macs.length > 0}
      onClose={() => onClose(false)}
      title={t('newDevices.ackTitle', { n: macs.length })}
      footer={
        <>
          <Button onClick={() => onClose(false)}>{t('common.cancel')}</Button>
          <Button variant="primary" busy={m.isPending} onClick={() => m.mutate()}>
            {t('newDevices.acknowledge')}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <p className="text-sm">{t('newDevices.ackText')}</p>
        {macs.length > MAX_ACK_BATCH && (
          <p className="text-xs text-amber-700">{t('newDevices.ackLimit', { n: MAX_ACK_BATCH })}</p>
        )}
        <Field label={t('newDevices.comment')}>
          <Textarea
            rows={2}
            maxLength={500}
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            dir="auto"
          />
        </Field>
        <ErrorBox error={m.error} />
      </div>
    </Modal>
  );
}

function MacTable({
  items,
  selectable,
  selected,
  onSelect,
  actions,
}: {
  items: KnownMac[];
  selectable: boolean;
  selected: string[];
  onSelect: (mac: string, on: boolean) => void;
  actions?: (m: KnownMac) => React.ReactNode;
}) {
  const { t } = useTranslation();
  const all = items.length > 0 && items.every((m) => selected.includes(m.mac));
  return (
    <Table>
      <thead>
        <tr>
          {selectable && (
            <th className={th}>
              <input
                type="checkbox"
                aria-label={t('common.selectAll')}
                checked={all}
                onChange={(e) => items.forEach((m) => onSelect(m.mac, e.target.checked))}
                className="h-4 w-4 rounded border-slate-300"
              />
            </th>
          )}
          <th className={th}>MAC</th>
          <th className={th}>{t('newDevices.vendor')}</th>
          <th className={th}>{t('newDevices.ipHost')}</th>
          <th className={th}>{t('newDevices.switchPort')}</th>
          <th className={th}>VLAN</th>
          <th className={th}>{t('newDevices.firstSeen')}</th>
          <th className={th}>{t('newDevices.state')}</th>
          {actions && <th className={th} />}
        </tr>
      </thead>
      <tbody>
        {items.map((m) => (
          <tr key={m.mac} className={cx(m.state === 'new' && 'bg-rose-50/60 dark:bg-rose-950/20')}>
            {selectable && (
              <td className={td}>
                <input
                  type="checkbox"
                  aria-label={m.mac}
                  checked={selected.includes(m.mac)}
                  onChange={(e) => onSelect(m.mac, e.target.checked)}
                  className="h-4 w-4 rounded border-slate-300"
                />
              </td>
            )}
            <td className={td}>
              <bdi dir="ltr" className="font-mono text-xs">
                {m.mac}
              </bdi>
              {m.locally_administered && (
                <span className="ms-1 text-xs text-slate-500" title={t('newDevices.randomHint')}>
                  ({t('newDevices.random')})
                </span>
              )}
            </td>
            <td className={td}>{m.vendor ?? '—'}</td>
            <td className={td}>
              {m.ip || m.first_ip ? (
                <bdi dir="ltr" className="font-mono text-xs">
                  {m.ip ?? m.first_ip}
                </bdi>
              ) : (
                '—'
              )}
              {(m.hostname ?? m.first_hostname) && (
                <div className="text-xs text-slate-500">
                  <bdi dir="ltr">{m.hostname ?? m.first_hostname}</bdi>
                </div>
              )}
            </td>
            <td className={td}>
              {m.first_device_id ? (
                <Link
                  to={`/devices/${m.first_device_id}?tab=ports`}
                  className="text-sky-700 hover:underline dark:text-sky-400"
                >
                  {m.first_device_name ?? `#${m.first_device_id}`}
                </Link>
              ) : (
                (m.first_device_name ?? '—')
              )}
              {m.first_port_name && (
                <bdi dir="ltr" className="ms-1 text-xs">
                  {m.first_port_name}
                </bdi>
              )}
              {m.last_device_name && m.last_device_id !== m.first_device_id && (
                <div className="text-xs text-slate-500">
                  {t('newDevices.nowAt', {
                    place: `${m.last_device_name} ${m.last_port_name ?? ''}`.trim(),
                  })}
                </div>
              )}
            </td>
            <td className={`${td} tabular-nums`}>{m.first_vlan ?? '—'}</td>
            <td className={td}>
              {formatDateTime(m.detected_at ?? m.first_seen)}
              <div className="text-xs text-slate-500">
                {t('newDevices.lastSeen', { rel: formatRelative(m.last_seen) })}
              </div>
            </td>
            <td className={td}>
              <span
                className={cx(
                  'rounded px-1.5 py-0.5 text-xs font-medium',
                  m.state === 'new' &&
                    'bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-200',
                  m.state === 'known' &&
                    'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-200',
                  m.state === 'allowed' &&
                    'bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-200',
                  m.state === 'baseline' &&
                    'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
                )}
              >
                {t(`newDevices.states.${m.state}`)}
              </span>
              {m.ack_by && (
                <div className="text-xs text-slate-500" title={m.ack_comment}>
                  {t('newDevices.ackBy', { user: m.ack_by })}
                </div>
              )}
            </td>
            {actions && <td className={`${td} text-end`}>{actions(m)}</td>}
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

function MacList({ mode, deviceId }: { mode: 'detected' | 'known'; deviceId: number | null }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const toast = useToast();
  const [page, setPage] = useState(1);
  const [text, setText] = useState('');
  const [state, setState] = useState<KnownMacState | ''>('');
  const [selected, setSelected] = useState<string[]>([]);
  const [ackMacs, setAckMacs] = useState<string[]>([]);
  const [forget, setForget] = useState<KnownMac | null>(null);
  const query: NewDevicesQuery = {
    state: mode === 'detected' ? ['new'] : state ? [state] : undefined,
    device_id: deviceId ?? undefined,
    q: text.trim() || undefined,
    page,
  };
  const q = useQuery({
    queryKey: qk3.newDevices(query),
    queryFn: () => endpoints3.newDevices(query),
    placeholderData: keepPreviousData,
  });
  const allow = useMutation({
    mutationFn: (m: KnownMac) => endpoints3.addAllow(ouiOf(m.mac), m.vendor ?? ''),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk3.allowlist });
      toast.push('success', t('allowlist.added'));
    },
  });
  const del = useMutation({
    mutationFn: (mac: string) => endpoints3.forgetMac(mac),
    onSuccess: () => {
      setForget(null);
      void qc.invalidateQueries({ queryKey: ['new-devices'] });
      void qc.invalidateQueries({ queryKey: qk3.newDeviceStatus });
      toast.push('success', t('newDevices.forgotten'));
    },
  });
  const items = q.data?.items ?? [];
  return (
    <Card bodyClassName="p-0">
      <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 p-3 dark:border-slate-800">
        <Input
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setPage(1);
          }}
          placeholder={t('newDevices.searchPlaceholder')}
          className="w-64"
          aria-label={t('newDevices.search')}
        />
        {mode === 'known' && (
          <Select
            value={state}
            onChange={(e) => {
              setState(e.target.value as KnownMacState | '');
              setPage(1);
            }}
            className="w-40"
            aria-label={t('newDevices.state')}
          >
            <option value="">{t('newDevices.anyState')}</option>
            {KNOWN_MAC_STATES.map((s) => (
              <option key={s} value={s}>
                {t(`newDevices.states.${s}`)}
              </option>
            ))}
          </Select>
        )}
        {mode === 'detected' && isAdmin && (
          <Button
            size="sm"
            variant="primary"
            className="ms-auto"
            disabled={!selected.length}
            onClick={() => setAckMacs(selected)}
          >
            {t('newDevices.ackSelected', { n: selected.length })}
          </Button>
        )}
      </div>
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error ?? allow.error} className="m-3" />
      {q.data && !items.length && (
        <Empty>{mode === 'detected' ? t('newDevices.noneNew') : t('newDevices.noneKnown')}</Empty>
      )}
      {items.length > 0 && (
        <>
          <MacTable
            items={items}
            selectable={mode === 'detected' && isAdmin}
            selected={selected}
            onSelect={(mac, on) => setSelected((s) => toggleSelection(s, mac, on))}
            actions={
              isAdmin
                ? (m) => (
                    <span className="inline-flex flex-wrap justify-end gap-1">
                      {m.state === 'new' && (
                        <Button size="sm" onClick={() => setAckMacs([m.mac])}>
                          {t('newDevices.acknowledge')}
                        </Button>
                      )}
                      {m.state === 'new' && !m.locally_administered && (
                        <Button
                          size="sm"
                          variant="ghost"
                          title={t('allowlist.allowVendorHint', { oui: ouiOf(m.mac) })}
                          onClick={() => allow.mutate(m)}
                        >
                          {t('allowlist.allowVendor')}
                        </Button>
                      )}
                      <Button size="sm" variant="ghost" onClick={() => setForget(m)}>
                        {t('newDevices.forget')}
                      </Button>
                    </span>
                  )
                : undefined
            }
          />
          <Pagination
            page={page}
            pageSize={q.data?.page_size ?? 100}
            total={q.data?.total ?? 0}
            onPage={setPage}
          />
        </>
      )}
      <AckModal
        macs={ackMacs}
        onClose={(done) => {
          if (done) setSelected((s) => s.filter((x) => !ackMacs.includes(x)));
          setAckMacs([]);
        }}
      />
      <ConfirmDialog
        open={!!forget}
        title={t('newDevices.forget')}
        message={t('newDevices.forgetConfirm', { mac: forget?.mac ?? '' })}
        confirmLabel={t('newDevices.forget')}
        onConfirm={() => forget && del.mutate(forget.mac)}
        onClose={() => setForget(null)}
        busy={del.isPending}
        error={del.error}
      />
    </Card>
  );
}

function AllowlistView() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const toast = useToast();
  const [prefix, setPrefix] = useState('');
  const [comment, setComment] = useState('');
  const q = useQuery({ queryKey: qk3.allowlist, queryFn: endpoints3.allowlist });
  const norm = prefix.trim() ? normalizeMacPrefix(prefix) : null;
  const add = useMutation({
    mutationFn: () => endpoints3.addAllow(prefix.trim(), comment.trim()),
    onSuccess: () => {
      setPrefix('');
      setComment('');
      void qc.invalidateQueries({ queryKey: qk3.allowlist });
      toast.push('success', t('allowlist.added'));
    },
  });
  const del = useMutation({
    mutationFn: (id: number) => endpoints3.deleteAllow(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk3.allowlist }),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (norm) add.mutate();
  };
  return (
    <Card bodyClassName="p-0">
      <p className="border-b border-slate-200 px-4 py-2 text-xs text-slate-500 dark:border-slate-800">
        {t('allowlist.intro')}
      </p>
      {isAdmin && (
        <form
          onSubmit={submit}
          className="flex flex-wrap items-end gap-3 border-b border-slate-200 p-3 dark:border-slate-800"
        >
          <Field
            label={t('allowlist.prefix')}
            className="w-56"
            hint={
              prefix.trim() && !norm ? (
                <span className="text-red-600">{t('allowlist.prefixInvalid')}</span>
              ) : norm ? (
                <bdi dir="ltr">
                  {norm.prefix} /{norm.bits}
                </bdi>
              ) : (
                t('allowlist.prefixHint')
              )
            }
          >
            <Input
              value={prefix}
              onChange={(e) => setPrefix(e.target.value)}
              dir="ltr"
              className="font-mono"
              placeholder="3c:a8:2a"
            />
          </Field>
          <Field label={t('allowlist.comment')} className="min-w-[12rem] flex-1">
            <Input value={comment} onChange={(e) => setComment(e.target.value)} maxLength={200} />
          </Field>
          <Button type="submit" variant="primary" busy={add.isPending} disabled={!norm}>
            {t('allowlist.add')}
          </Button>
        </form>
      )}
      <ErrorBox error={q.error ?? add.error ?? del.error} className="m-3" />
      {q.isLoading && <Loading />}
      {q.data && !q.data.items.length && <Empty>{t('allowlist.empty')}</Empty>}
      {q.data && q.data.items.length > 0 && (
        <Table>
          <thead>
            <tr>
              <th className={th}>{t('allowlist.prefix')}</th>
              <th className={th}>{t('newDevices.vendor')}</th>
              <th className={th}>{t('allowlist.comment')}</th>
              <th className={th}>{t('allowlist.created')}</th>
              {isAdmin && <th className={th} />}
            </tr>
          </thead>
          <tbody>
            {q.data.items.map((a) => (
              <tr key={a.id}>
                <td className={td}>
                  <bdi dir="ltr" className="font-mono text-xs">
                    {a.prefix} /{a.bits}
                  </bdi>
                </td>
                <td className={td}>{a.vendor ?? '—'}</td>
                <td className={td}>
                  <bdi dir="auto">{a.comment}</bdi>
                </td>
                <td className={td}>
                  {formatDateTime(a.created_at)}
                  {a.created_by && (
                    <span className="ms-1 text-xs text-slate-500">({a.created_by})</span>
                  )}
                </td>
                {isAdmin && (
                  <td className={`${td} text-end`}>
                    <Button size="sm" variant="ghost" onClick={() => del.mutate(a.id)}>
                      {t('common.delete')}
                    </Button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}

/** [3] New devices on the network (/new-devices). */
export function NewDevicesPage() {
  const { t } = useTranslation();
  const { features } = useFeatures();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const p = params.get('tab') as NdTab | null;
  const tab: NdTab = p && TABS.includes(p) ? p : 'detected';
  const dev = params.get('device_id');
  const deviceId = dev && /^\d+$/.test(dev) ? Number(dev) : null;
  useLiveMessages(['new_device.detected'], () => {
    void qc.invalidateQueries({ queryKey: ['new-devices'] });
    void qc.invalidateQueries({ queryKey: qk3.newDeviceStatus });
  });
  const on = features.new_device_alerts;
  return (
    <div className="space-y-4">
      <PageHeader title={t('newDevices.title')} subtitle={t('newDevices.subtitle')} />
      {!on && <FeatureOffNotice feature="new_device_alerts" />}
      {on && (
        <>
          <StatusCard />
          {deviceId !== null && (
            <p className="text-sm">
              {t('newDevices.filteredByDevice')}{' '}
              <button
                type="button"
                className="text-sky-700 hover:underline dark:text-sky-400"
                onClick={() => setParams(tab === 'detected' ? {} : { tab }, { replace: true })}
              >
                {t('common.clearFilters')}
              </button>
            </p>
          )}
          <TabBar
            tabs={[...TABS]}
            value={tab}
            label={t('newDevices.title')}
            onChange={(k) => {
              const n = new URLSearchParams();
              if (k !== 'detected') n.set('tab', k);
              if (deviceId !== null) n.set('device_id', String(deviceId));
              setParams(n, { replace: true });
            }}
            render={(k) => t(`newDevices.tab.${k}`)}
          />
          {tab === 'detected' && <MacList key="d" mode="detected" deviceId={deviceId} />}
          {tab === 'known' && <MacList key="k" mode="known" deviceId={deviceId} />}
          {tab === 'allowlist' && <AllowlistView />}
        </>
      )}
    </div>
  );
}
