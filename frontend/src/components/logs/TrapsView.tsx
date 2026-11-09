import { useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints } from '../../api/endpoints';
import type { Trap } from '../../api/types';
import { useAuth } from '../../hooks/auth';
import { useLocalState } from '../../hooks/useLocalState';
import { ruleFromTrap, type RuleForm } from '../../lib/eventRules';
import { formatDateTime, formatDuration } from '../../lib/format';
import {
  emptyFilter,
  liveAllowed,
  matchesTrap,
  rangeOf,
  ticksToSeconds,
  trapQuery,
  withCursor,
  type LogFilter,
} from '../../lib/logs';
import { DetailRows, Drawer } from '../Drawer';
import { RefreshIcon } from '../icons';
import { Button, Card, cx, Empty, ErrorBox, Loading } from '../ui';
import { SourceCell, TopList, topSourceItems, useGroupDevices } from './common';
import { LogFilterBar } from './FilterBar';
import { Histogram } from './Histogram';
import { RuleEditorModal } from './RuleEditor';
import { useRuleName } from './rules';
import { LiveToggle, NewRecordsBar } from './SyslogView';
import { useLogFeed } from './useLogFeed';

const th =
  'sticky top-0 z-10 whitespace-nowrap border-b border-slate-200 bg-white px-2 py-1.5 text-start text-[11px] font-medium uppercase tracking-wide text-slate-500 dark:border-slate-800 dark:bg-slate-900';
const td = 'border-b border-slate-100 px-2 py-1 align-top dark:border-slate-800/70';

export function TrapDetail({
  trap,
  onClose,
  onCreateRule,
}: {
  trap: Trap | null;
  onClose: () => void;
  onCreateRule?: (t: Trap) => void;
}) {
  const { t } = useTranslation();
  const ruleName = useRuleName();
  const up = trap ? ticksToSeconds(trap.uptime_ticks) : null;
  return (
    <Drawer
      open={!!trap}
      onClose={onClose}
      testId="trap-detail"
      title={trap ? <bdi dir="ltr">{trap.trap_name ?? trap.trap_oid}</bdi> : t('traps.detail')}
      footer={
        trap &&
        onCreateRule && (
          <Button variant="primary" onClick={() => onCreateRule(trap)}>
            {t('traps.createRule')}
          </Button>
        )
      }
    >
      {trap && (
        <div className="space-y-4">
          <p dir="ltr" className="text-left font-mono text-xs">
            {trap.summary}
          </p>
          <DetailRows
            rows={[
              [t('logs.col.time'), formatDateTime(trap.time)],
              [t('logs.col.source'), <SourceCell r={trap} />],
              [t('logs.sourceIp'), <bdi dir="ltr">{trap.source_ip}</bdi>],
              [t('traps.agentAddr'), <bdi dir="ltr">{trap.agent_addr ?? '—'}</bdi>],
              [t('traps.version'), `${trap.version} · ${trap.pdu}`],
              [t('traps.securityName'), <bdi dir="ltr">{trap.security_name ?? '—'}</bdi>],
              [
                t('traps.trapOid'),
                <bdi dir="ltr" className="font-mono text-xs">
                  {trap.trap_oid}
                </bdi>,
              ],
              [t('traps.uptime'), up === null ? '—' : formatDuration(up)],
              [
                t('logs.matchedRules'),
                trap.matched_rule_ids.length ? trap.matched_rule_ids.map(ruleName).join(', ') : '—',
              ],
            ]}
          />
          <div>
            <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
              {t('traps.varbinds')}
            </h4>
            <div className="overflow-x-auto">
              <table className="w-full text-xs" data-testid="varbinds">
                <thead>
                  <tr>
                    <th className={th}>{t('traps.vb.name')}</th>
                    <th className={th}>{t('traps.vb.oid')}</th>
                    <th className={th}>{t('traps.vb.type')}</th>
                    <th className={th}>{t('traps.vb.value')}</th>
                  </tr>
                </thead>
                <tbody>
                  {trap.varbinds.map((v, i) => (
                    <tr key={i}>
                      <td className={td}>
                        <bdi dir="ltr">{v.name ?? '—'}</bdi>
                      </td>
                      <td className={cx(td, 'font-mono')}>
                        <bdi dir="ltr">{v.oid}</bdi>
                      </td>
                      <td className={td}>{v.type}</td>
                      <td className={cx(td, 'break-all font-mono')} title={v.value}>
                        <bdi dir="ltr">{v.display}</bdi>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </Drawer>
  );
}

/** [2b-3] Traps: same pattern as syslog (columns time, device/source, version, trap name, summary). */
export function TrapsView({ deviceId }: { deviceId?: number }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const [filter, setFilter] = useState<LogFilter>(() =>
    emptyFilter({ deviceId: deviceId ?? null }),
  );
  const [anchor, setAnchor] = useState(() => Date.now());
  const [live, setLive] = useLocalState('netmon.logs.live', false);
  const [selected, setSelected] = useState<Trap | null>(null);
  const [ruleDraft, setRuleDraft] = useState<RuleForm | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const update = (p: Partial<LogFilter>) => {
    setFilter((f) => ({ ...f, ...p }));
    setAnchor(Date.now());
  };
  const range = rangeOf(filter, anchor);
  const query = range.ok ? trapQuery(filter, range) : null;
  const groupDevices = useGroupDevices(filter.groupId);
  const canLive = liveAllowed(filter);
  const feed = useLogFeed<Trap>({
    queryKey: ['traps', query],
    fetchPage: (c) => endpoints.traps(withCursor(query!, c)),
    enabled: !!query,
    live: live && canLive,
    topic: 'trap',
    matches: (x) => matchesTrap(x, filter, groupDevices),
  });
  const stats = useQuery({
    queryKey: ['trap-stats', query],
    queryFn: () => endpoints.trapStats(query!),
    enabled: !!query,
    refetchInterval: live && canLive ? 60_000 : false,
  });

  return (
    <div className="space-y-3" data-testid="traps-view">
      <Card bodyClassName="p-3">
        <LogFilterBar
          kind="trap"
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
          <div className="text-xs text-slate-500">{t('logs.total', { n: stats.data.total })}</div>
          <Histogram
            times={stats.data.buckets.map((b) => b.t)}
            series={[
              {
                name: t('traps.title'),
                color: '#f97316',
                data: stats.data.buckets.map((b) => b.count),
              },
            ]}
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
              title={t('traps.topTraps')}
              items={stats.data.top_traps.map((x) => ({
                key: x.trap_oid,
                label: <bdi dir="ltr">{x.trap_name ?? x.trap_oid}</bdi>,
                count: x.count,
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
        >
          <NewRecordsBar
            count={feed.pendingCount}
            onShow={() => {
              scrollRef.current?.scrollTo({ top: 0 });
              feed.showPending();
            }}
          />
          {!feed.isLoading && !feed.items.length ? (
            <Empty>{t('traps.empty')}</Empty>
          ) : (
            <table className="w-full text-xs">
              <thead>
                <tr>
                  <th className={th}>{t('logs.col.time')}</th>
                  {deviceId === undefined && <th className={th}>{t('logs.col.source')}</th>}
                  <th className={th}>{t('traps.version')}</th>
                  <th className={th}>{t('traps.name')}</th>
                  <th className={cx(th, 'w-full')}>{t('traps.summary')}</th>
                </tr>
              </thead>
              <tbody>
                {feed.items.map((x) => (
                  <tr
                    key={x.id}
                    onClick={() => setSelected(x)}
                    className="cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-800/50"
                  >
                    <td className={cx(td, 'whitespace-nowrap text-slate-500')}>
                      {formatDateTime(x.time)}
                    </td>
                    {deviceId === undefined && (
                      <td className={cx(td, 'whitespace-nowrap')}>
                        <SourceCell r={x} />
                      </td>
                    )}
                    <td className={cx(td, 'whitespace-nowrap')}>
                      {x.version}
                      {x.pdu === 'inform' && (
                        <span className="ms-1 text-slate-500">({t('traps.inform')})</span>
                      )}
                    </td>
                    <td className={cx(td, 'whitespace-nowrap')}>
                      <bdi dir="ltr" title={x.trap_oid}>
                        {x.trap_name ?? x.trap_oid}
                      </bdi>
                    </td>
                    <td className={td}>
                      <div dir="ltr" className="line-clamp-2 break-all text-left font-mono">
                        {x.summary}
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
      <TrapDetail
        trap={selected}
        onClose={() => setSelected(null)}
        onCreateRule={
          isAdmin
            ? (x) => {
                setSelected(null);
                setRuleDraft(ruleFromTrap(x));
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
