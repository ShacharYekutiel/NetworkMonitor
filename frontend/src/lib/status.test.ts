import { describe, expect, it } from 'vitest';
import {
  canAcknowledge,
  compareSeverity,
  COUNT_ORDER,
  countStatuses,
  emptyCounts,
  isProblem,
  severity,
  STATUS_BADGE,
  STATUS_DOT,
  STATUS_HEX,
  STATUS_SOFT,
  STATUS_TEXT,
  STATUSES,
  statusClass,
  worstStatus,
} from './status';
import type { Status } from '../api/types';

describe('status helpers', () => {
  it('orders severity down > warning > unreachable > unknown > up > paused (2b-1)', () => {
    const s: Status[] = ['up', 'unreachable', 'paused', 'down', 'unknown', 'warning'];
    expect([...s].sort(compareSeverity)).toEqual([
      'down',
      'warning',
      'unreachable',
      'unknown',
      'up',
      'paused',
    ]);
    expect(severity('down')).toBe(5);
    expect(severity('warning')).toBe(4);
    expect(severity('unreachable')).toBe(3);
    expect(severity('unknown')).toBe(2);
    expect(severity('up')).toBe(1);
    expect(severity('paused')).toBe(0);
    expect(STATUSES).toEqual(['down', 'warning', 'unreachable', 'unknown', 'up', 'paused']);
  });

  it('computes the worst status, ignoring paused members', () => {
    expect(worstStatus([])).toBe('unknown');
    expect(worstStatus(['paused', 'paused'])).toBe('paused');
    expect(worstStatus(['paused', 'up'])).toBe('up');
    expect(worstStatus(['up', 'warning', 'unknown'])).toBe('warning');
    expect(worstStatus(['up', 'down', 'warning'])).toBe('down');
  });

  it('rolls up unreachable: a dead parent beats up/unknown, never a real warning/down', () => {
    expect(worstStatus(['up', 'unreachable'])).toBe('unreachable');
    expect(worstStatus(['unknown', 'unreachable', 'paused'])).toBe('unreachable');
    expect(worstStatus(['unreachable', 'warning'])).toBe('warning');
    // A group with a down router and its unreachable children shows the root cause.
    expect(worstStatus(['down', 'unreachable', 'unreachable'])).toBe('down');
  });

  it('flags problems; unreachable is neither a problem nor acknowledgeable', () => {
    expect(isProblem('down')).toBe(true);
    expect(isProblem('warning')).toBe(true);
    expect(isProblem('unknown')).toBe(false);
    expect(isProblem('unreachable')).toBe(false);
    expect(canAcknowledge({ status: 'down' })).toBe(true);
    expect(canAcknowledge({ status: 'warning' })).toBe(true);
    expect(canAcknowledge({ status: 'unreachable' })).toBe(false);
    expect(canAcknowledge({ status: 'up' })).toBe(false);
  });

  it('uses the status design tokens', () => {
    expect(STATUS_DOT).toEqual({
      up: 'bg-status-up',
      warning: 'bg-status-warning',
      down: 'bg-status-down',
      paused: 'bg-status-paused',
      unknown: 'bg-status-unknown',
      unreachable: 'bg-status-unreachable',
    });
    expect(STATUS_HEX.unreachable).toBe('#8b5cf6');
    expect(STATUS_BADGE.unreachable).toBe('bg-status-unreachable text-on-status');
    expect(STATUS_SOFT.unreachable).toBe('bg-status-unreachable-soft text-status-unreachable');
    expect(STATUS_TEXT.unreachable).toBe('text-status-unreachable');
    expect(statusClass('bogus', STATUS_DOT)).toBe('bg-status-unknown');
    // Every map has every status.
    for (const m of [STATUS_DOT, STATUS_TEXT, STATUS_BADGE, STATUS_SOFT, STATUS_HEX])
      expect(Object.keys(m).sort()).toEqual([...STATUSES].sort());
  });

  it('counts statuses incl. unreachable', () => {
    expect(countStatuses(['up', 'up', 'down', 'unreachable'])).toEqual({
      up: 2,
      warning: 0,
      down: 1,
      paused: 0,
      unknown: 0,
      unreachable: 1,
      total: 4,
    });
    expect(emptyCounts().unreachable).toBe(0);
    expect(COUNT_ORDER).toContain('unreachable');
    expect(COUNT_ORDER.indexOf('unreachable')).toBeLessThan(COUNT_ORDER.indexOf('up'));
  });
});
