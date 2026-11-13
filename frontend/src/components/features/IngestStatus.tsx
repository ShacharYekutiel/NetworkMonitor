import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../../api/endpoints';
import type { IngestCounters, IngestStatus, ReceiverStatus } from '../../api/types';
import { DROP_REASONS } from '../../api/types';
import { useFeatures } from '../../hooks/features';
import { ingestOn } from '../../lib/features';
import { formatDateTime, formatNumber, formatRelative } from '../../lib/format';
import { Card, cx, ErrorBox, Loading, Table, td, th } from '../ui';

export function useIngestStatus(refetch = 30_000) {
  const { features } = useFeatures();
  return useQuery({
    queryKey: qk.ingestStatus,
    queryFn: endpoints.ingestStatus,
    enabled: ingestOn(features),
    refetchInterval: refetch,
  });
}

const STATE_CLS: Record<ReceiverStatus['state'], string> = {
  listening: 'text-status-up',
  error: 'text-status-down',
  disabled: 'text-ink-muted',
};

function proto(k: ReceiverStatus['kind']) {
  return k === 'syslog_tcp' ? 'TCP' : 'UDP';
}

/** One line for page headers: "Syslog UDP 514 listening · 12/min". */
export function IngestStatusLine() {
  const { t } = useTranslation();
  const { features } = useFeatures();
  const q = useIngestStatus();
  if (!ingestOn(features) || !q.data) return null;
  const d = q.data;
  return (
    <span className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs" data-testid="ingest-line">
      {d.receivers
        .filter((r) => (r.kind === 'trap' ? features.traps : features.syslog))
        .map((r) => (
          <span key={r.kind} className={STATE_CLS[r.state]} title={r.error ?? undefined}>
            {t(`ingest.receiver.${r.kind}`)}{' '}
            <bdi dir="ltr">
              {proto(r.kind)} {r.port}
            </bdi>
            : {t(`ingest.state.${r.state}`)}
          </span>
        ))}
      {features.syslog && (
        <span>
          {t('ingest.ratePerMin', {
            kind: t('logs.tab.syslog'),
            n: formatNumber(d.rate_per_min.syslog, 1),
          })}
        </span>
      )}
      {features.traps && (
        <span>
          {t('ingest.ratePerMin', {
            kind: t('logs.tab.traps'),
            n: formatNumber(d.rate_per_min.trap, 1),
          })}
        </span>
      )}
      {d.nat_suspected && <span className="text-status-warning">{t('ingest.natShort')}</span>}
    </span>
  );
}

function Counters({ title, c }: { title: string; c: IngestCounters }) {
  const { t } = useTranslation();
  const dropped = DROP_REASONS.filter((r) => (c.dropped[r] ?? 0) > 0);
  const total = DROP_REASONS.reduce((a, r) => a + (c.dropped[r] ?? 0), 0);
  return (
    <div className="min-w-0">
      <div className="mb-1 text-xs font-semibold text-ink-muted">{title}</div>
      <dl className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-xs">
        <dt className="text-ink-muted">{t('ingest.received')}</dt>
        <dd className="tabular-nums">{formatNumber(c.received, 0)}</dd>
        <dt className="text-ink-muted">{t('ingest.stored')}</dt>
        <dd className="tabular-nums">{formatNumber(c.stored, 0)}</dd>
        <dt className="text-ink-muted">{t('ingest.forwarded')}</dt>
        <dd className="tabular-nums">{formatNumber(c.forwarded, 0)}</dd>
        <dt className="text-ink-muted">{t('ingest.dropped')}</dt>
        <dd className={cx('tabular-nums', total > 0 && 'font-semibold text-status-warning')}>
          {formatNumber(total, 0)}
        </dd>
        {dropped.map((r) => (
          <div key={r} className="contents">
            <dt className="ps-3 text-ink-muted">{t(`ingest.drop.${r}`)}</dt>
            <dd className="tabular-nums">{formatNumber(c.dropped[r], 0)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

export function IngestStatusBody({ d }: { d: IngestStatus }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-4" data-testid="ingest-status">
      {d.nat_suspected && (
        <div className="rounded-md bg-status-warning-soft px-3 py-2 text-sm text-status-warning">
          {t('ingest.nat', { addr: d.nat_address ?? '?' })}
        </div>
      )}
      {!d.trgm && <p className="text-xs text-ink-muted">{t('ingest.noTrgm')}</p>}
      <Table>
        <thead>
          <tr>
            <th className={th}>{t('ingest.receiverCol')}</th>
            <th className={th}>{t('ingest.port')}</th>
            <th className={th}>{t('ingest.stateCol')}</th>
            <th className={th}>{t('ingest.since')}</th>
          </tr>
        </thead>
        <tbody>
          {d.receivers.map((r) => (
            <tr key={r.kind}>
              <td className={td}>{t(`ingest.receiver.${r.kind}`)}</td>
              <td className={td}>
                <bdi dir="ltr">
                  {proto(r.kind)} {r.port || '—'}
                </bdi>
              </td>
              <td className={cx(td, STATE_CLS[r.state])}>
                {t(`ingest.state.${r.state}`)}
                {r.error && (
                  <div className="text-xs">
                    <bdi dir="ltr">{r.error}</bdi>
                  </div>
                )}
              </td>
              <td className={cx(td, 'text-ink-muted')} title={formatDateTime(r.since)}>
                {r.since ? formatRelative(r.since) : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </Table>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <Counters title={t('logs.tab.syslog')} c={d.counters.syslog} />
        <Counters title={t('logs.tab.traps')} c={d.counters.trap} />
        <div className="text-xs">
          <dl className="grid grid-cols-2 gap-x-3 gap-y-0.5">
            <dt className="text-ink-muted">{t('ingest.rateSyslog')}</dt>
            <dd className="tabular-nums">{formatNumber(d.rate_per_min.syslog, 1)}</dd>
            <dt className="text-ink-muted">{t('ingest.rateTrap')}</dt>
            <dd className="tabular-nums">{formatNumber(d.rate_per_min.trap, 1)}</dd>
            <dt className="text-ink-muted">{t('ingest.queue')}</dt>
            <dd className="tabular-nums">
              {d.queue.syslog + d.queue.trap} / {d.queue.capacity}
            </dd>
            <dt className="text-ink-muted">{t('ingest.tcpConnections')}</dt>
            <dd className="tabular-nums">{d.tcp_connections}</dd>
          </dl>
          <p className="mt-2 text-ink-muted">
            {t('ingest.countersSince', { time: formatDateTime(d.counters_since) })}
          </p>
        </div>
      </div>
      {d.v3_failures.length > 0 && (
        <div>
          <h4 className="mb-1 text-xs font-semibold text-ink-muted">{t('ingest.v3Failures')}</h4>
          <Table>
            <thead>
              <tr>
                <th className={th}>{t('logs.col.time')}</th>
                <th className={th}>{t('logs.sourceIp')}</th>
                <th className={th}>{t('ingest.engineId')}</th>
                <th className={th}>{t('users.username')}</th>
                <th className={th}>{t('ingest.reason')}</th>
              </tr>
            </thead>
            <tbody>
              {d.v3_failures.map((f, i) => (
                <tr key={i}>
                  <td className={cx(td, 'whitespace-nowrap')}>{formatDateTime(f.time)}</td>
                  <td className={td}>
                    <bdi dir="ltr">{f.source_ip}</bdi>
                  </td>
                  <td className={cx(td, 'font-mono text-xs')}>
                    <bdi dir="ltr">{f.engine_id}</bdi>
                  </td>
                  <td className={td}>
                    <bdi dir="ltr">{f.username}</bdi>
                  </td>
                  <td className={td}>
                    <bdi dir="ltr">{f.reason}</bdi>
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        </div>
      )}
    </div>
  );
}

export function IngestStatusCard() {
  const { t } = useTranslation();
  const q = useIngestStatus(15_000);
  return (
    <Card title={t('ingest.title')}>
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} />
      {q.data && <IngestStatusBody d={q.data} />}
    </Card>
  );
}
