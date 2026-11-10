import { useMemo, useState } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { endpoints3, qk3 } from '../api/endpoints3';
import type { FlowExporter, FlowTop, FlowTopItem } from '../api/types3';
import { FLOW_DROP_REASONS } from '../api/types3';
import { EChart } from '../components/flows/EChart';
import { DevicePicker } from '../components/Dependency';
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
  FieldGroup,
  Input,
  Loading,
  Modal,
  PageHeader,
  Select,
  Table,
  td,
  th,
} from '../components/ui';
import { useAuth } from '../hooks/auth';
import { FeatureOffNotice, useFeatures } from '../hooks/features';
import { useLiveMessages } from '../hooks/live';
import { formatDateTime, formatRelative } from '../lib/format';
import {
  conversationsAllowed,
  effectiveSampling,
  FLOW_PRESETS,
  flowRange,
  formatBits,
  formatBytes,
  formatCount,
  formatShare,
  mirroredSeries,
  peakRate,
  samplingError,
  totalDropped,
  type FlowPreset,
} from '../lib/flows';

const TABS = ['top', 'interfaces', 'exporters', 'status'] as const;
type FlowTab = (typeof TABS)[number];

function useRange(preset: FlowPreset) {
  // re-anchored once a minute so queries stay cached between renders
  const minute = Math.floor(Date.now() / 60_000);
  return useMemo(() => flowRange(preset, minute * 60_000), [preset, minute]);
}

function TopBars({ top, testId }: { top: FlowTop; testId: string }) {
  const option = useMemo(() => {
    const items = [...top.items].reverse();
    return {
      grid: { left: 8, right: 70, top: 8, bottom: 8, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        valueFormatter: (v: number) => formatBytes(v),
      },
      xAxis: { type: 'value', axisLabel: { formatter: (v: number) => formatBytes(v) } },
      yAxis: {
        type: 'category',
        data: items.map((x) => x.label),
        axisLabel: { width: 220, overflow: 'truncate' },
      },
      series: [
        {
          type: 'bar',
          data: items.map((x) => x.bytes),
          itemStyle: { color: '#0284c7' },
          label: {
            show: true,
            position: 'right',
            formatter: (p: { value: number }) => formatBytes(p.value),
          },
        },
      ],
    };
  }, [top]);
  return (
    <EChart option={option} className={top.items.length > 6 ? 'h-80' : 'h-56'} testId={testId} />
  );
}

function TopTable({ top, kind }: { top: FlowTop; kind: 'talkers' | 'apps' | 'conversations' }) {
  const { t } = useTranslation();
  return (
    <Table>
      <thead>
        <tr>
          <th className={th}>{t(`flows.col.${kind}`)}</th>
          <th className={`${th} text-end`}>{t('flows.bytes')}</th>
          <th className={`${th} text-end`}>{t('flows.packets')}</th>
          <th className={`${th} text-end`}>{t('flows.share')}</th>
        </tr>
      </thead>
      <tbody>
        {top.items.map((x: FlowTopItem) => (
          <tr key={x.key}>
            <td className={td}>
              <bdi dir="ltr" className="font-mono text-xs">
                {x.label}
              </bdi>
              {x.device_id ? (
                <Link
                  to={`/devices/${x.device_id}`}
                  className="ms-2 text-xs text-sky-700 hover:underline dark:text-sky-400"
                >
                  {t('flows.device')}
                </Link>
              ) : null}
            </td>
            <td className={`${td} text-end tabular-nums`}>{formatBytes(x.bytes)}</td>
            <td className={`${td} text-end tabular-nums`}>{formatCount(x.packets)}</td>
            <td className={`${td} text-end tabular-nums`}>{formatShare(x.share)}</td>
          </tr>
        ))}
        {top.other_bytes > 0 && (
          <tr className="text-slate-500">
            <td className={td}>{t('flows.other')}</td>
            <td className={`${td} text-end tabular-nums`}>{formatBytes(top.other_bytes)}</td>
            <td className={td} />
            <td className={`${td} text-end tabular-nums`}>
              {top.total_bytes ? formatShare(top.other_bytes / top.total_bytes) : ''}
            </td>
          </tr>
        )}
      </tbody>
    </Table>
  );
}

function TopView({
  range,
  preset,
  exporterId,
  multiExporter,
}: {
  range: { since: string; until: string };
  preset: FlowPreset;
  exporterId: number | null;
  multiExporter: boolean;
}) {
  const { t } = useTranslation();
  const [side, setSide] = useState<'src' | 'dst' | 'both'>('both');
  const base = { ...range, exporter_id: exporterId ?? undefined, limit: 10 };
  const talkers = useQuery({
    queryKey: qk3.flowTop('talkers', { ...base, side }),
    queryFn: () => endpoints3.topTalkers({ ...base, side }),
    placeholderData: keepPreviousData,
  });
  const apps = useQuery({
    queryKey: qk3.flowTop('apps', base),
    queryFn: () => endpoints3.topApplications(base),
    placeholderData: keepPreviousData,
  });
  const convOk = conversationsAllowed(preset);
  const conv = useQuery({
    queryKey: qk3.flowTop('conversations', base),
    queryFn: () => endpoints3.topConversations(base),
    enabled: convOk,
    placeholderData: keepPreviousData,
  });
  return (
    <div className="space-y-4">
      {exporterId === null && multiExporter && (
        <p className="text-xs text-amber-700 dark:text-amber-300">{t('flows.doubleCount')}</p>
      )}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Card
          title={t('flows.topTalkers')}
          bodyClassName="p-0"
          actions={
            <Select
              value={side}
              onChange={(e) => setSide(e.target.value as 'src' | 'dst' | 'both')}
              className="w-auto py-0.5 text-xs"
              aria-label={t('flows.side')}
            >
              {(['both', 'src', 'dst'] as const).map((s) => (
                <option key={s} value={s}>
                  {t(`flows.sides.${s}`)}
                </option>
              ))}
            </Select>
          }
        >
          {talkers.isLoading && <Loading />}
          <ErrorBox error={talkers.error} className="m-3" />
          {talkers.data && !talkers.data.items.length && <Empty>{t('flows.noData')}</Empty>}
          {talkers.data && talkers.data.items.length > 0 && (
            <>
              <div className="p-2">
                <TopBars top={talkers.data} testId="chart-talkers" />
              </div>
              <TopTable top={talkers.data} kind="talkers" />
            </>
          )}
        </Card>
        <Card title={t('flows.topApps')} bodyClassName="p-0">
          {apps.isLoading && <Loading />}
          <ErrorBox error={apps.error} className="m-3" />
          {apps.data && !apps.data.items.length && <Empty>{t('flows.noData')}</Empty>}
          {apps.data && apps.data.items.length > 0 && (
            <>
              <div className="p-2">
                <TopBars top={apps.data} testId="chart-apps" />
              </div>
              <TopTable top={apps.data} kind="apps" />
            </>
          )}
        </Card>
      </div>
      <Card title={t('flows.topConversations')} bodyClassName="p-0">
        {!convOk && <p className="p-3 text-sm text-slate-500">{t('flows.conversationsRange')}</p>}
        {convOk && conv.isLoading && <Loading />}
        <ErrorBox error={conv.error} className="m-3" />
        {convOk && conv.data && !conv.data.items.length && <Empty>{t('flows.noData')}</Empty>}
        {convOk && conv.data && conv.data.items.length > 0 && (
          <TopTable top={conv.data} kind="conversations" />
        )}
      </Card>
    </div>
  );
}

function InterfacesView({
  range,
  exporters,
  exporterId,
}: {
  range: { since: string; until: string };
  exporters: FlowExporter[];
  exporterId: number | null;
}) {
  const { t } = useTranslation();
  const exp = exporterId ?? exporters[0]?.id ?? null;
  const [ifIndex, setIfIndex] = useState<number | null>(null);
  const [split, setSplit] = useState<'none' | 'app' | 'host'>('none');
  const ifs = useQuery({
    queryKey: qk3.flowInterfaces({ exporter_id: exp, ...range }),
    queryFn: () => endpoints3.flowInterfaces({ exporter_id: exp ?? undefined, ...range }),
    enabled: exp !== null,
  });
  const chosen = ifIndex ?? ifs.data?.items[0]?.if_index ?? null;
  const sq = { exporter_id: exp ?? undefined, if_index: chosen ?? undefined, split, ...range };
  const series = useQuery({
    queryKey: qk3.flowSeries(sq),
    queryFn: () => endpoints3.flowSeries(sq),
    enabled: exp !== null && chosen !== null,
    placeholderData: keepPreviousData,
  });
  const option = useMemo(() => {
    const lines = mirroredSeries(series.data);
    return {
      grid: { left: 8, right: 16, top: 36, bottom: 40, containLabel: true },
      legend: { type: 'scroll', top: 0 },
      tooltip: {
        trigger: 'axis',
        valueFormatter: (v: number) => formatBits(Math.abs(v)),
      },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 6 }],
      xAxis: { type: 'time' },
      yAxis: {
        type: 'value',
        axisLabel: { formatter: (v: number) => formatBits(Math.abs(v)) },
      },
      series: lines.map((l) => ({
        name: `${l.name} (${t(`flows.dir.${l.direction}`)})`,
        type: 'line',
        stack: l.direction,
        areaStyle: { opacity: 0.6 },
        showSymbol: false,
        lineStyle: { width: 1 },
        data: l.data,
      })),
    };
  }, [series.data, t]);
  if (exp === null) return <Empty>{t('flows.noExporters')}</Empty>;
  return (
    <div className="space-y-4">
      <Card bodyClassName="p-3">
        <div className="flex flex-wrap items-end gap-3">
          <Field label={t('flows.interface')} className="w-72">
            <Select
              value={chosen ?? ''}
              onChange={(e) => setIfIndex(Number(e.target.value))}
              disabled={!ifs.data?.items.length}
            >
              {ifs.data?.items.map((i) => (
                <option key={i.if_index} value={i.if_index}>
                  {i.name || `ifIndex ${i.if_index}`} — {formatBytes(i.in_bytes + i.out_bytes)}
                </option>
              ))}
            </Select>
          </Field>
          <Field label={t('flows.split')} className="w-44">
            <Select value={split} onChange={(e) => setSplit(e.target.value as typeof split)}>
              {(['none', 'app', 'host'] as const).map((s) => (
                <option key={s} value={s}>
                  {t(`flows.splits.${s}`)}
                </option>
              ))}
            </Select>
          </Field>
          {series.data?.interface?.speed_bps ? (
            <span className="pb-2 text-xs text-slate-500">
              {t('flows.speed', { speed: formatBits(series.data.interface.speed_bps) })}
            </span>
          ) : null}
        </div>
      </Card>
      <ErrorBox error={ifs.error ?? series.error} />
      {ifs.data && !ifs.data.items.length && <Empty>{t('flows.noData')}</Empty>}
      {series.data && (
        <Card
          title={series.data.interface?.name ?? t('flows.interface')}
          actions={
            <span className="text-xs text-slate-500">
              {t('flows.peak', {
                in: formatBits(peakRate(series.data, 'in')),
                out: formatBits(peakRate(series.data, 'out')),
              })}
            </span>
          }
        >
          <EChart option={option} className="h-96" testId="chart-interface" />
          <p className="mt-1 text-xs text-slate-500">{t('flows.mirrorHint')}</p>
        </Card>
      )}
    </div>
  );
}

function ExporterModal({ e, onClose }: { e: FlowExporter | null; onClose: () => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const [sampling, setSampling] = useState('');
  const [device, setDevice] = useState<number | null>(null);
  const [opened, setOpened] = useState<number | null>(null);
  if (e && opened !== e.id) {
    setOpened(e.id);
    setSampling(e.sampling_override ? String(e.sampling_override) : '');
    setDevice(e.device_id);
  }
  const m = useMutation({
    mutationFn: () =>
      endpoints3.updateExporter((e as FlowExporter).id, {
        sampling_override: sampling.trim() ? Number(sampling) : null,
        device_id: device,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk3.flowExporters });
      toast.push('success', t('common.saved'));
      onClose();
    },
  });
  const err = samplingError(sampling);
  return (
    <Modal open={!!e} onClose={onClose} title={t('exporter.edit', { address: e?.address ?? '' })}>
      <form
        className="space-y-3"
        onSubmit={(ev) => {
          ev.preventDefault();
          if (!err) m.mutate();
        }}
      >
        <Field
          label={t('exporter.samplingOverride')}
          hint={
            err ? (
              <span className="text-red-600">{t(err)}</span>
            ) : (
              t('exporter.samplingHint', { seen: e?.sampling_seen ?? '—' })
            )
          }
        >
          <Input
            type="number"
            min={1}
            value={sampling}
            onChange={(x) => setSampling(x.target.value)}
            dir="ltr"
          />
        </Field>
        <FieldGroup label={t('exporter.device')} hint={t('exporter.deviceHint')}>
          <DevicePicker
            value={device}
            onChange={setDevice}
            allowNone
            label={t('exporter.device')}
          />
        </FieldGroup>
        <ErrorBox error={m.error} />
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="submit" variant="primary" busy={m.isPending} disabled={!!err}>
            {t('common.save')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function ExportersView({
  exporters,
  error,
}: {
  exporters: FlowExporter[] | undefined;
  error: unknown;
}) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const toast = useToast();
  const [edit, setEdit] = useState<FlowExporter | null>(null);
  const [del, setDel] = useState<FlowExporter | null>(null);
  const setState = useMutation({
    mutationFn: (x: { id: number; state: 'accepted' | 'ignored' }) =>
      endpoints3.updateExporter(x.id, { state: x.state }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk3.flowExporters });
      toast.push('success', t('common.saved'));
    },
  });
  const remove = useMutation({
    mutationFn: (id: number) => endpoints3.deleteExporter(id),
    onSuccess: () => {
      setDel(null);
      void qc.invalidateQueries({ queryKey: qk3.flowExporters });
      toast.push('success', t('common.deleted'));
    },
  });
  return (
    <Card bodyClassName="p-0">
      <ErrorBox error={error ?? setState.error} className="m-3" />
      {exporters && !exporters.length && <Empty>{t('flows.noExporters')}</Empty>}
      {exporters && exporters.length > 0 && (
        <Table>
          <thead>
            <tr>
              <th className={th}>{t('exporter.address')}</th>
              <th className={th}>{t('exporter.state')}</th>
              <th className={th}>{t('common.device')}</th>
              <th className={th}>{t('exporter.protocols')}</th>
              <th className={`${th} text-end`}>{t('exporter.rate')}</th>
              <th className={th}>{t('exporter.sampling')}</th>
              <th className={th}>{t('exporter.lastSeen')}</th>
              {isAdmin && <th className={th} />}
            </tr>
          </thead>
          <tbody>
            {exporters.map((e) => (
              <tr key={e.id}>
                <td className={td}>
                  <bdi dir="ltr" className="font-mono">
                    {e.address}
                  </bdi>
                  {e.both_directions_warning && (
                    <div className="text-xs text-amber-700 dark:text-amber-300">
                      {t('exporter.bothDirections')}
                    </div>
                  )}
                  {e.unsampled_warning && (
                    <div className="text-xs text-amber-700 dark:text-amber-300">
                      {t('exporter.unsampled')}
                    </div>
                  )}
                </td>
                <td className={td}>
                  <span
                    className={cx(
                      'rounded px-1.5 py-0.5 text-xs font-medium',
                      e.state === 'accepted' &&
                        'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-200',
                      e.state === 'pending' &&
                        'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200',
                      e.state === 'ignored' &&
                        'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400',
                    )}
                  >
                    {t(`exporter.states.${e.state}`)}
                  </span>
                </td>
                <td className={td}>
                  {e.device_id ? (
                    <Link
                      to={`/devices/${e.device_id}`}
                      className="text-sky-700 hover:underline dark:text-sky-400"
                    >
                      {e.device_name ?? `#${e.device_id}`}
                    </Link>
                  ) : (
                    '—'
                  )}
                </td>
                <td className={td}>
                  <bdi dir="ltr" className="text-xs">
                    {e.protocols.map((p) => t(`exporter.proto.${p}`)).join(', ')}
                  </bdi>
                </td>
                <td className={`${td} text-end tabular-nums`}>
                  {t('exporter.perSecond', { n: formatCount(e.records_per_s) })}
                </td>
                <td className={`${td} tabular-nums`}>
                  1:{effectiveSampling(e)}
                  {e.sampling_override !== null && (
                    <span className="ms-1 text-xs text-slate-500">({t('exporter.override')})</span>
                  )}
                </td>
                <td className={td}>{formatRelative(e.last_seen)}</td>
                {isAdmin && (
                  <td className={`${td} text-end`}>
                    <span className="inline-flex flex-wrap justify-end gap-1">
                      {e.state !== 'accepted' && (
                        <Button
                          size="sm"
                          variant="primary"
                          onClick={() => setState.mutate({ id: e.id, state: 'accepted' })}
                        >
                          {t('exporter.accept')}
                        </Button>
                      )}
                      {e.state !== 'ignored' && (
                        <Button
                          size="sm"
                          onClick={() => setState.mutate({ id: e.id, state: 'ignored' })}
                        >
                          {t('exporter.ignore')}
                        </Button>
                      )}
                      <Button size="sm" variant="ghost" onClick={() => setEdit(e)}>
                        {t('common.edit')}
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setDel(e)}>
                        {t('common.delete')}
                      </Button>
                    </span>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      <ExporterModal e={edit} onClose={() => setEdit(null)} />
      <ConfirmDialog
        open={!!del}
        title={t('exporter.deleteTitle')}
        message={t('exporter.deleteConfirm', { address: del?.address ?? '' })}
        onConfirm={() => del && remove.mutate(del.id)}
        onClose={() => setDel(null)}
        busy={remove.isPending}
        error={remove.error}
      />
    </Card>
  );
}

function StatusView() {
  const { t } = useTranslation();
  const q = useQuery({
    queryKey: qk3.flowStatus,
    queryFn: endpoints3.flowStatus,
    refetchInterval: 10_000,
  });
  const s = q.data;
  return (
    <div className="space-y-4">
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} />
      {s && (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            {[
              [
                t('flows.st.rate'),
                `${formatCount(s.records_per_s)} / ${formatCount(s.budget_per_s)}`,
              ],
              [t('flows.st.queue'), `${s.queue} / ${s.queue_capacity}`],
              [t('flows.st.templates'), String(s.templates)],
              [
                t('flows.st.storage'),
                s.storage_bytes === null ? '—' : formatBytes(s.storage_bytes),
              ],
            ].map(([k, v]) => (
              <Card key={k} bodyClassName="p-3">
                <div className="text-xs text-slate-500">{k}</div>
                <div className="text-lg font-semibold tabular-nums">
                  <bdi dir="ltr">{v}</bdi>
                </div>
              </Card>
            ))}
          </div>
          <Card title={t('flows.st.receivers')}>
            <ul className="space-y-1 text-sm">
              {s.receivers.map((r) => (
                <li key={r.kind} className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{t(`flows.receiver.${r.kind}`)}</span>
                  <bdi dir="ltr" className="text-xs">
                    UDP {r.port}
                  </bdi>
                  <span
                    className={
                      r.state === 'listening'
                        ? 'text-green-700 dark:text-green-400'
                        : r.state === 'error'
                          ? 'text-red-600'
                          : 'text-slate-500'
                    }
                  >
                    {t(`ingest.state.${r.state}`)}
                  </span>
                  {r.error && (
                    <bdi dir="ltr" className="text-xs text-red-600">
                      {r.error}
                    </bdi>
                  )}
                </li>
              ))}
            </ul>
            <p className="mt-2 text-xs text-slate-500">
              {t('flows.st.rollup', { mode: s.rollup ? t(`flows.rollup.${s.rollup}`) : '—' })}
            </p>
          </Card>
          <Card
            title={t('flows.st.counters')}
            actions={
              <span className="text-xs text-slate-500">
                {t('flows.st.since', { time: formatDateTime(s.counters_since) })}
              </span>
            }
          >
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
                <dt className="text-slate-500">{t('flows.st.datagrams')}</dt>
                <dd className="tabular-nums">{formatCount(s.counters.received_datagrams)}</dd>
                <dt className="text-slate-500">{t('flows.st.records')}</dt>
                <dd className="tabular-nums">{formatCount(s.counters.records)}</dd>
                <dt className="text-slate-500">{t('flows.st.stored')}</dt>
                <dd className="tabular-nums">{formatCount(s.counters.stored_rows)}</dd>
                <dt className="text-slate-500">{t('flows.st.dropped')}</dt>
                <dd className={cx('tabular-nums', totalDropped(s.counters) > 0 && 'text-red-600')}>
                  {formatCount(totalDropped(s.counters))}
                </dd>
              </dl>
              <dl
                className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm"
                data-testid="flow-drops"
              >
                {FLOW_DROP_REASONS.map((r) => (
                  <div key={r} className="contents">
                    <dt className="text-slate-500">{t(`flows.drop.${r}`)}</dt>
                    <dd className={cx('tabular-nums', s.counters.dropped[r] > 0 && 'text-red-600')}>
                      {formatCount(s.counters.dropped[r] ?? 0)}
                    </dd>
                  </div>
                ))}
              </dl>
            </div>
          </Card>
        </>
      )}
    </div>
  );
}

/** [3] Flows (/flows): tops, interface traffic, exporters, receiver status. */
export function FlowsPage() {
  const { t } = useTranslation();
  const { features } = useFeatures();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const p = params.get('tab') as FlowTab | null;
  const tab: FlowTab = p && TABS.includes(p) ? p : 'top';
  const [preset, setPreset] = useState<FlowPreset>('1h');
  const [exporterId, setExporterId] = useState<number | null>(null);
  const range = useRange(preset);
  const on = features.flows;
  const exporters = useQuery({
    queryKey: qk3.flowExporters,
    queryFn: () => endpoints3.flowExporters(),
    enabled: on,
  });
  useLiveMessages(
    ['flows.exporter'],
    () => void qc.invalidateQueries({ queryKey: qk3.flowExporters }),
  );
  const accepted = (exporters.data?.items ?? []).filter((e) => e.state === 'accepted');
  const pending = (exporters.data?.items ?? []).filter((e) => e.state === 'pending').length;
  return (
    <div className="space-y-4">
      <PageHeader
        title={t('flows.title')}
        actions={
          on &&
          (tab === 'top' || tab === 'interfaces') && (
            <>
              <Select
                value={exporterId ?? ''}
                onChange={(e) => setExporterId(e.target.value ? Number(e.target.value) : null)}
                className="w-56"
                aria-label={t('flows.exporter')}
              >
                {tab === 'top' && <option value="">{t('flows.allExporters')}</option>}
                {accepted.map((e) => (
                  <option key={e.id} value={e.id}>
                    {e.device_name ? `${e.device_name} (${e.address})` : e.address}
                  </option>
                ))}
              </Select>
              <Select
                value={preset}
                onChange={(e) => setPreset(e.target.value as FlowPreset)}
                className="w-36"
                aria-label={t('flows.range')}
              >
                {FLOW_PRESETS.map((x) => (
                  <option key={x} value={x}>
                    {t(`flows.preset.${x}`)}
                  </option>
                ))}
              </Select>
            </>
          )
        }
      />
      {!on && <FeatureOffNotice feature="flows" />}
      {on && (
        <>
          <TabBar
            tabs={[...TABS]}
            value={tab}
            label={t('flows.title')}
            onChange={(k) => setParams(k === 'top' ? {} : { tab: k }, { replace: true })}
            render={(k) => (
              <>
                {t(`flows.tab.${k}`)}
                {k === 'exporters' && pending > 0 && (
                  <span className="rounded-full bg-amber-500 px-1.5 text-xs text-white">
                    {pending}
                  </span>
                )}
              </>
            )}
          />
          {tab === 'top' && (
            <TopView
              range={range}
              preset={preset}
              exporterId={exporterId}
              multiExporter={accepted.length > 1}
            />
          )}
          {tab === 'interfaces' && (
            <InterfacesView range={range} exporters={accepted} exporterId={exporterId} />
          )}
          {tab === 'exporters' && (
            <ExportersView exporters={exporters.data?.items} error={exporters.error} />
          )}
          {tab === 'status' && <StatusView />}
        </>
      )}
    </div>
  );
}
