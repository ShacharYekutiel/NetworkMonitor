// [profiles] config.changed for connection profiles invalidates profiles, groups, devices and the tree.
import { describe, expect, it, vi } from 'vitest';
import { QueryClient } from '@tanstack/react-query';
import { applyLiveMessage, InvalidationBatcher } from './liveCache';

describe('[profiles] live updates', () => {
  it('invalidates profile, group, device and tree queries', () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, 'invalidateQueries');
    const batch = new InvalidationBatcher(qc, 10_000);
    applyLiveMessage(
      qc,
      {
        type: 'config.changed',
        ts: '',
        data: { entity: 'connection_profile', id: 5, action: 'updated' },
      },
      batch,
    );
    batch.flush();
    const keys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    for (const k of [
      '["connectionProfiles"]',
      '["connectionProfileUsage"]',
      '["tree"]',
      '["group"]',
      '["groups"]',
      '["device"]',
      '["devices"]',
    ])
      expect(keys).toContain(k);
    batch.dispose();
  });
});
