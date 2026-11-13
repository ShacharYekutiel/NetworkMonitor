import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { isApiError } from '../api/client';
import { endpoints3 } from '../api/endpoints3';
import type { Status, StatusCounts } from '../api/types';
import type { KioskView, KioskWidget } from '../api/types3';
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
        'inline-flex h-8 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-sm px-2.5 text-lg font-semibold',
        statusClass(status, STATUS_SOFT),
      )}
    >
      <StatusGlyph status={status} size={16} />
      {t(`status.${status}`)}
    </span>
  );
}

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
  return (
    <div className={cx('min-w-0 px-5 pb-4 pt-4', hot ? SOFT_BG[status] : 'bg-surface')}>
      <div className="flex items-center gap-2.5 text-lg font-medium text-ink-muted">
        <span className={cx('h-3 w-3 shrink-0 rounded-full', STATUS_DOT[status])} />
        <span className="truncate">{label}</span>
      </div>
      <div
        className={cx(
          'mt-2 font-display text-[64px] font-semibold leading-none tabular-nums',
          hot ? STATUS_TEXT[status] : value ? 'text-ink' : 'text-ink-subtle',
        )}
      >
        {value}
      </div>
    </div>
  );
}

const SOFT_BG: Record<Status, string> = {
  up: 'bg-status-up-soft',
  warning: 'bg-status-warning-soft',
  down: 'bg-status-down-soft',
  unreachable: 'bg-status-unreachable-soft',
  unknown: 'bg-status-unknown-soft',
  paused: 'bg-status-paused-soft',
};

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
    <section className={cx('min-w-0 rounded-lg border border-line bg-surface', className)}>
      <h2 className="flex min-h-[56px] items-center border-b border-line px-5 text-xl font-semibold">
        {title}
      </h2>
      <div className="p-5">{children}</div>
    </section>
  );
}

function Summary({ s }: { s: NonNullable<KioskView['summary']> }) {
  const { t } = useTranslation();
  const c: StatusCounts = s.device_counts;
  return (
    <div
      className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-line bg-line md:grid-cols-3 xl:grid-cols-6"
      data-testid="kiosk-kpis"
    >
      <Kpi label={t('noc.devicesUp')} value={c.up} status="up" />
      <Kpi
        label={t('noc.devicesDown')}
        value={c.down + c.unreachable}
        status="down"
        hot={c.down + c.unreachable > 0}
      />
      <Kpi label={t('noc.devicesWarning')} value={c.warning} status="warning" hot={c.warning > 0} />
      <Kpi
        label={t('noc.sensorsDown')}
        value={s.sensor_counts.down}
        status="down"
        hot={s.sensor_counts.down > 0}
      />
      <Kpi label={t('noc.acknowledged')} value={s.acknowledged} status="up" />
      <Kpi label={t('noc.maintenance')} value={s.in_maintenance} status="paused" />
    </div>
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
  switch (w) {
    case 'summary':
      return v.summary ? <Summary s={v.summary} /> : null;
    case 'down_devices':
      return (
        <Panel title={t('noc.downDevices', { n: v.down_devices?.length ?? 0 })}>
          {!v.down_devices?.length ? (
            <p className="text-2xl text-status-up">{t('noc.allUp')}</p>
          ) : (
            <ul className="space-y-2">
              {v.down_devices.map((d) => (
                <li key={d.id} className="flex flex-wrap items-center gap-x-3">
                  <WallBadge status={d.status} />
                  <span className="text-2xl font-semibold">{d.name}</span>
                  {d.host && (
                    <bdi dir="ltr" className="font-mono text-lg text-ink-subtle">
                      {d.host}
                    </bdi>
                  )}
                  <span className="text-ink-subtle">{d.group}</span>
                  <span className={cx('ms-auto font-mono tabular-nums', STATUS_TEXT[d.status])}>
                    {since(d.since, now)}
                  </span>
                  {d.acknowledged && <span className="text-ack">✓ {t('noc.ack')}</span>}
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
            <p className="text-2xl text-status-up">{t('noc.noProblems')}</p>
          ) : (
            <ul className="space-y-1.5">
              {v.problems.map((p) => (
                <li
                  key={p.sensor_id}
                  className={cx(
                    'flex flex-wrap items-center gap-x-3',
                    p.acknowledged && 'opacity-60',
                  )}
                >
                  <WallBadge status={p.status} />
                  <span className="font-semibold">{p.device_name}</span>
                  <span className="text-ink-subtle">{p.sensor_name}</span>
                  <bdi dir="auto" className="min-w-0 truncate text-ink-subtle">
                    {p.message}
                  </bdi>
                  <span className="ms-auto font-mono tabular-nums text-ink-subtle">
                    {since(p.since, now)}
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
          <ul className="space-y-1">
            {(v.recent_alerts ?? []).map((a, i) => (
              <li key={i} className="flex flex-wrap items-baseline gap-x-3">
                <span className="font-mono tabular-nums text-ink-subtle">
                  {new Intl.DateTimeFormat(undefined, {
                    timeStyle: 'short',
                    timeZone: v.timezone,
                  }).format(new Date(a.time))}
                </span>
                <span className="font-medium">{a.device_name}</span>
                <span className="text-ink-subtle">{a.sensor_name}</span>
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
            <div key={g.id} className="rounded-lg border border-line bg-surface p-4">
              <div className="flex items-center gap-2">
                <span className="truncate text-xl font-semibold">{g.name}</span>
              </div>
              <div className="mt-2">
                <WallBadge status={g.status} />
              </div>
              <div className="mt-3 flex gap-4 font-display text-[28px] font-semibold leading-none tabular-nums">
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
          <MapPanel map={v.map} />
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
  const half = (w: KioskWidget) =>
    w === 'problems' || w === 'recent_alerts' || w === 'down_devices';
  return (
    <div
      // Always dark (wall screens): the dark tokens are scoped with data-theme.
      data-theme="dark"
      className="min-h-screen bg-bg p-6 text-[20px] text-ink"
      data-testid="kiosk"
      onDoubleClick={toggleFs}
    >
      <button
        type="button"
        onClick={toggleFs}
        className="fixed end-0 top-0 h-8 w-8 opacity-0 focus:opacity-100"
        aria-label={t('noc.fullscreen')}
      />
      <header className="mb-5 flex flex-wrap items-baseline gap-4">
        <span className="self-center">
          <Mark size={32} />
        </span>
        <h1 className="text-3xl font-semibold">{v?.screen.name ?? 'NetworkMonitor'}</h1>
        <span className="ms-auto font-display text-5xl font-semibold tabular-nums">
          {new Intl.DateTimeFormat(undefined, {
            timeStyle: 'medium',
            timeZone: v?.timezone,
          }).format(new Date(now))}
        </span>
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
          className="mb-4 rounded-lg border-s-4 border-status-warning bg-status-warning-soft px-4 py-3 text-xl text-ink"
        >
          {t('noc.stale')}
        </div>
      )}
      {v && !st.notFound && (
        <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
          {widgets.map((w) => (
            <div key={w} className={half(w) ? '' : 'xl:col-span-2'}>
              <Widget w={w} v={v} now={now} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
