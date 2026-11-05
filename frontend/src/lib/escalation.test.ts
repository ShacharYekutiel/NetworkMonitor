import { describe, expect, it } from 'vitest';
import { escalationForm, escalationToInput } from './escalation';

const ctx = { delay_s: 60, on_statuses: ['down', 'up'] as ('down' | 'warning' | 'up')[] };

describe('escalation form mapping', () => {
  it('defaults to off and sends the cleared values', () => {
    const f = escalationForm();
    expect(f.escalate).toBe(false);
    expect(f.remind).toBe(false);
    expect(escalationToInput(f, ctx)).toEqual({
      body: { escalation_after_s: null, escalation_channel_ids: [], repeat_every_s: null },
      errors: {},
    });
  });

  it('prefills minutes from a rule (seconds)', () => {
    const f = escalationForm({
      escalation_after_s: 900,
      escalation_channel_ids: [4],
      repeat_every_s: 5400,
    });
    expect(f).toEqual({
      escalate: true,
      escalation_after_min: '15',
      escalation_channel_ids: [4],
      remind: true,
      repeat_every_min: '90',
    });
    expect(
      escalationForm({ escalation_after_s: 90, escalation_channel_ids: [], repeat_every_s: null })
        .escalation_after_min,
    ).toBe('1.5');
  });

  it('converts minutes to seconds (api.md example)', () => {
    const f = {
      ...escalationForm(),
      escalate: true,
      escalation_after_min: '15',
      escalation_channel_ids: [4, 4],
      remind: true,
      repeat_every_min: '60',
    };
    expect(escalationToInput(f, ctx)).toEqual({
      body: { escalation_after_s: 900, escalation_channel_ids: [4], repeat_every_s: 3600 },
      errors: {},
    });
  });

  it('disabling escalation clears the channels too', () => {
    const f = {
      ...escalationForm({
        escalation_after_s: 900,
        escalation_channel_ids: [4],
        repeat_every_s: null,
      }),
      escalate: false,
    };
    expect(escalationToInput(f, ctx).body).toEqual({
      escalation_after_s: null,
      escalation_channel_ids: [],
      repeat_every_s: null,
    });
  });

  it('validates ranges, channels, the delay and the statuses', () => {
    const base = { ...escalationForm(), escalate: true, escalation_channel_ids: [1] };
    expect(
      escalationToInput({ ...base, escalation_after_min: '0.5' }, ctx).errors.escalation_after,
    ).toBe('escalation.err.after');
    expect(
      escalationToInput({ ...base, escalation_after_min: '10081' }, ctx).errors.escalation_after,
    ).toBe('escalation.err.after');
    expect(
      escalationToInput({ ...base, escalation_after_min: 'x' }, ctx).errors.escalation_after,
    ).toBe('escalation.err.after');
    expect(
      escalationToInput({ ...base, escalation_after_min: '2' }, { ...ctx, delay_s: 300 }).errors
        .escalation_after,
    ).toBe('escalation.err.afterDelay');
    expect(
      escalationToInput({ ...base, escalation_channel_ids: [] }, ctx).errors.escalation_channels,
    ).toBe('escalation.err.channels');
    const remind = { ...escalationForm(), remind: true, repeat_every_min: '4' };
    expect(escalationToInput(remind, ctx).errors.repeat_every).toBe('escalation.err.repeat');
    expect(escalationToInput({ ...remind, repeat_every_min: '5' }, ctx).errors).toEqual({});
    expect(
      escalationToInput({ ...remind, repeat_every_min: '5' }, { ...ctx, on_statuses: ['up'] })
        .errors.statuses,
    ).toBe('escalation.err.statuses');
    expect(
      escalationToInput({ ...base, escalation_after_min: '1,5' }, ctx).body.escalation_after_s,
    ).toBe(90);
  });
});
