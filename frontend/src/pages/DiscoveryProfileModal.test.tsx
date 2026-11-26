// [1.6.0] E2E spec 15 failure: the "Add profile" dialog opened before the group tree had loaded
// kept group_id null (the select showed the first group anyway) and Save silently did nothing.
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import i18n from '../i18n';
import { authRoutes, Wrap } from '../test/v13';
import { ProfileModal } from './DiscoveryPage';

beforeAll(async () => {
  await i18n.changeLanguage('en');
});
afterEach(() => vi.unstubAllGlobals());

const tree = {
  groups: [
    {
      id: 7,
      name: 'Site',
      parent_id: null,
      status: 'up',
      status_counts: {},
      children: [],
      devices: [],
    },
  ],
};

describe('discovery profile dialog', () => {
  it('Save works when the dialog opened before the group tree loaded', async () => {
    let releaseTree: () => void = () => undefined;
    const treeReady = new Promise<void>((r) => (releaseTree = r));
    const posts: unknown[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method ?? 'GET').toUpperCase();
        const json = (status: number, body: unknown) =>
          new Response(JSON.stringify(body), {
            status,
            headers: { 'Content-Type': 'application/json' },
          });
        const a = authRoutes(method, url);
        if (a) return json(a.status, a.body);
        if (url.startsWith('/api/tree')) {
          await treeReady; // the tree arrives after the dialog is open
          return json(200, tree);
        }
        if (method === 'POST' && url.startsWith('/api/discovery/profiles')) {
          posts.push(JSON.parse(String(init?.body)));
          return json(201, { id: 1 });
        }
        if (url.startsWith('/api/connection-profiles'))
          return json(200, { items: [], total: 0, page: 1, page_size: 1000 });
        return json(200, { items: [], total: 0, page: 1, page_size: 100 });
      }),
    );
    const onClose = vi.fn();
    render(
      <Wrap auth>
        <ProfileModal open onClose={onClose} channels={[]} />
      </Wrap>,
    );
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'Nightly' } });
    fireEvent.change(screen.getByLabelText(/^Targets/), { target: { value: '10.0.0.0/30' } });
    releaseTree();
    await screen.findByRole('option', { name: 'Site' });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({ name: 'Nightly', group_id: 7, targets: ['10.0.0.0/30'] });
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });
});
