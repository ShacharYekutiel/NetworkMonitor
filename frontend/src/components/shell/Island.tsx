import { useMemo } from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useTree } from '../../hooks/queries';
import { useLiveState } from '../../hooks/live';
import { emptyCounts } from '../../lib/status';
import { cx } from '../ui';
import type { StatusCounts } from '../../api/types';

/** Sensor counts per status for the whole visible tree (root groups count recursively). */
export function useSensorCounts(): StatusCounts | null {
  const { data } = useTree();
  return useMemo(() => {
    if (!data) return null;
    const c = emptyCounts();
    for (const g of data.groups)
      for (const s of Object.keys(c) as (keyof StatusCounts)[]) c[s] += g.status_counts[s] ?? 0;
    return c;
  }, [data]);
}

export type IslandState = 'ok' | 'alert' | 'warn' | 'offline' | 'loading';

/** What the island shows for these counters and this live connection (pure, unit-tested). */
export function islandState(counts: StatusCounts | null, live: string): IslandState {
  if (live !== 'open') return 'offline';
  if (!counts) return 'loading';
  if (counts.down > 0) return 'alert';
  if (counts.warning > 0 || counts.unreachable > 0) return 'warn';
  return 'ok';
}

/**
 * The Dynamic Island: a black status pill in the middle of the top bar, fed by the live health
 * counters. Calm ("All OK") when nothing is wrong; it widens with a pulsing red dot when a sensor
 * is down. A click opens the overview (its "Needs attention" list).
 */
export function Island() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const counts = useSensorCounts();
  const live = useLiveState();
  const state = islandState(counts, live);
  const c = counts ?? emptyCounts();
  const text =
    state === 'offline'
      ? t(`live.${live}`)
      : state === 'alert'
        ? t('island.down', { count: c.down })
        : state === 'warn'
          ? t('island.attention', { count: c.warning + c.unreachable })
          : state === 'ok'
            ? t('island.ok')
            : '';
  const extra =
    state === 'alert' && c.warning > 0
      ? t('island.warnings', { count: c.warning })
      : state === 'ok' || state === 'warn'
        ? t('island.sensors', { count: c.total })
        : '';
  const dot =
    state === 'alert'
      ? 'bg-status-down shadow-[0_0_12px_var(--status-down)] motion-safe:animate-[nm-beat_1.2s_ease-in-out_infinite]'
      : state === 'warn'
        ? 'bg-status-warning shadow-[0_0_10px_var(--status-warning)]'
        : state === 'ok'
          ? 'bg-status-up shadow-[0_0_10px_var(--status-up)]'
          : 'bg-status-paused';
  return (
    <button
      type="button"
      onClick={() => {
        navigate('/');
        requestAnimationFrame(() =>
          document.getElementById('nm-problems')?.scrollIntoView({ block: 'start' }),
        );
      }}
      aria-label={[text, extra].filter(Boolean).join(' · ')}
      data-testid="island"
      data-state={state}
      className={cx(
        'flex items-center justify-center gap-2.5 overflow-hidden whitespace-nowrap rounded-full bg-island px-4 text-[13.5px] font-medium text-island-ink shadow-island transition-[min-width,height] duration-[600ms] ease-premium',
        state === 'alert' ? 'h-11 min-w-[300px]' : 'h-9 min-w-[150px]',
      )}
    >
      <span aria-hidden="true" className={cx('h-2 w-2 shrink-0 rounded-full', dot)} />
      <span aria-hidden="true">{text}</span>
      {extra && (
        <span aria-hidden="true" className="num text-xs font-medium opacity-60">
          {extra}
        </span>
      )}
      {/* The live connection word (kept for assistive tech and the E2E suite). */}
      <span className="sr-only" data-testid="ws-indicator">
        {t(`live.${live}`)}
      </span>
    </button>
  );
}
