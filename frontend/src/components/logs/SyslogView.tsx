import { useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints } from '../../api/endpoints';
import type { SyslogMessage } from '../../api/types';
import { useAuth } from '../../hooks/auth';
import { useLocalState } from '../../hooks/useLocalState';
import { ruleFromSyslog, type RuleForm } from '../../lib/eventRules';
import { formatDateTime } from '../../lib/format';
import {
  emptyFilter,
  liveAllowed,
  matchesSyslog,
  rangeOf,
  syslogQuery,
  withCursor,
  type LogFilter,
} from '../../lib/logs';
import { DetailRows, Drawer } from '../Drawer';
import { RefreshIcon } from '../icons';
import { Button, Card, cx, Empty, ErrorBox, Loading } from '../ui';
import {
  facilityLabel,
  SeverityBadge,
  SourceCell,
  TopList,
  topSourceItems,
  useGroupDevices,
} from './common';
import { LogFilterBar } from './FilterBar';
import { Histogram, severitySeries } from './Histogram';
import { RuleEditorModal } from './RuleEditor';
import { useRuleName } from './rules';
import { useLogFeed } from './useLogFeed';

const th =
  'sticky top-0 z-10 whitespace-nowrap border-b border-slate-200 bg-white px-2 py-1.5 text-start text-[11px] font-medium uppercase tracking-wide text-slate-500 dark:border-slate-800 dark:bg-slate-900';
const td = 'border-b border-slate-100 px-2 py-1 align-top dark:border-slate-800/70';

export function LiveToggle({
  live,
  allowed,
  onChange,
}: {
  live: boolean;
  allowed: boolean;
  onChange: (v: boolean) => void;
}) {
  const { t } = useTranslation();
  const on = live && allowed;
  return (
    <Button
      size="sm"
      variant={on ? 'primary' : 'secondary'}
      aria-pressed={on}
      disabled={!allowed}
      title={allowed ? t('logs.liveHint') : t('logs.liveNeedsOpenRange')}
      onClick={() => onChange(!live)}
    >
      <span
        className={cx('h-2 w-2 rounded-full', on ? 'animate-pulse bg-white' : 'bg-slate-400')}
      />
      {t('logs.live')}
    </Button>
  );
}

export function NewRecordsBar({ count, onShow }: { count: number; onShow: () => void }) {
  const { t } = useTranslation();
  if (!count) return null;
  return (
    <div className="sticky top-0 z-20 flex justify-center py-1">
      <Button size="sm" variant="primary" onClick={onShow} data-testid="new-records">
        {t('logs.newRecords', { n: count })}
      </Button>
    </div>
  );
}

export function SyslogDetail({
  m,
  onClose,
  onCreateRule,
}: {
  m: SyslogMessage | null;
  onClose: () => void;
  onCreateRule?: (m: SyslogMessage) => void;
}) {
  const { t } = useTranslation();
  const ruleName = useRuleName();
  return (
    <Drawer
      open={!!m}
      onClose={onClose}
      testId="syslog-detail"
      title={t('logs.syslogDetail')}
      footer={
        m &&
        onCreateRule && (
          <Button variant="primary" onClick={() => onCreateRule(m)}>
            {t('logs.createRule')}
          </Button>
        )
      }
    >
      {m && (
        <div className="space-y-4">
          <pre
            dir="ltr"
            className="whitespace-pre-wrap break-words rounded bg-slate-100 p-2 text-left font-mono text-xs dark:bg-slate-800"
          >
            {m.message}
          </pre>
          {m.truncated && (
            <p className="text-xs text-amber-700 dark:text-amber-300">{t('logs.truncated')}</p>
          )}
          <DetailRows
            rows={[
              [t('logs.col.time'), formatDateTime(m.time)],
              [t('logs.deviceTime'), m.device_time ? formatDateTime(m.device_time) : '—'],
              [t('logs.col.source'), <SourceCell r={m} />],
              [t('logs.sourceIp'), <bdi dir="ltr">{m.source_ip}</bdi>],
              [t('logs.col.severity'), <SeverityBadge severity={m.severity} />],
              [
                t('logs.col.facility'),
                <span>
                  <bdi dir="ltr">{facilityLabel(m.facility)}</bdi> ({m.facility}) ·{' '}
                  {t(`syslogFacility.${m.facility}`)}
                </span>,
              ],
              [t('logs.col.host'), <bdi dir="ltr">{m.hostname || '—'}</bdi>],
              [t('logs.col.program'), <bdi dir="ltr">{m.program || '—'}</bdi>],
              [t('logs.pid'), <bdi dir="ltr">{m.pid || '—'}</bdi>],
              [t('logs.msgid'), <bdi dir="ltr">{m.msgid || '—'}</bdi>],
              [t('logs.transport'), `${m.transport.toUpperCase()} · ${m.format}`],
              [
                t('logs.structuredData'),
                m.structured_data ? (
                  <bdi dir="ltr" className="break-all font-mono text-xs">
                    {m.structured_data}
                  </bdi>
                ) : (
                  '—'
                ),
              ],
              [
                t('logs.matchedRules'),
                m.matched_rule_ids.length ? m.matched_rule_ids.map(ruleName).join(', ') : '—',
              ],
            ]}
          />
        </div>
      )}
    </Drawer>
  );
}

/** [2b-3] Syslog: filters, histogram, cursor-paged table, live tail, detail drawer. */
export function SyslogView({ deviceId }: { deviceId?: number }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const [filter, setFilter] = useState<LogFilter>(() =>
    emptyFilter({ deviceId: deviceId ?? null }),
  );
  const [anchor, setAnchor] = useState(() => Date.now());
  const [live, setLive] = useLocalState('netmon.logs.live', false);
  const [selected, setSelected] = useState<SyslogMessage | null>(null);
  const [ruleDraft, setRuleDraft] = useState<RuleForm | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const update = (p: Partial<LogFilter>) => {
    setFilter((f) => ({ ...f, ...p }));
    setAnchor(Date.now());
  };
  const range = rangeOf(filter, anchor);
  const query = range.ok ? syslogQuery(filter, range) : null;
  const groupDevices = useGroupDevices(filter.groupId);
  const canLive = liveAllowed(filter);
  const feed = useLogFeed<SyslogMessage>({
    queryKey: ['syslog', query],
    fetchPage: (c) => endpoints.syslog(withCursor(query!, c)),
    enabled: !!query,
    live: live && canLive,
    topic: 'syslog',
    matches: (m) => matchesSyslog(m, filter, groupDevices),
  });
  const stats = useQuery({
    queryKey: ['syslog-stats', query],
    queryFn: () => endpoints.syslogStats(query!),
    enabled: !!query,
    refetchInterval: live && canLive ? 60_000 : false,
  });
  const series = useMemo(
    () => (stats.data ? severitySeries(stats.data.buckets, (s) => t(`syslogSeverity.${s}`)) : []),
    [stats.data, t],
  );

  return (
    <div className="space-y-3" data-testid="syslog-view">
      <Card bodyClassName="p-3">
        <LogFilterBar
          kind="syslog"
          filter={filter}
          onChange={update}
          fixedDevice={deviceId !== undefined}
          extra={
            <>
              <LiveToggle live={live} allowed={canLive} onChange={setLive} />
              <Button
                size="sm"
                onClick={() => setAnchor(Date.now())}
                aria-label={t('common.refresh')}
              >
                <RefreshIcon size={13} />
              </Button>
            </>
          }
        />
        {!range.ok && (
          <ErrorBox className="mt-2" error={new Error(t(`logs.rangeErr.${range.error}`))} />
        )}
      </Card>
      {stats.data && (
        <Card bodyClassName="p-3">
          <div className="flex flex-wrap items-baseline justify-between gap-2 text-xs text-slate-500">
            <span>{t('logs.total', { n: stats.data.total })}</span>
          </div>
          <Histogram
            times={stats.data.buckets.map((b) => b.t)}
            series={series}
            bucketS={stats.data.bucket_s}
            from={stats.data.since}
            to={stats.data.until}
          />
          <div className="mt-2 grid grid-cols-1 gap-4 sm:grid-cols-2">
            {deviceId === undefined && (
              <TopList
                title={t('logs.topSources')}
                items={topSourceItems(stats.data.top_sources)}
              />
            )}
            <TopList
              title={t('logs.topPrograms')}
              items={stats.data.top_programs.map((p) => ({
                key: p.program,
                label: <bdi dir="ltr">{p.program || '—'}</bdi>,
                count: p.count,
              }))}
            />
          </div>
        </Card>
      )}
      <Card bodyClassName="p-0">
        <ErrorBox error={feed.error} className="m-3" />
        {feed.isLoading && <Loading />}
        <div
          ref={scrollRef}
          className="max-h-[65vh] overflow-auto"
          onScroll={(e) => feed.onScroll(e.currentTarget)}
          data-testid="syslog-scroll"
        >
          <NewRecordsBar
            count={feed.pendingCount}
            onShow={() => {
              scrollRef.current?.scrollTo({ top: 0 });
              feed.showPending();
            }}
          />
          {!feed.isLoading && !feed.items.length ? (
            <Empty>{t('logs.empty')}</Empty>
          ) : (
            <table className="w-full text-xs">
              <thead>
                <tr>
                  <th className={th}>{t('logs.col.time')}</th>
                  {deviceId === undefined && <th className={th}>{t('logs.col.source')}</th>}
                  <th className={th}>{t('logs.col.severity')}</th>
                  <th className={th}>{t('logs.col.facility')}</th>
                  <th className={th}>{t('logs.col.host')}</th>
                  <th className={th}>{t('logs.col.program')}</th>
                  <th className={cx(th, 'w-full')}>{t('logs.col.message')}</th>
                </tr>
              </thead>
              <tbody>
                {feed.items.map((m) => (
                  <tr
                    key={m.id}
                    onClick={() => setSelected(m)}
                    className="cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-800/50"
                  >
                    <td className={cx(td, 'whitespace-nowrap text-slate-500')}>
                      {formatDateTime(m.time)}
                    </td>
                    {deviceId === undefined && (
                      <td className={cx(td, 'whitespace-nowrap')}>
                        <SourceCell r={m} />
                      </td>
                    )}
                    <td className={td}>
                      <SeverityBadge severity={m.severity} />
                    </td>
                    <td className={cx(td, 'whitespace-nowrap')} dir="ltr">
                      {facilityLabel(m.facility)}
                    </td>
                    <td className={cx(td, 'whitespace-nowrap')}>
                      <bdi dir="ltr">{m.hostname}</bdi>
                    </td>
                    <td className={cx(td, 'whitespace-nowrap')}>
                      <bdi dir="ltr">{m.program}</bdi>
                    </td>
                    <td className={td}>
                      <div dir="ltr" className="line-clamp-2 break-all text-left font-mono">
                        {m.message}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {feed.hasMore && (
            <div className="flex justify-center p-2">
              <Button size="sm" busy={feed.loadingMore} onClick={feed.loadMore}>
                {t('logs.loadMore')}
              </Button>
            </div>
          )}
        </div>
      </Card>
      <SyslogDetail
        m={selected}
        onClose={() => setSelected(null)}
        onCreateRule={
          isAdmin
            ? (m) => {
                setSelected(null);
                setRuleDraft(ruleFromSyslog(m));
              }
            : undefined
        }
      />
      {isAdmin && (
        <RuleEditorModal
          open={!!ruleDraft}
          initial={ruleDraft}
          onClose={() => setRuleDraft(null)}
        />
      )}
    </div>
  );
}
