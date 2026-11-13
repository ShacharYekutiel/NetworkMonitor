import { useState } from 'react';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { endpoints } from '../../api/endpoints';
import type { TimelineItem, TimelineKind } from '../../api/types';
import { useAuth } from '../../hooks/auth';
import { useFeatures } from '../../hooks/features';
import { formatDateTime } from '../../lib/format';
import { rangeOf, type LogFilter } from '../../lib/logs';
import { availableKinds, groupByDay, TIMELINE_TONE, timelineHref } from '../../lib/timeline';
import { StatusBadge } from '../Status';
import { Button, Card, cx, Empty, ErrorBox, Loading } from '../ui';
import { SeverityBadge, TimeRangeSelect } from '../logs/common';
import { SyslogDetail } from '../logs/SyslogView';
import { TrapDetail } from '../logs/TrapsView';
import { useLocalizeMessage } from '../../hooks/localize';

function ItemText({ it }: { it: TimelineItem }) {
  const { t } = useTranslation();
  const localize = useLocalizeMessage();
  switch (it.kind) {
    case 'status':
    case 'port':
      return (
        <span className="inline-flex flex-wrap items-center gap-1.5">
          {it.sensor_name && <span className="font-medium">{it.sensor_name}</span>}
          {it.old_status && <StatusBadge status={it.old_status} />}
          {it.old_status && <span className="text-ink-subtle rtl:rotate-180">→</span>}
          {it.status && <StatusBadge status={it.status} />}
          <bdi dir="auto" className="text-ink-muted">
            {localize(it.message)}
          </bdi>
        </span>
      );
    case 'syslog':
      return (
        <span className="inline-flex min-w-0 items-center gap-1.5">
          {it.severity !== null && <SeverityBadge severity={it.severity} />}
          <bdi dir="ltr" className="truncate font-mono text-xs">
            {it.message}
          </bdi>
        </span>
      );
    case 'trap':
      return (
        <bdi dir="ltr" className="font-mono text-xs">
          {it.message}
        </bdi>
      );
    case 'audit':
      return (
        <span>
          <bdi dir="ltr" className="font-mono text-xs">
            {it.message}
          </bdi>
          {it.actor && (
            <span className="ms-2 text-xs text-ink-muted">
              {t('timeline.by', { actor: it.actor })}
            </span>
          )}
        </span>
      );
    default:
      return (
        <span>
          {it.sensor_name && <span className="me-1 font-medium">{it.sensor_name}:</span>}
          <bdi dir="auto">{localize(it.message)}</bdi>
          {it.actor && (
            <span className="ms-2 text-xs text-ink-muted">
              {t('timeline.by', { actor: it.actor })}
            </span>
          )}
        </span>
      );
  }
}

/** [2b-3] Device timeline: kind chips, time range, newest first, "Load more". */
export function TimelineTab({ deviceId }: { deviceId: number }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const { features } = useFeatures();
  const kindsAvail = availableKinds({ ...features, isAdmin });
  const [off, setOff] = useState<TimelineKind[]>([]);
  const [range, setRange] = useState<Pick<LogFilter, 'preset' | 'from' | 'to'>>({
    preset: '24h',
    from: '',
    to: '',
  });
  const [anchor, setAnchor] = useState(() => Date.now());
  const [trapId, setTrapId] = useState<number | null>(null);
  const [syslogId, setSyslogId] = useState<number | null>(null);
  const r = rangeOf(range, anchor);
  const active = kindsAvail.filter((k) => !off.includes(k));
  const q = useInfiniteQuery({
    queryKey: ['timeline', deviceId, active, r],
    queryFn: ({ pageParam }) =>
      endpoints.timeline(deviceId, {
        kinds: active,
        since: r.ok ? r.since : undefined,
        until: r.ok ? r.until : undefined,
        cursor: pageParam ?? undefined,
        limit: 50,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: r.ok && active.length > 0,
  });
  const trap = useQuery({
    queryKey: ['trap', trapId],
    queryFn: () => endpoints.trap(trapId!),
    enabled: trapId !== null,
  });
  const msg = useQuery({
    queryKey: ['syslog-msg', syslogId],
    queryFn: () => endpoints.syslogMessage(syslogId!),
    enabled: syslogId !== null,
  });
  const items = q.data?.pages.flatMap((p) => p.items) ?? [];
  const toggle = (k: TimelineKind) =>
    setOff((ks) => (ks.includes(k) ? ks.filter((x) => x !== k) : [...ks, k]));

  return (
    <div className="space-y-3" data-testid="timeline-tab">
      <Card bodyClassName="flex flex-wrap items-center gap-2 p-3">
        <TimeRangeSelect
          filter={{ ...range } as LogFilter}
          onChange={(p) => {
            setRange((x) => ({ ...x, ...p }));
            setAnchor(Date.now());
          }}
        />
        <div className="flex flex-wrap gap-1" role="group" aria-label={t('timeline.kinds')}>
          {kindsAvail.map((k) => (
            <button
              key={k}
              type="button"
              aria-pressed={!off.includes(k)}
              onClick={() => toggle(k)}
              className={cx(
                'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs ring-1 ring-inset',
                !off.includes(k)
                  ? 'bg-ink text-surface ring-ink'
                  : 'text-ink-muted ring-line-strong',
              )}
            >
              <span className={cx('h-2 w-2 rounded-full', TIMELINE_TONE[k])} />
              {t(`timeline.kind.${k}`)}
            </button>
          ))}
        </div>
        {!r.ok && <ErrorBox error={new Error(t(`logs.rangeErr.${r.error}`))} className="w-full" />}
      </Card>
      <Card bodyClassName="p-0">
        {q.isLoading && <Loading />}
        <ErrorBox error={q.error} className="m-3" />
        {!q.isLoading && !items.length && <Empty>{t('timeline.empty')}</Empty>}
        {groupByDay(items).map((g) => (
          <div key={g.day}>
            <div className="sticky top-0 bg-surface-sunken px-3 py-1 text-xs font-semibold text-ink-muted">
              <bdi dir="ltr">{g.day}</bdi>
            </div>
            <ol className="relative">
              {g.items.map((it) => {
                const href = timelineHref(it, deviceId);
                const onClick =
                  it.ref.type === 'trap'
                    ? () => setTrapId(it.ref.id)
                    : it.ref.type === 'syslog'
                      ? () => setSyslogId(it.ref.id)
                      : undefined;
                const body = (
                  <>
                    <span
                      className={cx(
                        'mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full',
                        TIMELINE_TONE[it.kind] ?? 'bg-slate-500',
                      )}
                      aria-hidden="true"
                    />
                    <span
                      className="w-20 shrink-0 text-xs tabular-nums text-ink-muted"
                      title={formatDateTime(it.time)}
                    >
                      {new Date(it.time).toLocaleTimeString()}
                    </span>
                    <span className="w-24 shrink-0 text-xs text-ink-muted">
                      {t(`timeline.kind.${it.kind}`, { defaultValue: it.kind })}
                    </span>
                    <span className="min-w-0 flex-1 text-sm">
                      <ItemText it={it} />
                    </span>
                  </>
                );
                const cls = 'flex items-start gap-3 border-b border-line px-3 py-1.5';
                return (
                  <li key={`${it.kind}-${it.id}`}>
                    {href && (isAdmin || (it.kind !== 'audit' && it.kind !== 'config')) ? (
                      <Link to={href} className={cx(cls, 'hover:bg-surface-hover')}>
                        {body}
                      </Link>
                    ) : onClick ? (
                      <button
                        type="button"
                        onClick={onClick}
                        className={cx(cls, 'w-full text-start hover:bg-surface-hover')}
                      >
                        {body}
                      </button>
                    ) : (
                      <div className={cls}>{body}</div>
                    )}
                  </li>
                );
              })}
            </ol>
          </div>
        ))}
        {q.hasNextPage && (
          <div className="flex justify-center p-2">
            <Button size="sm" busy={q.isFetchingNextPage} onClick={() => void q.fetchNextPage()}>
              {t('logs.loadMore')}
            </Button>
          </div>
        )}
      </Card>
      <TrapDetail
        trap={trapId !== null ? (trap.data ?? null) : null}
        onClose={() => setTrapId(null)}
      />
      <SyslogDetail
        m={syslogId !== null ? (msg.data ?? null) : null}
        onClose={() => setSyslogId(null)}
      />
    </div>
  );
}
