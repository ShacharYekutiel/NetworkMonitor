import { describe, expect, it } from 'vitest';
import { monthDays } from './assistant';

describe('monthDays (usage chart axis)', () => {
  it('fills every day of the month, keeping the reported ones', () => {
    const days = monthDays('2026-02', [
      { day: '2026-02-03', questions: 2, tokens: 900, cost_estimate: 0.1 },
    ]);
    expect(days).toHaveLength(28);
    expect(days[0]).toEqual({ day: '2026-02-01', questions: 0, tokens: 0, cost_estimate: null });
    expect(days[2].tokens).toBe(900);
    expect(days[27].day).toBe('2026-02-28');
  });

  it('knows leap years and 31-day months', () => {
    expect(monthDays('2028-02', [])).toHaveLength(29);
    expect(monthDays('2026-12', [])).toHaveLength(31);
  });
});
