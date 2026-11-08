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

const STATUS_BG: Record<string, string> = {
  up: 'bg-green-500',
  warning: 'bg-amber-400',
  down: 'bg-red-500',
  unreachable: 'bg-red-800',
  paused: 'bg-slate-500',
  unknown: 'bg-slate-400',
};
const STATUS_TEXT: Record<string, string> = {
  up: 'text-green-400',
  warning: 'text-amber-300',
  down: 'text-red-400',
  unreachable: 'text-red-300',
  paused: 'text-slate-400',
  unknown: 'text-slate-400',
};
const STATUS_HEX: Record<string, string> = {
  up: '#22c55e',
  warning: '#fbbf24',
  down: '#ef4444',
  unreachable: '#991b1b',
  paused: '#64748b',
  unknown: '#94a3b8',
};

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
};

function useKiosk(token: string | null): KState {
  const [st, setSt] = useState<KState>({
    view: null,
    lastOk: null,
    notFound: false,
    failing: false,
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
      setSt({ view, lastOk: Date.now(), notFound: false, failing: false });
    } catch (e) {
      if (e instanceof DOMException && e.name === 'AbortError') return;
      const nf = isApiError(e) && e.status === 404;
      setSt((s) => ({ ...s, notFound: nf, failing: true }));
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

function Kpi({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <div className="rounded-xl bg-slate-900 px-5 py-4 ring-1 ring-slate-800">
      <div className={cx('text-[64px] font-bold leading-none tabular-nums', tone)}>{value}</div>
      <div className="mt-2 text-lg text-slate-400">{label}</div>
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
    <section className={cx('min-w-0 rounded-xl bg-slate-900 p-4 ring-1 ring-slate-800', className)}>
      <h2 className="mb-3 text-xl font-semibold text-slate-300">{title}</h2>
      {children}
    </section>
  );
}

function Summary({ s }: { s: NonNullable<KioskView['summary']> }) {
  const { t } = useTranslation();
  const c: StatusCounts = s.device_counts;
  return (
    <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-6" data-testid="kiosk-kpis">
      <Kpi label={t('noc.devicesUp')} value={c.up} tone="text-green-400" />
      <Kpi
        label={t('noc.devicesDown')}
        value={c.down + c.unreachable}
        tone={c.down + c.unreachable ? 'text-red-400' : 'text-slate-500'}
      />
      <Kpi
        label={t('noc.devicesWarning')}
        value={c.warning}
        tone={c.warning ? 'text-amber-300' : 'text-slate-500'}
      />
      <Kpi
        label={t('noc.sensorsDown')}
        value={s.sensor_counts.down}
        tone={s.sensor_counts.down ? 'text-red-400' : 'text-slate-500'}
      />
      <Kpi label={t('noc.acknowledged')} value={s.acknowledged} tone="text-sky-300" />
      <Kpi label={t('noc.maintenance')} value={s.in_maintenance} tone="text-slate-300" />
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
            stroke={l.status === 'down' ? '#ef4444' : l.status === 'up' ? '#475569' : '#334155'}
            strokeWidth={l.status === 'down' ? 4 : 2}
            strokeDasharray={l.status === 'unknown' ? '6 6' : undefined}
          />
        );
      })}
      {map.nodes.map((n) => {
        const p = at(n.id);
        return (
          <g key={n.id}>
            <circle
              cx={p.x}
              cy={p.y}
              r={11}
              fill={STATUS_HEX[n.status] ?? '#94a3b8'}
              stroke="#0f172a"
              strokeWidth={3}
            />
            <text x={p.x} y={p.y + 30} textAnchor="middle" fontSize={16} fill="#cbd5e1">
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
            <p className="text-2xl text-green-400">{t('noc.allUp')}</p>
          ) : (
            <ul className="space-y-2">
              {v.down_devices.map((d) => (
                <li key={d.id} className="flex flex-wrap items-baseline gap-x-3">
                  <span
                    className={cx(
                      'inline-block h-4 w-4 self-center rounded-full',
                      STATUS_BG[d.status],
                    )}
                  />
                  <span className="text-2xl font-semibold">{d.name}</span>
                  {d.host && (
                    <bdi dir="ltr" className="text-slate-400">
                      {d.host}
                    </bdi>
                  )}
                  <span className="text-slate-400">{d.group}</span>
                  <span className={cx('ms-auto tabular-nums', STATUS_TEXT[d.status])}>
                    {since(d.since, now)}
                  </span>
                  {d.acknowledged && <span className="text-sky-300">✓ {t('noc.ack')}</span>}
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
            <p className="text-2xl text-green-400">{t('noc.noProblems')}</p>
          ) : (
            <ul className="space-y-1.5">
              {v.problems.map((p) => (
                <li
                  key={p.sensor_id}
                  className={cx(
                    'flex flex-wrap items-baseline gap-x-3',
                    p.acknowledged && 'opacity-60',
                  )}
                >
                  <span
                    className={cx(
                      'inline-block h-3 w-3 self-center rounded-full',
                      STATUS_BG[p.status],
                    )}
                  />
                  <span className="font-semibold">{p.device_name}</span>
                  <span className="text-slate-300">{p.sensor_name}</span>
                  <bdi dir="auto" className="min-w-0 truncate text-slate-400">
                    {p.message}
                  </bdi>
                  <span className="ms-auto tabular-nums text-slate-400">{since(p.since, now)}</span>
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
                <span className="tabular-nums text-slate-500">
                  {new Intl.DateTimeFormat(undefined, {
                    timeStyle: 'short',
                    timeZone: v.timezone,
                  }).format(new Date(a.time))}
                </span>
                <span className="font-medium">{a.device_name}</span>
                <span className="text-slate-400">{a.sensor_name}</span>
                {a.new_status && (
                  <span className={STATUS_TEXT[a.new_status as Status]}>
                    {t(`status.${a.new_status}`)}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </Panel>
      );
    case 'group_tiles':
      return (
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4 xl:grid-cols-6">
          {(v.group_tiles ?? []).map((g) => (
            <div key={g.id} className="rounded-xl bg-slate-900 p-4 ring-1 ring-slate-800">
              <div className="flex items-center gap-2">
                <span className={cx('h-4 w-4 rounded-full', STATUS_BG[g.status])} />
                <span className="truncate text-xl font-semibold">{g.name}</span>
              </div>
              <div className="mt-2 flex gap-3 text-lg tabular-nums">
                <span className="text-green-400">{g.counts.up}</span>
                <span className="text-amber-300">{g.counts.warning}</span>
                <span className="text-red-400">{g.counts.down + g.counts.unreachable}</span>
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
      className="dark min-h-screen bg-slate-950 p-6 text-[20px] text-slate-100"
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
        <h1 className="text-3xl font-bold">{v?.screen.name ?? 'NetworkMonitor'}</h1>
        <span className="ms-auto text-4xl font-semibold tabular-nums">
          {new Intl.DateTimeFormat(undefined, {
            timeStyle: 'medium',
            timeZone: v?.timezone,
          }).format(new Date(now))}
        </span>
        {st.lastOk && (
          <span className="text-base text-slate-400">
            {t('noc.updated', {
              time: new Intl.DateTimeFormat(undefined, {
                timeStyle: 'medium',
                timeZone: v?.timezone,
              }).format(new Date(st.lastOk)),
            })}
          </span>
        )}
      </header>
      {!token && <p className="mt-24 text-center text-3xl text-slate-400">{t('noc.noToken')}</p>}
      {token && st.notFound && (
        <p className="mt-24 text-center text-3xl text-slate-400" data-testid="kiosk-unavailable">
          {t('noc.unavailable')}
        </p>
      )}
      {stale && !st.notFound && (
        <div
          role="alert"
          className="mb-4 rounded-xl bg-amber-500/20 px-4 py-3 text-xl text-amber-200 ring-1 ring-amber-500/40"
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
