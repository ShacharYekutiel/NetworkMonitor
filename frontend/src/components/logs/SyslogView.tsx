import { useCallback, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints } from '../../api/endpoints';
import type { SyslogMessage } from '../../api/types';
import { useAuth } from '../../hooks/auth';
import { useLiveMessages } from '../../hooks/live';
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
  withRepeat,
  type LogFilter,
} from '../../lib/logs';
import { DetailRows, Drawer } from '../Drawer';
import { RefreshIcon } from '../icons';
import { Button, Card, cx, Empty, ErrorBox, Loading, TAG, TAG_TONE } from '../ui';
import {
  facilityLabel,
  LogRow,
  LogTable,
  SeverityBadge,
  severityDot,
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
      <span className={cx('h-2 w-2 rounded-full', on ? 'bg-accent-on' : 'bg-status-paused')} />
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

/** [syslog-protection] "×37": identical messages folded into this row. */
export function RepeatBadge({ n, lastSeen }: { n: number; lastSeen?: string | null }) {
  const { t } = useTranslation();
  if (n <= 1) return null;
  return (
    <span
      className={cx(TAG, TAG_TONE.accent, 'num')}
      title={t('logs.repeatTitle', {
        n,
        time: lastSeen ? formatDateTime(lastSeen) : '—',
      })}
      data-testid="repeat-count"
    >
      <bdi dir="ltr">×{n}</bdi>
    </span>
  );
}

type Repeats = Record<number, { repeat_count: number; last_seen: string }>;

/** Live repeat counts (`syslog.repeats`) over the rows already on screen. */
function useRepeats(live: boolean): Repeats {
  const [rep, setRep] = useState<Repeats>({});
  const onMsg = useCallback((m: { type: string; data: unknown }) => {
    if (m.type !== 'syslog.repeats') return;
    const items = (m.data as { items: { id: number; repeat_count: number; last_seen: string }[] })
      .items;
    setRep((prev) => {
      const next = { ...prev };
      for (const x of items) next[x.id] = { repeat_count: x.repeat_count, last_seen: x.last_seen };
      return next;
    });
  }, []);
  useLiveMessages(['syslog.repeats'], onMsg, live ? 'syslog' : null);
  return rep;
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
            className="whitespace-pre-wrap break-words rounded bg-surface-sunken p-2 text-left font-mono text-xs"
          >
            {m.message}
          </pre>
          {m.truncated && <p className="text-xs text-status-warning">{t('logs.truncated')}</p>}
          <DetailRows
            rows={[
              [t('logs.col.time'), formatDateTime(m.time)],
              ...(m.repeat_count > 1
                ? ([
                    [
                      t('logs.repeats'),
                      <span data-testid="detail-repeats">
                        {t('logs.repeatsValue', {
                          n: m.repeat_count,
                          first: formatDateTime(m.time),
                          last: m.last_seen ? formatDateTime(m.last_seen) : '—',
                        })}
                      </span>,
                    ],
                  ] as [string, React.ReactNode][])
                : []),
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
  const repeats = useRepeats(live && canLive);
  const withRepeats = (m: SyslogMessage): SyslogMessage => withRepeat(m, repeats[m.id]);
  const series = useMemo(
    () => (stats.data ? severitySeries(stats.data.buckets, (s) => t(`syslogSeverity.${s}`)) : []),
    [stats.data, t],
  );

  return (
    <div className="space-y-4" data-testid="syslog-view">
      <Card bodyClassName="px-5 py-4">
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
        <Card bodyClassName="px-5 py-4">
          <div className="flex flex-wrap items-baseline justify-between gap-2 text-[13px] text-ink-muted">
            <span>
              {t('logs.total', { n: stats.data.total })}
              {stats.data.rows < stats.data.total && (
                <> · {t('logs.rowsStored', { n: stats.data.rows })}</>
              )}
            </span>
          </div>
          <Histogram
            times={stats.data.buckets.map((b) => b.t)}
            series={series}
            bucketS={stats.data.bucket_s}
            from={stats.data.since}
            to={stats.data.until}
          />
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
            <LogTable head={[t('logs.col.severity'), t('logs.col.message'), t('logs.col.time')]}>
              {feed.items.map(withRepeats).map((m) => (
                <LogRow
                  key={m.id}
                  dot={severityDot(m.severity)}
                  onClick={() => setSelected(m)}
                  title={m.message}
                  time={m.time}
                  sub={
                    <>
                      {m.repeat_count > 1 && (
                        <>
                          <RepeatBadge n={m.repeat_count} lastSeen={m.last_seen} />{' '}
                        </>
                      )}
                      {deviceId === undefined && (
                        <>
                          <SourceCell r={m} />
                          {' · '}
                        </>
                      )}
                      {t(`syslogSeverity.${m.severity}`)}
                      {m.program && (
                        <>
                          {' · '}
                          <bdi dir="ltr">{m.program}</bdi>
                        </>
                      )}
                      {' · '}
                      <bdi dir="ltr">{facilityLabel(m.facility)}</bdi>
                      {m.hostname && m.hostname !== m.device_name && (
                        <>
                          {' · '}
                          <bdi dir="ltr">{m.hostname}</bdi>
                        </>
                      )}
                    </>
                  }
                />
              ))}
            </LogTable>
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
      {/* top sources / programs under the lines (the prototype shows the lines first) */}
      {stats.data && stats.data.total > 0 && (
        <Card bodyClassName="px-[22px] py-5">
          <div className="grid grid-cols-1 gap-x-10 gap-y-6 sm:grid-cols-2">
            {deviceId === undefined && (
              <TopList
                title={t('logs.topSources')}
                items={topSourceItems(stats.data.top_sources).slice(0, 5)}
              />
            )}
            <TopList
              title={t('logs.topPrograms')}
              items={stats.data.top_programs.slice(0, 5).map((p) => ({
                key: p.program,
                label: <bdi dir="ltr">{p.program || '—'}</bdi>,
                count: p.count,
              }))}
            />
          </div>
        </Card>
      )}
      <SyslogDetail
        m={selected && withRepeats(selected)}
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
