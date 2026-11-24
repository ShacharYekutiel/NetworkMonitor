import { describe, expect, it } from 'vitest';
import { isTreePage } from './Layout';

describe('inventory panel default per page', () => {
  it('is open on tree pages and closed on list pages', () => {
    for (const p of ['/', '/groups/3', '/devices/12', '/sensors/7'])
      expect(isTreePage(p)).toBe(true);
    for (const p of ['/events', '/templates', '/settings', '/logs', '/find', '/topology'])
      expect(isTreePage(p)).toBe(false);
  });
});
