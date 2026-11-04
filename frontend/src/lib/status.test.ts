import { describe, expect, it } from 'vitest';
import {
  compareSeverity,
  countStatuses,
  isProblem,
  severity,
  STATUS_DOT,
  statusClass,
  worstStatus,
} from './status';
import type { Status } from '../api/types';

describe('status helpers', () => {
  it('orders severity down > warning > unknown > up > paused', () => {
    const s: Status[] = ['up', 'paused', 'down', 'unknown', 'warning'];
    expect([...s].sort(compareSeverity)).toEqual(['down', 'warning', 'unknown', 'up', 'paused']);
    expect(severity('down')).toBe(4);
    expect(severity('paused')).toBe(0);
  });

  it('computes the worst status, ignoring paused members', () => {
    expect(worstStatus([])).toBe('unknown');
    expect(worstStatus(['paused', 'paused'])).toBe('paused');
    expect(worstStatus(['paused', 'up'])).toBe('up');
    expect(worstStatus(['up', 'warning', 'unknown'])).toBe('warning');
    expect(worstStatus(['up', 'down', 'warning'])).toBe('down');
  });

  it('flags problems', () => {
    expect(isProblem('down')).toBe(true);
    expect(isProblem('warning')).toBe(true);
    expect(isProblem('unknown')).toBe(false);
  });

  it('uses the convention colors', () => {
    expect(STATUS_DOT).toEqual({
      up: 'bg-green-600',
      warning: 'bg-amber-500',
      down: 'bg-red-600',
      paused: 'bg-gray-400',
      unknown: 'bg-slate-500',
    });
    expect(statusClass('bogus', STATUS_DOT)).toBe('bg-slate-500');
  });

  it('counts statuses', () => {
    expect(countStatuses(['up', 'up', 'down'])).toEqual({
      up: 2,
      warning: 0,
      down: 1,
      paused: 0,
      unknown: 0,
      total: 3,
    });
  });
});
