import { useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../../api/endpoints';
import type { DevicePorts, PortDetail, PortMac, PortNeighbor, Sensor } from '../../api/types';
import { formatDateTime, formatNumber, formatRelative, formatValue } from '../../lib/format';
import { errorsDelta, formatSpeed, formatVlanList, tileView } from '../../lib/ports';
import { useAuth } from '../../hooks/auth';
import { refetchIfInvalidated } from '../../lib/liveCache';
import { DeviceClassIcon } from '../discovery/Badges';
import { HistoryChart } from '../HistoryChart';
import { AlertIcon, PlayIcon } from '../icons';
import { StatusDot } from '../Status';
import { Button, cx, ErrorBox, Loading, Modal, Notice } from '../ui';
import { TileGlyph } from './Faceplate';
import { usePortText } from './text';
import { NewdevPortToggle } from '../newdevices/NewdevPortToggle';

function Section({
  title,
  children,
  className,
}: {
  title: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cx('min-w-0', className)}>
      <h4 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">
        {title}
      </h4>
      {children}
    </section>
  );
}

function Rows({ rows }: { rows: [ReactNode, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-sm">
      {rows.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="text-slate-500">{k}</dt>
          <dd className="min-w-0 break-words text-slate-800 dark:text-slate-200">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

const IF_CHANNELS = ['in_bps', 'out_bps', 'in_errors_ps', 'out_errors_ps'] as const;

function TrafficChart({ sensor }: { sensor: Sensor }) {
  const { t } = useTranslation();
  const [channel, setChannel] = useState<(typeof IF_CHANNELS)[number]>('in_bps');
  const history = useQuery({
    queryKey: qk.history(sensor.id, '24h'),
    queryFn: () => endpoints.history(sensor.id, '24h'),
    placeholderData: keepPreviousData,
  });
  const available = IF_CHANNELS.filter((c) => sensor.channels.some((x) => x.key === c));
  const hc = history.data?.channels.find((c) => c.key === channel);
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-1" role="tablist">
        {available.map((c) => {
          const last = sensor.channels.find((x) => x.key === c);
          return (
            <button
              key={c}
              type="button"
              role="tab"
              aria-selected={channel === c}
              onClick={() => setChannel(c)}
              className={cx(
                'rounded px-2 py-1 text-xs',
                channel === c
                  ? 'bg-sky-100 font-medium text-sky-900 dark:bg-sky-900/50 dark:text-sky-100'
                  : 'text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800',
              )}
            >
              {t(`channel.${c}`)}{' '}
              <bdi dir="ltr" className="tabular-nums text-slate-500">
                {formatValue(last?.last_value, last?.unit ?? '')}
              </bdi>
            </button>
          );
        })}
        <Link
          to={`/sensors/${sensor.id}`}
          className="ms-auto text-xs text-sky-700 hover:underline dark:text-sky-400"
        >
          {t('ports.openSensor')}
        </Link>
      </div>
      {history.isLoading && <Loading />}
      <ErrorBox error={history.error} />
      {history.data && hc && (
        <HistoryChart
          points={hc.points}
          bucketS={history.data.bucket_s}
          unit={hc.unit}
          label={t(`channel.${channel}`)}
          from={history.data.from}
          to={history.data.to}
          heightClass="h-40"
        />
      )}
    </div>
  );
}

function NeighborRow({ n }: { n: PortNeighbor }) {
  const { t } = useTranslation();
  return (
    <li className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-sm">
      {n.device_class && <DeviceClassIcon cls={n.device_class} />}
      {n.status && <StatusDot status={n.status} />}
      {n.kind === 'device' && n.device_id ? (
        <Link
          to={`/devices/${n.device_id}`}
          className="font-medium text-sky-700 hover:underline dark:text-sky-400"
        >
          <bdi dir="auto">{n.name}</bdi>
        </Link>
      ) : (
        <bdi dir="auto" className="font-medium">
          {n.name}
        </bdi>
      )}
      {n.port && (
        <span className="text-slate-500">
          {t('ports.remotePort')} <bdi dir="ltr">{n.port}</bdi>
        </span>
      )}
      {n.mgmt_address && (
        <bdi dir="ltr" className="text-xs text-slate-500">
          {n.mgmt_address}
        </bdi>
      )}
      <span className="rounded bg-slate-100 px-1 text-[10px] font-medium uppercase text-slate-600 dark:bg-slate-800 dark:text-slate-300">
        {t(`topology.source.${n.source}`)}
      </span>
    </li>
  );
}

function MacTable({ macs }: { macs: PortMac[] }) {
  const { t } = useTranslation();
  const th =
    'whitespace-nowrap border-b border-slate-200 px-2 py-1.5 text-start text-[11px] font-medium uppercase tracking-wide text-slate-500 dark:border-slate-800';
  const td =
    'whitespace-nowrap border-b border-slate-100 px-2 py-1.5 align-top dark:border-slate-800/70';
  return (
    <div className="overflow-x-auto" data-testid="mac-table">
      <table className="w-full text-xs">
        <thead>
          <tr>
            <th className={th}>{t('ports.mac.mac')}</th>
            <th className={th}>{t('ports.mac.vendor')}</th>
            <th className={th}>{t('ports.mac.vlan')}</th>
            <th className={th}>{t('ports.mac.ip')}</th>
            <th className={th}>{t('ports.mac.hostname')}</th>
            <th className={th}>{t('ports.mac.device')}</th>
            <th className={th}>{t('ports.mac.firstSeen')}</th>
            <th className={th}>{t('ports.mac.lastSeen')}</th>
          </tr>
        </thead>
        <tbody>
          {macs.map((m) => (
            <tr key={`${m.mac}-${m.vlan}`}>
              <td className={cx(td, 'font-mono')}>
                <bdi dir="ltr">{m.mac}</bdi>
                {m.via_lag && (
                  <span
                    className="ms-1 font-sans text-[10px] text-violet-700 dark:text-violet-300"
                    title={t('ports.lag.macViaLag')}
                  >
                    ({t('ports.lag.viaLag')})
                  </span>
                )}
              </td>
              <td className={td}>
                {m.locally_administered ? (
                  <span className="text-slate-500">{t('ports.mac.random')}</span>
                ) : (
                  <bdi dir="auto">{m.vendor ?? '—'}</bdi>
                )}
              </td>
              <td className={cx(td, 'tabular-nums')}>{m.vlan ?? '—'}</td>
              <td className={td}>
                {m.ip ? (
                  <bdi
                    dir="ltr"
                    className={m.ips.length ? undefined : 'text-slate-400'}
                    title={m.ips.length ? m.ips.join(', ') : t('ports.mac.lastKnownIp')}
                  >
                    {m.ip}
                    {m.ips.length > 1 && ` +${m.ips.length - 1}`}
                  </bdi>
                ) : (
                  '—'
                )}
              </td>
              <td className={td}>
                <bdi dir="ltr">{m.hostname ?? '—'}</bdi>
              </td>
              <td className={td}>
                {m.device_id ? (
                  <Link
                    to={`/devices/${m.device_id}`}
                    className="text-sky-700 hover:underline dark:text-sky-400"
                  >
                    <bdi dir="auto">{m.device_name ?? `#${m.device_id}`}</bdi>
                  </Link>
                ) : (
                  '—'
                )}
              </td>
              <td className={td} title={formatDateTime(m.first_seen)}>
                {formatRelative(m.first_seen)}
              </td>
              <td className={td} title={formatDateTime(m.last_seen)}>
                {formatRelative(m.last_seen)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** [2b-3] "Member of Trk1" (link to the LAG) or, on a LAG row, its member ports. */
function LagSection({
  deviceId,
  p,
  onOpenPort,
}: {
  deviceId: number;
  p: PortDetail;
  onOpenPort?: (ifIndex: number) => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const all = qc.getQueryData<DevicePorts>(qk.devicePorts(deviceId))?.ports ?? [];
  const link = (ifIndex: number, label: string) =>
    onOpenPort ? (
      <button
        type="button"
        onClick={() => onOpenPort(ifIndex)}
        className="text-sky-700 hover:underline dark:text-sky-400"
      >
        <bdi dir="ltr">{label}</bdi>
      </button>
    ) : (
      <bdi dir="ltr">{label}</bdi>
    );
  if (p.lag)
    return (
      <div
        className="rounded-md bg-violet-50 px-3 py-2 text-sm ring-1 ring-inset ring-violet-200 dark:bg-violet-950/30 dark:ring-violet-900"
        data-testid="lag-section"
      >
        {t('ports.lag.memberOfPrefix')} {link(p.lag.if_index, p.lag.name)}
        {p.lag.attached === false && (
          <span className="ms-2 text-xs font-medium text-amber-700 dark:text-amber-300">
            {t('ports.lag.inactive')}
          </span>
        )}
        {p.lag.attached === null && (
          <span className="ms-2 text-xs text-slate-500">{t('ports.lag.stackOnly')}</span>
        )}
        {p.vlans_from_lag && (
          <div className="text-xs text-slate-500">{t('ports.lag.vlansFromLag')}</div>
        )}
      </div>
    );
  if (p.kind === 'lag' && p.lag_members?.length) {
    const byIndex = new Map(all.map((x) => [x.if_index, x]));
    return (
      <div
        className="rounded-md bg-violet-50 px-3 py-2 text-sm ring-1 ring-inset ring-violet-200 dark:bg-violet-950/30 dark:ring-violet-900"
        data-testid="lag-section"
      >
        {t('ports.lag.membersPrefix')}{' '}
        {[...p.lag_members]
          .sort((a, b) => a - b)
          .map((i, n) => {
            const m = byIndex.get(i);
            return (
              <span key={i}>
                {n > 0 && ', '}
                {link(i, m ? m.name || m.descr || String(i) : String(i))}
                {m?.lag?.attached === false && (
                  <span className="text-xs text-amber-700"> ({t('ports.lag.inactive')})</span>
                )}
              </span>
            );
          })}
      </div>
    );
  }
  return null;
}

function Body({
  deviceId,
  p,
  showAll,
  onShowAll,
  onOpenPort,
}: {
  deviceId: number;
  p: PortDetail;
  showAll: boolean;
  onShowAll: () => void;
  onOpenPort?: (ifIndex: number) => void;
}) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const text = usePortText();
  const v = tileView(p);
  const startMonitoring = useMutation({
    mutationFn: () =>
      endpoints.createSensor({
        device_id: deviceId,
        type: 'snmp_interface',
        name: p.name || p.descr || `if${p.if_index}`,
        config: { if_index: p.if_index, if_name: p.name || p.descr },
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['port-detail', deviceId] });
      void qc.invalidateQueries({ queryKey: qk.devicePorts(deviceId) });
      void qc.invalidateQueries({ queryKey: ['sensors'] });
      void qc.invalidateQueries({ queryKey: qk.tree });
    },
  });
  const errs = errorsDelta(p);
  const dash = <span className="text-slate-400">—</span>;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <TileGlyph kind={v.kind} icon={v.icon} trunk={v.trunk} badge={v.badge} />
        <span className="font-medium">{text.state(p)}</span>
        {p.state === 'up' && <span className="text-slate-500">· {text.kindLabel(p)}</span>}
        {p.is_uplink && (
          <span className="rounded bg-sky-50 px-1.5 py-0.5 text-[11px] font-medium text-sky-800 ring-1 ring-inset ring-sky-200 dark:bg-sky-950 dark:text-sky-300 dark:ring-sky-900">
            {t(`ports.uplinkReason.${p.uplink_reason ?? 'neighbor'}`)}
          </span>
        )}
        {p.alert && (
          <span className="inline-flex items-center gap-1 rounded bg-orange-50 px-1.5 py-0.5 text-[11px] font-medium text-orange-800 ring-1 ring-inset ring-orange-200 dark:bg-orange-950/50 dark:text-orange-300 dark:ring-orange-900">
            <AlertIcon size={12} />
            {p.alert_reasons.map((r) => t(`ports.alertReason.${r}`)).join(', ')}
          </span>
        )}
      </div>

      <LagSection deviceId={deviceId} p={p} onOpenPort={onOpenPort} />

      <div className="grid grid-cols-1 gap-5 sm:grid-cols-2">
        <Section title={t('ports.section.state')}>
          <Rows
            rows={[
              [t('ports.field.admin'), text.ifStatus(p.admin_status)],
              [t('ports.field.oper'), text.ifStatus(p.oper_status)],
              [
                t('ports.field.lastChange'),
                p.last_change_at ? (
                  <span title={formatDateTime(p.last_change_at)}>
                    {formatRelative(p.last_change_at)}
                  </span>
                ) : (
                  dash
                ),
              ],
              [t('ports.field.ifIndex'), <span className="tabular-nums">{p.if_index}</span>],
              [t('ports.field.kind'), t(`ports.kind.${p.kind}`)],
              [
                t('ports.field.mac'),
                p.mac ? (
                  <bdi dir="ltr" className="font-mono text-xs">
                    {p.mac}
                  </bdi>
                ) : (
                  dash
                ),
              ],
            ]}
          />
        </Section>
        <Section title={t('ports.section.link')}>
          <Rows
            rows={[
              [t('ports.field.speed'), <bdi dir="ltr">{formatSpeed(p.speed_bps) ?? '—'}</bdi>],
              [t('ports.field.duplex'), p.duplex ? t(`ports.duplex.${p.duplex}`) : dash],
              [t('ports.field.mode'), text.mode(p)],
              [t('ports.field.pvid'), p.pvid ?? dash],
              [
                t('ports.field.untagged'),
                p.untagged_vlans.length ? (
                  <bdi dir="ltr">{formatVlanList(p.untagged_vlans)}</bdi>
                ) : (
                  dash
                ),
              ],
              [
                t('ports.field.tagged'),
                p.tagged_vlans.length ? (
                  <bdi dir="ltr">{formatVlanList(p.tagged_vlans)}</bdi>
                ) : (
                  dash
                ),
              ],
            ]}
          />
        </Section>
        <Section title={t('ports.section.poe')}>
          {p.poe ? (
            <Rows
              rows={[
                [
                  t('ports.field.poeAdmin'),
                  p.poe.admin_enabled === null
                    ? dash
                    : p.poe.admin_enabled
                      ? t('ports.poeEnabled')
                      : t('ports.poeDisabled'),
                ],
                [
                  t('ports.field.poeDetection'),
                  p.poe.detection ? (
                    <span
                      className={
                        p.poe.detection === 'fault' || p.poe.detection === 'other_fault'
                          ? 'font-medium text-orange-600 dark:text-orange-400'
                          : undefined
                      }
                    >
                      {t(`ports.poeDetection.${p.poe.detection}`)}
                    </span>
                  ) : (
                    dash
                  ),
                ],
                [t('ports.field.poeClass'), p.poe.power_class ?? dash],
                [
                  t('ports.field.poePower'),
                  p.poe.power_w !== null ? (
                    <bdi dir="ltr">{formatNumber(p.poe.power_w, 1)} W</bdi>
                  ) : (
                    dash
                  ),
                ],
              ]}
            />
          ) : (
            <p className="text-sm text-slate-500">{t('ports.noPoe')}</p>
          )}
        </Section>
        <Section title={t('ports.section.traffic')}>
          <Rows
            rows={[
              [t('ports.field.rx'), <bdi dir="ltr">{formatValue(p.in_bps, 'bit/s')}</bdi>],
              [t('ports.field.tx'), <bdi dir="ltr">{formatValue(p.out_bps, 'bit/s')}</bdi>],
              [
                t('ports.field.errors'),
                errs === null ? (
                  dash
                ) : (
                  <span className={errs > 0 ? 'font-medium text-orange-600' : undefined}>
                    {errs}
                  </span>
                ),
              ],
            ]}
          />
          {!p.sensor && (
            <div className="mt-2 space-y-1.5">
              <p className="text-xs text-slate-500">{t('ports.noSensor')}</p>
              {isAdmin && (
                <Button
                  size="sm"
                  variant="primary"
                  busy={startMonitoring.isPending}
                  onClick={() => startMonitoring.mutate()}
                >
                  <PlayIcon size={12} className="rtl:rotate-180" /> {t('ports.startMonitoring')}
                </Button>
              )}
              <ErrorBox error={startMonitoring.error} />
            </div>
          )}
        </Section>
      </div>

      {p.sensor && (
        <Section
          title={
            <span className="inline-flex items-center gap-1.5">
              {t('ports.section.history')} <StatusDot status={p.sensor.status} />
            </span>
          }
        >
          <TrafficChart sensor={p.sensor} />
        </Section>
      )}

      <Section title={t('ports.section.connected')}>
        <div className="space-y-3">
          {p.neighbors.length > 0 ? (
            <ul className="space-y-1">
              {p.neighbors.map((n) => (
                <NeighborRow key={n.link_id} n={n} />
              ))}
            </ul>
          ) : (
            <p className="text-sm text-slate-500">{t('ports.noNeighbor')}</p>
          )}
          {p.is_uplink && (p.macs_hidden || showAll) && (
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span>{t('ports.uplinkMacs', { count: p.mac_count })}</span>
              {p.macs_hidden && p.mac_count > 0 && (
                <Button size="sm" onClick={onShowAll}>
                  {t('ports.showAllMacs')}
                </Button>
              )}
            </div>
          )}
          {!p.macs_hidden && p.macs.length > 0 && <MacTable macs={p.macs} />}
          {!p.macs_hidden && p.macs.length === 0 && !p.is_uplink && (
            <p className="text-sm text-slate-500">{t('ports.noMacs')}</p>
          )}
        </div>
      </Section>
    </div>
  );
}

export function PortModal({
  deviceId,
  ifIndex,
  onClose,
  onOpenPort,
}: {
  deviceId: number;
  ifIndex: number | null;
  onClose: () => void;
  /** [2b-3] open another port's modal (LAG ⇄ members) */
  onOpenPort?: (ifIndex: number) => void;
}) {
  const { t } = useTranslation();
  const [showAllFor, setShowAllFor] = useState<number | null>(null);
  const showAll = showAllFor !== null && showAllFor === ifIndex;
  const q = useQuery({
    queryKey: qk.portDetail(deviceId, ifIndex ?? -1, showAll),
    queryFn: () => endpoints.portDetail(deviceId, ifIndex as number, showAll),
    enabled: ifIndex !== null,
    refetchOnWindowFocus: refetchIfInvalidated,
    placeholderData: (prev) => (prev && prev.if_index === ifIndex ? prev : undefined),
  });
  const p = q.data;
  return (
    <Modal
      open={ifIndex !== null}
      onClose={onClose}
      wide
      closeOnBackdrop
      testId="port-modal"
      title={
        p ? (
          <span className="flex min-w-0 flex-wrap items-baseline gap-x-2">
            <bdi dir="ltr">{t('ports.portN', { name: p.name || p.descr || p.if_index })}</bdi>
            {p.alias && (
              <bdi dir="auto" className="truncate text-sm font-normal text-slate-500">
                {p.alias}
              </bdi>
            )}
          </span>
        ) : (
          t('ports.portN', { name: ifIndex ?? '' })
        )
      }
    >
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} />
      {p && (
        <>
          {q.isFetching && showAll && p.macs_hidden && <Notice>{t('common.loading')}</Notice>}
          <Body
            deviceId={deviceId}
            p={p}
            showAll={showAll}
            onShowAll={() => setShowAllFor(ifIndex)}
            onOpenPort={onOpenPort}
          />
          <NewdevPortToggle deviceId={deviceId} ifIndex={p.if_index} />
        </>
      )}
    </Modal>
  );
}
