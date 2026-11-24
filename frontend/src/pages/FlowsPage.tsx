import { useMemo, useState } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { endpoints3, qk3 } from '../api/endpoints3';
import type { FlowExporter, FlowSeries, FlowTop, FlowTopItem } from '../api/types3';
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
  SegmentedControl,
  Select,
  TAG,
  TAG_TONE,
  Table,
  td,
  th,
} from '../components/ui';
import { useAuth } from '../hooks/auth';
import { FeatureOffNotice, useFeatures } from '../hooks/features';
import { useLiveMessages } from '../hooks/live';
import { formatDateTime, formatRelative, formatShortTime } from '../lib/format';
import { Banner, ShareBar } from '../components/kit';
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

/** A big number with its unit small after it (prototype .big). */
function BigStat({ label, text }: { label: string; text: string }) {
  const m = /^([-+]?[\d.,]+)\s*(.*)$/.exec(text);
  return (
    <div className="grid gap-1">
      <span className="text-[13px] text-ink-muted">{label}</span>
      <bdi dir="ltr" className="num text-[44px] font-semibold leading-none tracking-[-0.03em]">
        {m ? m[1] : text}
        {m?.[2] && (
          <small className="ms-1 text-[18px] font-medium tracking-normal text-ink-muted">
            {m[2]}
          </small>
        )}
      </bdi>
    </div>
  );
}

/**
 * Traffic over the range as flat bars (prototype "תעבורה": .bars, time left → right): the inbound
 * rate per bucket summed over every series, with five time labels under it.
 */
function TrafficBars({ series }: { series: FlowSeries }) {
  const { t } = useTranslation();
  const sums = new Map<string, number>();
  for (const x of series.series)
    if (x.direction === 'in') for (const [tm, v] of x.points) sums.set(tm, (sums.get(tm) ?? 0) + v);
  const pts = [...sums.entries()].sort(([a], [b]) => (a < b ? -1 : 1));
  const max = Math.max(1, ...pts.map(([, v]) => v));
  const since = Date.parse(series.since);
  const until = Date.parse(series.until);
  const ticks = [0, 1, 2, 3, 4].map((i) => since + ((until - since) * i) / 4);
  return (
    <div className="mt-5" data-testid="flows-bars">
      <div
        className="flex h-[120px] items-end gap-[3px]"
        dir="ltr"
        role="img"
        aria-label={t('flows.overTime')}
      >
        {pts.map(([tm, v]) => (
          <i
            key={tm}
            title={`${formatShortTime(tm)} — ${formatBits(v)}`}
            className="min-h-[3px] flex-1 rounded-b-[2px] rounded-t-[4px] bg-[color-mix(in_srgb,var(--ink)_22%,transparent)]"
            style={{ height: `${(v / max) * 100}%` }}
          />
        ))}
      </div>
      <div dir="ltr" className="num mt-2.5 flex justify-between text-xs text-ink-subtle">
        {ticks.map((x, i) => (
          <span key={i}>
            {i === 4 ? t('flows.now') : formatShortTime(new Date(x).toISOString())}
          </span>
        ))}
      </div>
    </div>
  );
}

/**
 * A top list (prototype "מי מדבר הכי הרבה" / "אפליקציות" / "שיחות"): the name and its volume, a
 * share bar under the name. A table, so each value is a cell.
 */
function TopTable({ top, kind }: { top: FlowTop; kind: 'talkers' | 'apps' | 'conversations' }) {
  const { t } = useTranslation();
  const max = Math.max(1, ...top.items.map((x) => x.bytes));
  const cell = 'border-b border-line py-2.5 align-top';
  return (
    <table className="w-full text-[14.5px] [&_tbody>tr:last-child>td]:border-b-0">
      <thead className="sr-only">
        <tr>
          <th>{t(`flows.col.${kind}`)}</th>
          <th>{t('flows.bytes')}</th>
        </tr>
      </thead>
      <tbody>
        {top.items.map((x: FlowTopItem) => (
          <tr key={x.key} title={`${formatCount(x.packets)} ${t('flows.packets')}`}>
            <td className={cx(cell, 'pe-3')}>
              <div className="grid gap-1.5">
                <span className="flex min-w-0 items-baseline gap-2">
                  <bdi dir="ltr" className="num min-w-0 truncate">
                    {x.label}
                  </bdi>
                  {x.device_id ? (
                    <Link
                      to={`/devices/${x.device_id}`}
                      className="shrink-0 text-xs text-ink-muted hover:text-ink hover:underline"
                    >
                      {t('flows.device')}
                    </Link>
                  ) : null}
                </span>
                <ShareBar pct={(x.bytes / max) * 100} />
              </div>
            </td>
            <td className={cx(cell, 'w-0 whitespace-nowrap text-end')}>
              <b className="num block font-medium">
                <bdi dir="ltr">{formatBytes(x.bytes)}</bdi>
              </b>
              <span className="num text-xs text-ink-subtle">{formatShare(x.share)}</span>
            </td>
          </tr>
        ))}
        {top.other_bytes > 0 && (
          <tr className="text-ink-muted">
            <td className={cell}>{t('flows.other')}</td>
            <td className={cx(cell, 'whitespace-nowrap text-end')}>
              <span className="num block">
                <bdi dir="ltr">{formatBytes(top.other_bytes)}</bdi>
              </span>
              <span className="num text-xs text-ink-subtle">
                {top.total_bytes ? formatShare(top.other_bytes / top.total_bytes) : ''}
              </span>
            </td>
          </tr>
        )}
      </tbody>
    </table>
  );
}

function TopCard({
  title,
  q,
  kind,
  actions,
  children,
  testId,
}: {
  title: string;
  q: { isLoading: boolean; error: unknown; data?: FlowTop };
  kind: 'talkers' | 'apps' | 'conversations';
  actions?: React.ReactNode;
  children?: React.ReactNode;
  testId: string;
}) {
  const { t } = useTranslation();
  return (
    <Card title={title} actions={actions} bodyClassName="px-[22px] pb-3 pt-0">
      <div data-testid={testId}>
        {children}
        {q.isLoading && <Loading />}
        <ErrorBox error={q.error} />
        {q.data && !q.data.items.length && <Empty>{t('flows.noData')}</Empty>}
        {q.data && q.data.items.length > 0 && <TopTable top={q.data} kind={kind} />}
      </div>
    </Card>
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
  const sq = { ...range, exporter_id: exporterId ?? undefined, split: 'none' as const };
  const series = useQuery({
    queryKey: qk3.flowSeries(sq),
    queryFn: () => endpoints3.flowSeries(sq),
    placeholderData: keepPreviousData,
  });
  return (
    <div className="space-y-6">
      {exporterId === null && multiExporter && (
        <Banner tone="warn" title={t('flows.doubleCount')} />
      )}
      <section
        className="rounded-xl bg-surface px-[22px] py-5 shadow-card"
        aria-label={t('flows.overTime')}
      >
        <div className="flex flex-wrap gap-10">
          <BigStat label={t('flows.peakIn')} text={formatBits(peakRate(series.data, 'in'))} />
          <BigStat
            label={t('flows.volume')}
            text={formatBytes(apps.data?.total_bytes ?? talkers.data?.total_bytes ?? 0)}
          />
        </div>
        <ErrorBox error={series.error} />
        {series.data && <TrafficBars series={series.data} />}
      </section>
      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-3">
        <TopCard
          title={t('flows.topTalkers')}
          q={talkers}
          kind="talkers"
          testId="chart-talkers"
          actions={
            <Select
              value={side}
              onChange={(e) => setSide(e.target.value as 'src' | 'dst' | 'both')}
              className="w-auto text-[13px]"
              aria-label={t('flows.side')}
            >
              {(['both', 'src', 'dst'] as const).map((s) => (
                <option key={s} value={s}>
                  {t(`flows.sides.${s}`)}
                </option>
              ))}
            </Select>
          }
        />
        <TopCard title={t('flows.topApps')} q={apps} kind="apps" testId="chart-apps" />
        <TopCard
          title={t('flows.topConversations')}
          q={convOk ? conv : { isLoading: false, error: null }}
          kind="conversations"
          testId="top-conversations"
        >
          {!convOk && (
            <p className="m-0 py-3 text-sm text-ink-muted">{t('flows.conversationsRange')}</p>
          )}
        </TopCard>
      </div>
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
            <span className="pb-2 text-xs text-ink-muted">
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
            <span className="text-xs text-ink-muted">
              {t('flows.peak', {
                in: formatBits(peakRate(series.data, 'in')),
                out: formatBits(peakRate(series.data, 'out')),
              })}
            </span>
          }
        >
          <EChart option={option} className="h-96" testId="chart-interface" />
          <p className="mt-1 text-xs text-ink-muted">{t('flows.mirrorHint')}</p>
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
              <span className="text-status-down">{t(err)}</span>
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
                    <div className="text-xs text-status-warning">
                      {t('exporter.bothDirections')}
                    </div>
                  )}
                  {e.unsampled_warning && (
                    <div className="text-xs text-status-warning">{t('exporter.unsampled')}</div>
                  )}
                </td>
                <td className={td}>
                  <span
                    className={cx(
                      'rounded px-1.5 py-0.5 text-xs font-medium',
                      e.state === 'accepted' && 'bg-status-up-soft text-status-up',
                      e.state === 'pending' && 'bg-status-warning-soft text-status-warning',
                      e.state === 'ignored' && 'bg-surface-sunken text-ink-muted',
                    )}
                  >
                    {t(`exporter.states.${e.state}`)}
                  </span>
                </td>
                <td className={td}>
                  {e.device_id ? (
                    <Link
                      to={`/devices/${e.device_id}`}
                      className="text-accent-ink hover:underline"
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
                <td className={`${td} text-end font-mono text-[13px] tabular-nums`}>
                  {t('exporter.perSecond', { n: formatCount(e.records_per_s) })}
                </td>
                <td className={`${td} tabular-nums`}>
                  1:{effectiveSampling(e)}
                  {e.sampling_override !== null && (
                    <span className="ms-1 text-xs text-ink-muted">({t('exporter.override')})</span>
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
                <div className="text-xs text-ink-muted">{k}</div>
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
                        ? 'text-status-up'
                        : r.state === 'error'
                          ? 'text-status-down'
                          : 'text-ink-muted'
                    }
                  >
                    {t(`ingest.state.${r.state}`)}
                  </span>
                  {r.error && (
                    <bdi dir="ltr" className="text-xs text-status-down">
                      {r.error}
                    </bdi>
                  )}
                </li>
              ))}
            </ul>
            <p className="mt-2 text-xs text-ink-muted">
              {t('flows.st.rollup', { mode: s.rollup ? t(`flows.rollup.${s.rollup}`) : '—' })}
            </p>
          </Card>
          <Card
            title={t('flows.st.counters')}
            actions={
              <span className="text-xs text-ink-muted">
                {t('flows.st.since', { time: formatDateTime(s.counters_since) })}
              </span>
            }
          >
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
                <dt className="text-ink-muted">{t('flows.st.datagrams')}</dt>
                <dd className="tabular-nums">{formatCount(s.counters.received_datagrams)}</dd>
                <dt className="text-ink-muted">{t('flows.st.records')}</dt>
                <dd className="tabular-nums">{formatCount(s.counters.records)}</dd>
                <dt className="text-ink-muted">{t('flows.st.stored')}</dt>
                <dd className="tabular-nums">{formatCount(s.counters.stored_rows)}</dd>
                <dt className="text-ink-muted">{t('flows.st.dropped')}</dt>
                <dd
                  className={cx('tabular-nums', totalDropped(s.counters) > 0 && 'text-status-down')}
                >
                  {formatCount(totalDropped(s.counters))}
                </dd>
              </dl>
              <dl
                className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm"
                data-testid="flow-drops"
              >
                {FLOW_DROP_REASONS.map((r) => (
                  <div key={r} className="contents">
                    <dt className="text-ink-muted">{t(`flows.drop.${r}`)}</dt>
                    <dd
                      className={cx(
                        'tabular-nums',
                        s.counters.dropped[r] > 0 && 'text-status-down',
                      )}
                    >
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
        subtitle={on && t('flows.subline', { count: accepted.length })}
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
            </>
          )
        }
      />
      {!on && <FeatureOffNotice feature="flows" />}
      {on && (
        <>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <TabBar
              tabs={[...TABS]}
              value={tab}
              label={t('flows.title')}
              onChange={(k) => setParams(k === 'top' ? {} : { tab: k }, { replace: true })}
              render={(k) => (
                <>
                  {t(`flows.tab.${k}`)}
                  {k === 'exporters' && pending > 0 && (
                    <span className={cx(TAG, TAG_TONE.warning)}>{pending}</span>
                  )}
                </>
              )}
            />
            {(tab === 'top' || tab === 'interfaces') && (
              // the range (prototype: segmented שעה / 6 שעות / 24 שעות / 7 ימים)
              <SegmentedControl
                label={t('flows.range')}
                size="sm"
                value={preset}
                onChange={setPreset}
                options={FLOW_PRESETS.map((x) => ({ value: x, label: t(`flows.preset.${x}`) }))}
              />
            )}
          </div>
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
