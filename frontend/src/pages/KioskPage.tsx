import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { isApiError } from '../api/client';
import { endpoints3 } from '../api/endpoints3';
import type { Status, StatusCounts } from '../api/types';
import type { KioskView, KioskWidget } from '../api/types3';
import { SEVERITIES, type SeverityCounts } from '../api/severity';
import { SeverityTag } from '../components/kit';
import { setLanguage } from '../i18n';
import {
  isStale,
  KIOSK_TOKEN_KEY,
  kioskBackoff,
  kioskTokenFromHash,
  kioskWsUrl,
  normalizePositions,
  visibleWidgets,
} from '../lib/kiosk';
import { formatDuration } from '../lib/format';
import { cx } from '../components/ui';
import { Mark } from '../components/Layout';
import { StatusGlyph } from '../components/Status';
import { DARK_TOKENS, statusColor } from '../lib/chartTokens';
import { STATUS_DOT, STATUS_SOFT, statusClass } from '../lib/status';

/** Status word + glyph at wall-screen size (status is never colour alone). */
function WallBadge({ status }: { status: string }) {
  const { t } = useTranslation();
  return (
    <span
      className={cx(
        'inline-flex h-8 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full px-3 text-base font-semibold',
        statusClass(status, STATUS_SOFT),
      )}
    >
      <StatusGlyph status={status} size={15} />
      {t(`status.${status}`)}
    </span>
  );
}

/** The glowing status dot of a wall row (prototype .wall .lst i). */
function GlowDot({ status }: { status: string }) {
  return (
    <i
      aria-hidden="true"
      className={cx(
        'h-3.5 w-3.5 shrink-0 rounded-full shadow-[0_0_14px_currentColor]',
        STATUS_DOT[status as Status] ?? STATUS_DOT.unknown,
        STATUS_TEXT[status],
      )}
    />
  );
}

/** A wall row (prototype .wall .lst div): a 22px-radius card, dot, bold line, the duration at the end. */
const WALL_ROW =
  'grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-4 rounded-[22px] bg-surface px-[22px] py-[18px] text-[20px]';

const STATUS_TEXT: Record<string, string> = {
  up: 'text-status-up',
  warning: 'text-status-warning',
  down: 'text-status-down',
  unreachable: 'text-status-unreachable',
  paused: 'text-status-paused',
  unknown: 'text-status-unknown',
};

/** The wall screen is always dark: its literal colours come from the dark tokens. */
const TK = DARK_TOKENS;

/** Reads the token once: fragment → sessionStorage, then removes it from the address bar. */
export function takeKioskToken(): string | null {
  const fromHash = kioskTokenFromHash(window.location.hash);
  try {
    if (fromHash) sessionStorage.setItem(KIOSK_TOKEN_KEY, fromHash);
  } catch {
    /* ignore */
  }
  if (window.location.hash)
    window.history.replaceState(null, '', window.location.pathname + window.location.search);
  if (fromHash) return fromHash;
  try {
    return sessionStorage.getItem(KIOSK_TOKEN_KEY);
  } catch {
    return null;
  }
}

type KState = {
  view: KioskView | null;
  lastOk: number | null;
  notFound: boolean;
  failing: boolean;
  /** HTTP status of the last failure (0 = network), null while OK. */
  errorStatus: number | null;
};

function useKiosk(token: string | null): KState {
  const [st, setSt] = useState<KState>({
    view: null,
    lastOk: null,
    notFound: false,
    failing: false,
    errorStatus: null,
  });
  const refreshS = st.view?.screen.refresh_s ?? 30;
  const inflight = useRef<AbortController | null>(null);
  const load = useCallback(async () => {
    if (!token) return;
    inflight.current?.abort();
    const ac = new AbortController();
    inflight.current = ac;
    try {
      const view = await endpoints3.kioskView(token, ac.signal);
      setSt({ view, lastOk: Date.now(), notFound: false, failing: false, errorStatus: null });
    } catch (e) {
      if (e instanceof DOMException && e.name === 'AbortError') return;
      const nf = isApiError(e) && e.status === 404;
      setSt((s) => ({
        ...s,
        notFound: nf,
        failing: true,
        errorStatus: isApiError(e) ? e.status : 0,
      }));
    }
  }, [token]);

  // fallback polling every refresh_s (slower while the screen is unavailable)
  useEffect(() => {
    if (!token) return;
    void load();
    const id = setInterval(() => void load(), (st.notFound ? 60 : refreshS) * 1000);
    return () => clearInterval(id);
  }, [token, load, refreshS, st.notFound]);

  // live refresh over /ws/kiosk
  useEffect(() => {
    if (!token) return;
    let ws: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let stopped = false;
    const connect = () => {
      if (stopped) return;
      try {
        ws = new WebSocket(kioskWsUrl(window.location));
      } catch {
        schedule();
        return;
      }
      ws.onopen = () => ws?.send(JSON.stringify({ type: 'kiosk_auth', token }));
      ws.onmessage = (ev) => {
        try {
          const m = JSON.parse(String(ev.data)) as { type?: string };
          if (m.type === 'kiosk.ready') {
            attempt = 0;
          } else if (m.type === 'kiosk.refresh') void load();
        } catch {
          /* ignore */
        }
      };
      ws.onclose = (ev) => {
        ws = null;
        if (ev.code === 4403 || ev.code === 4401) void load();
        schedule(ev.code === 4403 || ev.code === 4401 ? 60_000 : undefined);
      };
    };
    const schedule = (ms?: number) => {
      if (stopped) return;
      timer = setTimeout(connect, ms ?? kioskBackoff(attempt++));
    };
    connect();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      if (ws) {
        ws.onclose = null;
        ws.close(1000);
      }
    };
  }, [token, load]);
  return st;
}

function useNow(ms: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}

function since(iso: string | null, now: number): string {
  if (!iso) return '';
  const s = (now - Date.parse(iso)) / 1000;
  return Number.isFinite(s) && s >= 0 ? formatDuration(s) : '';
}

function Kpi({
  label,
  value,
  status,
  hot,
}: {
  label: string;
  value: number;
  status: Status;
  hot?: boolean;
}) {
  // prototype .wall .nums: a 26px-radius tile, a 72px Inter number in the status colour when it
  // counts something wrong (or the healthy count), the word under it
  return (
    <div className="grid min-w-0 gap-1 rounded-[26px] bg-surface px-[26px] py-[22px]">
      <div
        className={cx(
          'num text-[72px] font-medium leading-none tracking-[-0.04em] tabular-nums',
          hot || (status === 'up' && value > 0) ? STATUS_TEXT[status] : 'text-ink',
        )}
      >
        {value}
      </div>
      <div className="flex items-center gap-2 text-lg text-ink-muted">
        <span className={cx('h-2.5 w-2.5 shrink-0 rounded-full', STATUS_DOT[status])} />
        <span className="truncate">{label}</span>
      </div>
    </div>
  );
}

function Panel({
  title,
  children,
  className,
}: {
  title: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={cx('min-w-0', className)}>
      <h2 className="mx-1.5 mb-3 text-[22px] font-semibold text-ink-muted">{title}</h2>
      {children}
    </section>
  );
}

/** [severity] Alerts per severity at wall size (only when the payload has the counts). */
function SeverityKpis({ counts }: { counts: SeverityCounts }) {
  const { t } = useTranslation();
  return (
    <ul
      aria-label={t('severity.counts')}
      data-testid="kiosk-severity"
      className="m-0 mt-4 grid list-none grid-cols-2 gap-4 p-0 md:grid-cols-5"
    >
      {SEVERITIES.map((s) => (
        <li
          key={s}
          data-severity={s}
          className={cx(
            'flex items-center justify-between gap-3 rounded-[22px] bg-surface px-[22px] py-4',
            !counts[s] && 'opacity-50',
          )}
        >
          <SeverityTag severity={s} size="md" className="!h-8 !text-base" />
          <span className="num text-[44px] font-medium leading-none tracking-[-0.03em] tabular-nums">
            {counts[s]}
          </span>
        </li>
      ))}
    </ul>
  );
}

function Summary({ s }: { s: NonNullable<KioskView['summary']> }) {
  const { t } = useTranslation();
  const c: StatusCounts = s.device_counts;
  return (
    <>
      <div
        className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-6"
        data-testid="kiosk-kpis"
      >
        <Kpi label={t('noc.devicesUp')} value={c.up} status="up" />
        <Kpi
          label={t('noc.devicesDown')}
          value={c.down + c.unreachable}
          status="down"
          hot={c.down + c.unreachable > 0}
        />
        <Kpi
          label={t('noc.devicesWarning')}
          value={c.warning}
          status="warning"
          hot={c.warning > 0}
        />
        <Kpi
          label={t('noc.sensorsDown')}
          value={s.sensor_counts.down}
          status="down"
          hot={s.sensor_counts.down > 0}
        />
        <Kpi label={t('noc.acknowledged')} value={s.acknowledged} status="up" />
        <Kpi label={t('noc.maintenance')} value={s.in_maintenance} status="paused" />
      </div>
      {s.severity_counts && <SeverityKpis counts={s.severity_counts} />}
    </>
  );
}

function MapPanel({ map }: { map: NonNullable<KioskView['map']> }) {
  const pos = useMemo(() => normalizePositions(map.nodes), [map.nodes]);
  const W = 1000;
  const H = 560;
  const pad = 40;
  const at = (id: number) => {
    const p = pos.get(id) ?? { x: 0.5, y: 0.5 };
    return { x: pad + p.x * (W - 2 * pad), y: pad + p.y * (H - 2 * pad) };
  };
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" data-testid="kiosk-map">
      {map.links.map((l, i) => {
        const a = at(l.a);
        const b = at(l.b);
        return (
          <line
            key={i}
            x1={a.x}
            y1={a.y}
            x2={b.x}
            y2={b.y}
            stroke={
              l.status === 'down'
                ? TK.status.down
                : l.status === 'up'
                  ? TK.status.up
                  : TK.borderStrong
            }
            strokeWidth={l.status === 'down' ? 4 : 2}
            strokeDasharray={
              l.status === 'down' ? '10 6' : l.status === 'unknown' ? '6 6' : undefined
            }
          />
        );
      })}
      {map.nodes.map((n) => {
        const p = at(n.id);
        return (
          <g key={n.id}>
            <rect
              x={p.x - 13}
              y={p.y - 13}
              width={26}
              height={26}
              rx={3}
              fill={TK.surface}
              stroke={statusColor(TK, n.status)}
              strokeWidth={3}
            />
            <circle cx={p.x} cy={p.y} r={5} fill={statusColor(TK, n.status)} />
            <text
              x={p.x}
              y={p.y + 34}
              textAnchor="middle"
              fontSize={16}
              fontWeight={600}
              fontFamily={TK.fontMono}
              fill={TK.ink}
            >
              {n.name}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function Widget({ w, v, now }: { w: KioskWidget; v: KioskView; now: number }) {
  const { t } = useTranslation();
  // [severity] the payload carries severities only while the feature is on
  const sevOn = !!v.summary?.severity_counts || (v.problems ?? []).some((p) => !!p.severity);
  switch (w) {
    case 'summary':
      return v.summary ? <Summary s={v.summary} /> : null;
    case 'down_devices':
      return (
        <Panel title={t('noc.downDevices', { n: v.down_devices?.length ?? 0 })}>
          {!v.down_devices?.length ? (
            <p className={cx(WALL_ROW, 'block text-status-up')}>{t('noc.allUp')}</p>
          ) : (
            <ul className="m-0 grid list-none gap-3 p-0">
              {v.down_devices.map((d) => (
                <li key={d.id} className={WALL_ROW}>
                  <GlowDot status={d.status} />
                  <span className="flex min-w-0 flex-wrap items-baseline gap-x-3">
                    <b className="font-display font-semibold">{d.name}</b>
                    {d.host && (
                      <bdi dir="ltr" className="font-display text-lg text-ink-muted">
                        {d.host}
                      </bdi>
                    )}
                    <span className="text-lg text-ink-muted">{d.group}</span>
                    <span className="sr-only">{t(`status.${d.status}`)}</span>
                  </span>
                  <span className="flex items-center gap-3">
                    {d.acknowledged && <span className="text-lg text-ack">✓ {t('noc.ack')}</span>}
                    <span className="num text-lg font-medium tabular-nums text-ink-muted">
                      {since(d.since, now)}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      );
    case 'problems':
      return (
        <Panel title={t('noc.problems', { n: v.problems?.length ?? 0 })}>
          {!v.problems?.length ? (
            <p className={cx(WALL_ROW, 'block text-status-up')}>{t('noc.noProblems')}</p>
          ) : (
            <ul className="m-0 grid list-none gap-3 p-0">
              {v.problems.map((p) => (
                <li key={p.sensor_id} className={cx(WALL_ROW, p.acknowledged && 'opacity-60')}>
                  <GlowDot status={p.status} />
                  <span className="flex min-w-0 items-baseline gap-x-3">
                    <b className="shrink-0 font-display font-semibold">{p.device_name}</b>
                    <span className="shrink-0 font-semibold">· {p.sensor_name}</span>
                    <bdi dir="auto" className="min-w-0 truncate text-lg text-ink-muted">
                      {p.message}
                    </bdi>
                    <span className="sr-only">{t(`status.${p.status}`)}</span>
                  </span>
                  <span className="flex items-center gap-3">
                    {sevOn && (
                      <SeverityTag severity={p.severity} size="md" className="!h-8 !text-base" />
                    )}
                    <span className="num text-lg font-medium tabular-nums text-ink-muted">
                      {since(p.since, now)}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      );
    case 'recent_alerts':
      return (
        <Panel title={t('noc.recentAlerts')}>
          <ul className="m-0 grid list-none gap-0 overflow-hidden rounded-[22px] bg-surface p-0 [&>li+li]:border-t [&>li+li]:border-line">
            {(v.recent_alerts ?? []).map((a, i) => (
              <li key={i} className="flex flex-wrap items-center gap-x-3 px-[22px] py-3">
                <span className="num tabular-nums text-ink-muted">
                  {new Intl.DateTimeFormat(undefined, {
                    timeStyle: 'short',
                    timeZone: v.timezone,
                  }).format(new Date(a.time))}
                </span>
                <span className="font-display font-medium">{a.device_name}</span>
                <span className="text-ink-muted">{a.sensor_name}</span>
                <span className="flex-1" />
                {sevOn && (
                  <SeverityTag severity={a.severity} size="md" className="!h-8 !text-base" />
                )}
                {a.new_status && <WallBadge status={a.new_status} />}
              </li>
            ))}
          </ul>
        </Panel>
      );
    case 'group_tiles':
      return (
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4 xl:grid-cols-6">
          {(v.group_tiles ?? []).map((g) => (
            <div key={g.id} className="rounded-[22px] bg-surface p-5">
              <div className="flex items-center gap-2">
                <span className="truncate text-xl font-semibold">{g.name}</span>
              </div>
              <div className="mt-2">
                <WallBadge status={g.status} />
              </div>
              <div className="num mt-3 flex gap-4 text-[28px] font-medium leading-none tabular-nums">
                <span className="text-status-up" title={t('status.up')}>
                  {g.counts.up}
                </span>
                <span className="text-status-warning" title={t('status.warning')}>
                  {g.counts.warning}
                </span>
                <span className="text-status-down" title={t('status.down')}>
                  {g.counts.down + g.counts.unreachable}
                </span>
              </div>
            </div>
          ))}
        </div>
      );
    case 'map':
      return v.map ? (
        <Panel title={t('noc.map')}>
          <div className="rounded-[22px] bg-surface p-5">
            <MapPanel map={v.map} />
          </div>
        </Panel>
      ) : null;
  }
}

/** [3] NOC wall screen (/noc): no session; token from the URL fragment; dark, large, auto refresh. */
export function KioskPage() {
  const { t } = useTranslation();
  const [token] = useState(takeKioskToken);
  const st = useKiosk(token);
  const now = useNow(1000);
  const v = st.view;
  const lang = v?.screen.language;
  useEffect(() => {
    if (lang) setLanguage(lang, false);
  }, [lang]);
  useEffect(() => {
    document.title = v ? `NOC · ${v.screen.name}` : 'NOC';
  }, [v]);
  const stale = v ? isStale(st.lastOk, v.screen.refresh_s, now) : false;
  const toggleFs = () => {
    if (document.fullscreenElement) void document.exitFullscreen?.();
    else void document.documentElement.requestFullscreen?.();
  };
  const widgets = v ? visibleWidgets(v) : [];
  // prototype: the incident list runs the full width; the device and alert lists share a row
  const half = (w: KioskWidget) => w === 'recent_alerts' || w === 'down_devices';
  // a half-width widget shares its row only with a neighbouring half-width one (the screen's own
  // order decides the pairs); alone it takes the full width
  const pairs = new Set<number>();
  for (let i = 0; i < widgets.length - 1; i++)
    if (!pairs.has(i) && half(widgets[i]) && half(widgets[i + 1])) {
      pairs.add(i);
      pairs.add(i + 1);
    }
  const paired = (i: number) => pairs.has(i);
  return (
    <div
      // Always dark (wall screens): the dark tokens are scoped with data-theme.
      data-theme="dark"
      className="min-h-screen bg-black px-11 py-[34px] text-[20px] text-ink max-sm:p-5"
      data-testid="kiosk"
      onDoubleClick={toggleFs}
    >
      <button
        type="button"
        onClick={toggleFs}
        className="fixed end-0 top-0 h-8 w-8 opacity-0 focus:opacity-100"
        aria-label={t('noc.fullscreen')}
      />
      <header className="mb-[26px] flex flex-wrap items-center gap-4">
        <Mark size={32} />
        <h1 className="m-0 text-[34px] font-semibold">{v?.screen.name ?? 'NetworkMonitor'}</h1>
        <span className="flex-1" />
        {st.lastOk && (
          <span className="text-base text-ink-subtle">
            {t('noc.updated', {
              time: new Intl.DateTimeFormat(undefined, {
                timeStyle: 'medium',
                timeZone: v?.timezone,
              }).format(new Date(st.lastOk)),
            })}
          </span>
        )}
        <span
          dir="ltr"
          className="num text-[64px] font-light leading-none tracking-[-0.03em] tabular-nums max-sm:text-[40px]"
        >
          {new Intl.DateTimeFormat(undefined, {
            timeStyle: 'medium',
            timeZone: v?.timezone,
          }).format(new Date(now))}
        </span>
      </header>
      {!token && <p className="mt-24 text-center text-3xl text-ink-subtle">{t('noc.noToken')}</p>}
      {token && st.notFound && (
        <p className="mt-24 text-center text-3xl text-ink-subtle" data-testid="kiosk-unavailable">
          {t('noc.unavailable')}
          <span className="mt-4 block text-xl text-ink-muted">{t('noc.unavailableHint')}</span>
        </p>
      )}
      {token && !v && st.failing && !st.notFound && (
        <p className="mt-24 text-center text-3xl text-ink-subtle" data-testid="kiosk-error">
          {st.errorStatus === 429 ? t('noc.rateLimited') : t('noc.loadError')}
        </p>
      )}
      {stale && !st.notFound && (
        <div
          role="alert"
          className="mb-5 rounded-[22px] bg-status-warning-soft px-[22px] py-4 text-xl text-ink"
        >
          {t('noc.stale')}
        </div>
      )}
      {v && !st.notFound && (
        <div className="grid grid-cols-1 gap-[26px] xl:grid-cols-2">
          {widgets.map((w, i) => (
            <div key={w} className={paired(i) ? '' : 'xl:col-span-2'}>
              <Widget w={w} v={v} now={now} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
