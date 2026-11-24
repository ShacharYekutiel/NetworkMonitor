import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import type { ReactNode } from 'react';
import i18n, { setLanguage } from '../../i18n';
import { AuthProvider } from '../../hooks/auth';
import { ThemeProvider } from '../../hooks/theme';
import { ToastProvider } from '../toast';
import { TreeNav } from '../Tree';

const counts = { up: 1, warning: 0, down: 0, paused: 0, unknown: 0, unreachable: 0, total: 1 };
const device = (id: number, name: string) => ({
  id,
  name,
  host: `10.0.0.${id}`,
  enabled: true,
  status: 'up',
  status_counts: counts,
  in_maintenance: false,
  parent_device_id: null,
  sensors: [],
});
const group = (
  id: number,
  name: string,
  parent: number | null,
  children: unknown[],
  devices: unknown[],
) => ({
  id,
  name,
  parent_id: parent,
  status: 'up',
  status_counts: counts,
  children,
  devices,
});
// Root(1) ─ Branch(2) ─ Floor(5) ; Root has core-sw(3), edge-sw(4) ; Branch has br-sw(6)
const TREE = {
  groups: [
    group(
      1,
      'Root',
      null,
      [group(2, 'Branch', 1, [group(5, 'Floor', 2, [], [])], [device(6, 'br-sw')])],
      [device(3, 'core-sw'), device(4, 'edge-sw')],
    ),
  ],
};

let role: 'admin' | 'viewer' = 'admin';
let moves: unknown[] = [];

function mockFetch() {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const json = (status: number, body: unknown) =>
      new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      });
    if (url.startsWith('/api/setup/status')) return json(200, { setup_required: false });
    if (url.startsWith('/api/auth/me'))
      return json(200, { user: { id: 1, username: role, role, language: 'en', is_active: true } });
    if (url.startsWith('/api/settings/public'))
      return json(200, { default_language: 'en', features: {} });
    if (url.startsWith('/api/tree/move') && init?.method === 'POST') {
      const body = JSON.parse(String(init.body));
      moves.push(body);
      return json(200, {
        target_group_id: body.target_group_id,
        moved: { device_ids: body.device_ids, group_ids: body.group_ids },
        skipped: { device_ids: [], group_ids: [] },
      });
    }
    if (url.startsWith('/api/tree')) return json(200, TREE);
    if (url.startsWith('/api/sensor-types')) return json(200, { items: [] });
    return json(404, { error: { code: 'not_found', message: 'nf', details: null } });
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

function wrap(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <ToastProvider>
          <MemoryRouter>
            <AuthProvider>{children}</AuthProvider>
          </MemoryRouter>
        </ToastProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}

async function row(name: string) {
  return (await screen.findByText(name)).closest('a') as HTMLAnchorElement;
}
const dropTarget = (a: HTMLElement) => a.closest('[data-drop-target]') as HTMLElement;

function dataTransfer() {
  const data: Record<string, string> = {};
  return {
    data,
    effectAllowed: 'all',
    dropEffect: 'none',
    setData: (k: string, v: string) => (data[k] = v),
    getData: (k: string) => data[k] ?? '',
    setDragImage: vi.fn(),
    types: [] as string[],
  };
}

beforeEach(async () => {
  role = 'admin';
  moves = [];
  localStorage.clear();
  setLanguage('en', false);
  await i18n.changeLanguage('en');
  mockFetch();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Tree move (drag and drop, selection, Move to…)', () => {
  it('drag a device onto a group: target highlighted, confirm, then POST', async () => {
    render(wrap(<TreeNav />));
    const src = await row('core-sw');
    await waitFor(() => expect(src).toHaveAttribute('draggable', 'true'));
    const dt = dataTransfer();
    fireEvent.dragStart(src, { dataTransfer: dt });
    expect(JSON.parse(dt.data['application/x-netmon-tree'])).toEqual([{ kind: 'device', id: 3 }]);
    const target = dropTarget(await row('Branch'));
    fireEvent.dragEnter(target, { dataTransfer: dt });
    fireEvent.dragOver(target, { dataTransfer: dt });
    await waitFor(() => expect(target).toHaveAttribute('data-drop', 'ok'));
    fireEvent.drop(target, { dataTransfer: dt });
    const dlg = await screen.findByTestId('move-confirm');
    expect(within(dlg).getByRole('heading')).toHaveTextContent('Move 1 device to Branch?');
    expect(within(dlg).getByTestId('move-confirm-items')).toHaveTextContent('core-sw');
    expect(within(dlg).getByTestId('move-confirm-hint')).toHaveTextContent(/inherited/);
    expect(moves).toEqual([]); // nothing before the confirmation
    fireEvent.click(within(dlg).getByTestId('move-confirm-ok'));
    await waitFor(() =>
      expect(moves).toEqual([{ device_ids: [3], group_ids: [], target_group_id: 2 }]),
    );
    await waitFor(() => expect(screen.queryByTestId('move-confirm')).toBeNull());
  });

  it('entering the next row before leaving the last keeps the new highlight', async () => {
    render(wrap(<TreeNav />));
    const src = await row('core-sw');
    await waitFor(() => expect(src).toHaveAttribute('draggable', 'true'));
    const dt = dataTransfer();
    fireEvent.dragStart(src, { dataTransfer: dt });
    const branch = dropTarget(await row('Branch'));
    const floor = dropTarget(await row('Floor'));
    fireEvent.dragEnter(branch, { dataTransfer: dt });
    // the browser order: dragenter on the new row, then dragleave on the old one, same tick
    fireEvent.dragEnter(floor, { dataTransfer: dt });
    fireEvent.dragLeave(branch, { dataTransfer: dt });
    await waitFor(() => expect(floor).toHaveAttribute('data-drop', 'ok'));
    expect(branch).not.toHaveAttribute('data-drop');
    fireEvent.dragEnd(src, { dataTransfer: dt });
  });

  it('cancel moves nothing', async () => {
    render(wrap(<TreeNav />));
    const src = await row('edge-sw');
    await waitFor(() => expect(src).toHaveAttribute('draggable', 'true'));
    const dt = dataTransfer();
    fireEvent.dragStart(src, { dataTransfer: dt });
    const target = dropTarget(await row('Floor'));
    fireEvent.dragOver(target, { dataTransfer: dt });
    fireEvent.drop(target, { dataTransfer: dt });
    const dlg = await screen.findByTestId('move-confirm');
    fireEvent.click(within(dlg).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByTestId('move-confirm')).toBeNull());
    expect(moves).toEqual([]);
  });

  it('a group cannot be dropped into its own subgroup', async () => {
    render(wrap(<TreeNav />));
    const src = await row('Branch');
    await waitFor(() => expect(src).toHaveAttribute('draggable', 'true'));
    const dt = dataTransfer();
    fireEvent.dragStart(src, { dataTransfer: dt });
    const target = dropTarget(await row('Floor'));
    const over = fireEvent.dragOver(target, { dataTransfer: dt });
    expect(over).toBe(true); // not prevented: the browser refuses the drop
    await waitFor(() => expect(target).toHaveAttribute('data-drop', 'no'));
    // groups alone may go to the top level: the zone is offered while dragging
    expect(screen.getByTestId('tree-drop-root')).toBeInTheDocument();
    fireEvent.drop(target, { dataTransfer: dt });
    expect(screen.queryByTestId('move-confirm')).toBeNull();
    fireEvent.dragEnd(src, { dataTransfer: dt });
    await waitFor(() => expect(screen.queryByTestId('tree-drop-root')).toBeNull());
  });

  it('Ctrl-click selects several, Shift-click a range; "Move to…" picks a group', async () => {
    render(wrap(<TreeNav />));
    const core = await row('core-sw');
    await waitFor(() => expect(core).toHaveAttribute('draggable', 'true'));
    fireEvent.click(core, { ctrlKey: true });
    fireEvent.click(await row('edge-sw'), { ctrlKey: true });
    await waitFor(() => expect(core).toHaveAttribute('data-selected', 'true'));
    expect(screen.getByTestId('tree-selection-bar')).toHaveTextContent('2 selected');
    // Shift-click: Branch .. edge-sw in display order (Branch, Floor, br-sw, core-sw, edge-sw)
    fireEvent.click(await row('Branch'), { shiftKey: true });
    await waitFor(() =>
      expect(screen.getByTestId('tree-selection-bar')).toHaveTextContent('5 selected'),
    );
    // back to two devices, then the right-click menu offers the selection
    fireEvent.click(await row('core-sw'));
    await waitFor(() => expect(screen.queryByTestId('tree-selection-bar')).toBeNull());
    fireEvent.click(await row('core-sw'), { ctrlKey: true });
    fireEvent.click(await row('edge-sw'), { metaKey: true });
    fireEvent.contextMenu(await row('edge-sw'), { clientX: 10, clientY: 10 });
    const menu = await screen.findByRole('menu');
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Move 2 selected items to…' }));
    const picker = await screen.findByTestId('move-picker');
    expect(within(picker).queryByText('Top level')).toBeNull(); // devices need a group
    const opts = within(picker).getAllByTestId('move-picker-option');
    expect(opts.map((o) => o.textContent)).toEqual(['Rootcurrent location', 'Branch', 'Floor']);
    expect(within(picker).getByTestId('move-picker-next')).toBeDisabled();
    fireEvent.click(within(picker).getByLabelText('Floor'));
    fireEvent.click(within(picker).getByTestId('move-picker-next'));
    const dlg = await screen.findByTestId('move-confirm');
    expect(within(dlg).getByRole('heading')).toHaveTextContent('Move 2 devices to Floor?');
    fireEvent.click(within(dlg).getByTestId('move-confirm-ok'));
    await waitFor(() =>
      expect(moves).toEqual([{ device_ids: [3, 4], group_ids: [], target_group_id: 5 }]),
    );
  });

  it('keyboard: Ctrl+Space selects the focused row', async () => {
    render(wrap(<TreeNav />));
    const core = await row('core-sw');
    await waitFor(() => expect(core).toHaveAttribute('draggable', 'true'));
    core.focus();
    fireEvent.keyDown(core, { key: ' ', ctrlKey: true });
    await waitFor(() => expect(core).toHaveAttribute('data-selected', 'true'));
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(core).not.toHaveAttribute('data-selected'));
  });

  it('a group moves to the top level through the picker', async () => {
    render(wrap(<TreeNav />));
    const branch = await row('Branch');
    await waitFor(() => expect(branch).toHaveAttribute('draggable', 'true'));
    fireEvent.contextMenu(branch, { clientX: 10, clientY: 10 });
    fireEvent.click(
      within(await screen.findByRole('menu')).getByRole('menuitem', { name: 'Move to…' }),
    );
    const picker = await screen.findByTestId('move-picker');
    const labels = within(picker)
      .getAllByTestId('move-picker-option')
      .map((o) => o.textContent);
    expect(labels).toEqual([
      'Top level',
      'Rootcurrent location',
      'Branchinside what you move',
      'Floorinside what you move',
    ]);
    fireEvent.click(within(picker).getByLabelText('Top level'));
    fireEvent.click(within(picker).getByTestId('move-picker-next'));
    const dlg = await screen.findByTestId('move-confirm');
    expect(within(dlg).getByRole('heading')).toHaveTextContent('Move 1 group to the top level?');
    fireEvent.click(within(dlg).getByTestId('move-confirm-ok'));
    await waitFor(() =>
      expect(moves).toEqual([{ device_ids: [], group_ids: [2], target_group_id: null }]),
    );
  });

  it('viewer: nothing to drag or select, no "Move to…"', async () => {
    role = 'viewer';
    render(wrap(<TreeNav />));
    const core = await row('core-sw');
    // wait for the role to be known (the menu is the viewer's)
    fireEvent.contextMenu(core, { clientX: 10, clientY: 10 });
    const menu = await screen.findByRole('menu');
    expect(within(menu).queryByRole('menuitem', { name: /Move/ })).toBeNull();
    expect(core).not.toHaveAttribute('draggable', 'true');
    fireEvent.click(core, { ctrlKey: true });
    expect(core).not.toHaveAttribute('data-selected');
    expect(screen.queryByTestId('tree-selection-bar')).toBeNull();
  });

  it('Hebrew: the confirmation reads naturally', async () => {
    await i18n.changeLanguage('he');
    expect(
      i18n.t('treeMove.confirmTitle', {
        what: i18n.t('treeMove.and', {
          a: i18n.t('treeMove.devices', { count: 2 }),
          b: i18n.t('treeMove.groups', { count: 1 }),
        }),
        target: 'סניף',
      }),
    ).toBe('להעביר שני התקנים וקבוצה אחת אל סניף?');
    expect(i18n.t('treeMove.andNum', { a: 'התקן אחד', b: '3 קבוצות' })).toBe('התקן אחד ו-3 קבוצות');
    expect(i18n.t('treeMenu.action.moveSelected', { count: 4 })).toBe('העברת 4 הפריטים שנבחרו אל…');
    await i18n.changeLanguage('en');
  });
});
