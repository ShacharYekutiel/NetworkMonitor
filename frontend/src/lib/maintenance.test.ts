import { describe, expect, it } from 'vitest';
import type { MaintenanceWindow } from '../api/types';
import {
  fromLocalInput,
  groupWindows,
  iso,
  maintenanceForm,
  maintenanceToInput,
  occurrences,
  toLocalInput,
  tzOffsetMin,
  weekdaysText,
  zonedToUtc,
} from './maintenance';

const T = (s: string) => Date.parse(s);
const DAY = 86_400_000;

describe('time-zone math (zoneinfo fold=0)', () => {
  it('converts wall-clock times and offsets', () => {
    expect(tzOffsetMin(T('2026-07-01T12:00:00Z'), 'Europe/Berlin')).toBe(120);
    expect(tzOffsetMin(T('2026-12-01T12:00:00Z'), 'Europe/Berlin')).toBe(60);
    expect(iso(zonedToUtc(2026, 11, 8, 2, 0, 'Asia/Jerusalem'))).toBe('2026-11-08T00:00:00Z');
    expect(iso(zonedToUtc(2026, 11, 8, 2, 0, 'UTC'))).toBe('2026-11-08T02:00:00Z');
  });

  it('puts a start time inside a DST gap after the gap', () => {
    // Berlin 2026-03-29: 02:00 → 03:00. 02:30 does not exist; fold=0 uses +01:00 → 03:30 CEST.
    expect(iso(zonedToUtc(2026, 3, 29, 2, 30, 'Europe/Berlin'))).toBe('2026-03-29T01:30:00Z');
    // A normal time later that day uses the new offset.
    expect(iso(zonedToUtc(2026, 3, 29, 10, 0, 'Europe/Berlin'))).toBe('2026-03-29T08:00:00Z');
  });

  it('takes the earlier instant of an ambiguous time (fall back)', () => {
    // Berlin 2026-10-25: 03:00 CEST → 02:00 CET; 02:30 happens twice, fold=0 = CEST.
    expect(iso(zonedToUtc(2026, 10, 25, 2, 30, 'Europe/Berlin'))).toBe('2026-10-25T00:30:00Z');
  });

  it('round-trips datetime-local values in the server zone', () => {
    expect(toLocalInput('2026-11-10T22:00:00Z', 'Asia/Jerusalem')).toBe('2026-11-11T00:00');
    expect(fromLocalInput('2026-11-11T00:00', 'Asia/Jerusalem')).toBe('2026-11-10T22:00:00Z');
    expect(fromLocalInput('', 'UTC')).toBeNull();
    expect(toLocalInput(null, 'UTC')).toBe('');
  });
});

describe('occurrence preview', () => {
  const weekly = { kind: 'weekly' as const, weekdays: [7], start_time: '02:00', duration_min: 90 };

  it('lists weekly occurrences in the zone', () => {
    const o = occurrences(
      weekly,
      'Asia/Jerusalem',
      T('2026-11-05T00:00:00Z'),
      T('2026-11-20T00:00:00Z'),
    );
    expect(o).toEqual([
      { start: '2026-11-08T00:00:00Z', end: '2026-11-08T01:30:00Z' },
      { start: '2026-11-15T00:00:00Z', end: '2026-11-15T01:30:00Z' },
    ]);
  });

  it('includes the current occurrence and windows across midnight', () => {
    const w = { kind: 'weekly' as const, weekdays: [6], start_time: '23:00', duration_min: 120 };
    // Saturday 2026-11-07 23:00 UTC → Sunday 01:00; "now" is Sunday 00:30.
    const o = occurrences(w, 'UTC', T('2026-11-08T00:30:00Z'), T('2026-11-10T00:00:00Z'));
    expect(o[0]).toEqual({ start: '2026-11-07T23:00:00Z', end: '2026-11-08T01:00:00Z' });
  });

  it('keeps the local start time across a DST change', () => {
    const w = { kind: 'weekly' as const, weekdays: [7], start_time: '02:30', duration_min: 60 };
    const o = occurrences(w, 'Europe/Berlin', T('2026-03-21T00:00:00Z'), T('2026-04-06T00:00:00Z'));
    expect(o.map((x) => x.start)).toEqual([
      '2026-03-22T01:30:00Z', // CET
      '2026-03-29T01:30:00Z', // gap day: lands at 03:30 CEST
      '2026-04-05T00:30:00Z', // CEST
    ]);
    // 60 real minutes, also on the gap day.
    expect(T(o[1].end) - T(o[1].start)).toBe(3_600_000);
  });

  it('merges overlapping occurrences and respects the limit', () => {
    const w = {
      kind: 'weekly' as const,
      weekdays: [1, 2],
      start_time: '00:00',
      duration_min: 2000,
    };
    const o = occurrences(w, 'UTC', T('2026-11-09T00:00:00Z'), T('2026-11-12T00:00:00Z'));
    expect(o).toEqual([{ start: '2026-11-09T00:00:00Z', end: '2026-11-11T09:20:00Z' }]);
    const many = occurrences(
      { kind: 'weekly', weekdays: [1, 2, 3, 4, 5, 6, 7], start_time: '01:00', duration_min: 10 },
      'UTC',
      T('2026-11-01T00:00:00Z'),
      T('2026-11-01T00:00:00Z') + 30 * DAY,
      5,
    );
    expect(many).toHaveLength(5);
  });

  it('handles one-time windows and invalid schedules', () => {
    const once = {
      kind: 'once' as const,
      starts_at: '2026-11-10T22:00:00Z',
      ends_at: '2026-11-11T02:00:00Z',
    };
    expect(occurrences(once, 'UTC', T('2026-11-01T00:00:00Z'), T('2026-11-20T00:00:00Z'))).toEqual([
      { start: '2026-11-10T22:00:00Z', end: '2026-11-11T02:00:00Z' },
    ]);
    expect(occurrences(once, 'UTC', T('2026-11-12T00:00:00Z'), T('2026-11-20T00:00:00Z'))).toEqual(
      [],
    );
    expect(occurrences({ ...weekly, weekdays: [] }, 'UTC', 0, DAY * 30)).toEqual([]);
    expect(occurrences({ ...weekly, start_time: '25:00' }, 'UTC', 0, DAY * 30)).toEqual([]);
  });

  it('formats weekday lists Monday-first', () => {
    expect(weekdaysText([7, 1, 3], 'en-GB')).toBe('Mon, Wed, Sun');
  });
});

describe('maintenance form mapping', () => {
  it('builds a once body in the server zone with weekly fields null', () => {
    const f = {
      ...maintenanceForm(null, 'Asia/Jerusalem'),
      name: ' Firmware night ',
      scope: 'group' as const,
      group_id: 4,
      device_id: 9,
      starts_local: '2026-11-11T00:00',
      ends_local: '2026-11-11T04:00',
    };
    const { body, errors } = maintenanceToInput(f, 'Asia/Jerusalem');
    expect(errors).toEqual({});
    expect(body).toMatchObject({
      name: 'Firmware night',
      scope: 'group',
      group_id: 4,
      device_id: null,
      kind: 'once',
      starts_at: '2026-11-10T22:00:00Z',
      ends_at: '2026-11-11T02:00:00Z',
      weekdays: null,
      start_time: null,
      duration_min: null,
    });
  });

  it('builds a weekly body (sorted distinct days) and validates', () => {
    const f = {
      ...maintenanceForm(null, 'UTC'),
      name: 'Weekly backup',
      scope: 'device' as const,
      device_id: 9,
      kind: 'weekly' as const,
      weekdays: [7, 1, 7],
      start_time: '02:00',
      duration_min: '90',
    };
    const { body, errors } = maintenanceToInput(f, 'UTC');
    expect(errors).toEqual({});
    expect(body).toMatchObject({
      weekdays: [1, 7],
      start_time: '02:00',
      duration_min: 90,
      starts_at: null,
    });
    const bad = maintenanceToInput(
      { ...f, weekdays: [], start_time: '24:00', duration_min: '0' },
      'UTC',
    );
    expect(bad.errors).toMatchObject({
      weekdays: 'maintenance.err.weekdays',
      start_time: 'maintenance.err.time',
      duration: 'maintenance.err.duration',
    });
    const once = maintenanceToInput(
      { ...f, kind: 'once', starts_local: '2026-11-11T05:00', ends_local: '2026-11-11T04:00' },
      'UTC',
    );
    expect(once.errors.ends).toBe('maintenance.err.endBeforeStart');
    expect(maintenanceToInput({ ...f, device_id: null }, 'UTC').errors.target).toBe(
      'validation.required',
    );
  });

  it('prefills from a stored window', () => {
    const f = maintenanceForm(
      {
        kind: 'once',
        starts_at: '2026-11-10T22:00:00Z',
        ends_at: '2026-11-11T02:00:00Z',
        weekdays: null,
        start_time: null,
        duration_min: null,
      } as MaintenanceWindow,
      'Asia/Jerusalem',
    );
    expect(f.starts_local).toBe('2026-11-11T00:00');
    expect(f.ends_local).toBe('2026-11-11T04:00');
  });
});

describe('window grouping', () => {
  const w = (p: Partial<MaintenanceWindow>): MaintenanceWindow =>
    ({
      id: 1,
      kind: 'once',
      active_now: false,
      occurrence_start: null,
      ends_at: null,
      ...p,
    }) as MaintenanceWindow;
  it('splits active / upcoming / recurring / past', () => {
    const now = T('2026-11-05T12:00:00Z');
    const g = groupWindows(
      [
        w({ id: 1, active_now: true, kind: 'weekly' }),
        w({ id: 2, ends_at: '2026-11-11T02:00:00Z', occurrence_start: '2026-11-10T22:00:00Z' }),
        w({ id: 3, kind: 'weekly', occurrence_start: '2026-11-08T00:00:00Z' }),
        w({ id: 4, ends_at: '2026-11-01T02:00:00Z' }),
        w({ id: 5, ends_at: '2026-11-06T02:00:00Z', occurrence_start: '2026-11-06T00:00:00Z' }),
      ],
      now,
    );
    expect(g.active.map((x) => x.id)).toEqual([1]);
    expect(g.upcoming.map((x) => x.id)).toEqual([5, 2]);
    expect(g.recurring.map((x) => x.id)).toEqual([3]);
    expect(g.past.map((x) => x.id)).toEqual([4]);
  });
});
