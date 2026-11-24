import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Link, MemoryRouter } from 'react-router';
import type { ReactNode } from 'react';
import i18n, { setLanguage } from '../../i18n';
import { AuthProvider } from '../../hooks/auth';
import { ThemeProvider } from '../../hooks/theme';
import { ToastProvider } from '../toast';
import { TreeNav } from '../Tree';
import { ContextMenu } from './ContextMenu';
import { TreeMenuProvider, useEntityMenu } from './TreeContextMenu';

const counts = { up: 1, warning: 0, down: 0, paused: 0, unknown: 0, unreachable: 0, total: 1 };
const TREE = {
  groups: [
    {
      id: 1,
      name: 'Root',
      parent_id: null,
      status: 'up',
      status_counts: counts,
      children: [],
      devices: [
        {
          id: 3,
          name: 'core-sw',
          host: '10.1.2.3',
          enabled: true,
          status: 'down',
          status_counts: counts,
          in_maintenance: false,
          parent_device_id: null,
          sensors: [
            {
              id: 7,
              name: 'Ping',
              type: 'ping',
              enabled: true,
              status: 'down',
              status_message: '',
              primary_channel: 'rtt',
              primary_value: null,
              primary_unit: 'ms',
              last_check_at: null,
              acknowledged: false,
            },
          ],
        },
      ],
    },
  ],
};

let role: 'admin' | 'viewer' = 'admin';
let lang: 'en' | 'he' = 'en';
let patches: { url: string; body: unknown }[] = [];

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
      return json(200, {
        user: { id: 1, username: role, role, language: lang, is_active: true },
      });
    if (url.startsWith('/api/settings/public'))
      return json(200, {
        default_language: 'en',
        features: { timeline: true, assistant: true, ai_explain_alert: true },
      });
    if (url.startsWith('/api/tree')) return json(200, TREE);
    if (url.startsWith('/api/sensor-types')) return json(200, { items: [] });
    if (url.startsWith('/api/devices/3') && init?.method === 'PATCH') {
      patches.push({ url, body: JSON.parse(String(init.body)) });
      return json(200, { ...TREE.groups[0].devices[0], enabled: false, group_id: 1 });
    }
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

async function deviceLink() {
  return (await screen.findByText('core-sw')).closest('a') as HTMLAnchorElement;
}
const menuItems = () =>
  within(screen.getByRole('menu'))
    .getAllByRole('menuitem')
    .map((x) => x.textContent);

beforeEach(async () => {
  role = 'admin';
  lang = 'en';
  patches = [];
  setLanguage('en', false);
  await i18n.changeLanguage('en');
  mockFetch();
});
afterEach(() => {
  vi.unstubAllGlobals();
  setLanguage('en', false);
});

describe('ContextMenu keyboard handling', () => {
  function renderMenu() {
    const onSelect = vi.fn();
    const onClose = vi.fn();
    render(
      <ContextMenu
        anchor={{ x: 10, y: 10 }}
        label="Device x"
        onSelect={onSelect}
        onClose={onClose}
        entries={[
          { type: 'item', key: 'open', label: 'Open' },
          { type: 'item', key: 'run', label: 'Run', disabled: true },
          { type: 'separator', key: 's' },
          { type: 'item', key: 'edit', label: 'Edit' },
          { type: 'item', key: 'delete', label: 'Delete', danger: true },
        ]}
      />,
    );
    return { onSelect, onClose, menu: screen.getByRole('menu', { name: 'Device x' }) };
  }

  it('focuses the first item; arrows skip disabled items and wrap; Enter activates', () => {
    const { onSelect, menu } = renderMenu();
    expect(document.activeElement).toHaveTextContent('Open');
    fireEvent.keyDown(menu, { key: 'ArrowDown' });
    expect(document.activeElement).toHaveTextContent('Edit');
    fireEvent.keyDown(menu, { key: 'ArrowDown' });
    fireEvent.keyDown(menu, { key: 'ArrowDown' });
    expect(document.activeElement).toHaveTextContent('Open');
    fireEvent.keyDown(menu, { key: 'ArrowUp' });
    expect(document.activeElement).toHaveTextContent('Delete');
    expect(document.activeElement).toHaveAttribute('data-danger', 'true');
    fireEvent.keyDown(menu, { key: 'Home' });
    fireEvent.keyDown(menu, { key: 'e' });
    expect(document.activeElement).toHaveTextContent('Edit');
    fireEvent.keyDown(menu, { key: 'Enter' });
    expect(onSelect).toHaveBeenCalledWith('edit');
    expect(screen.getAllByRole('separator')).toHaveLength(1);
  });

  it('a disabled item does nothing; Esc, Tab and an outside click close', () => {
    const { onSelect, onClose, menu } = renderMenu();
    fireEvent.click(screen.getByRole('menuitem', { name: 'Run' }));
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.keyDown(menu, { key: 'Escape' });
    expect(onClose).toHaveBeenLastCalledWith('escape');
    fireEvent.keyDown(menu, { key: 'Tab' });
    expect(onClose).toHaveBeenLastCalledWith('tab');
    fireEvent.mouseDown(document.body);
    expect(onClose).toHaveBeenLastCalledWith('outside');
  });
});

describe('Tree context menu', () => {
  it('right click on a device opens the admin menu at the pointer (browser menu prevented)', async () => {
    render(wrap(<TreeNav />));
    const link = await deviceLink();
    const ev = new MouseEvent('contextmenu', {
      bubbles: true,
      cancelable: true,
      clientX: 40,
      clientY: 50,
    });
    act(() => {
      link.dispatchEvent(ev);
    });
    expect(ev.defaultPrevented).toBe(true);
    const menu = await screen.findByRole('menu', { name: 'Device core-sw' });
    expect(menu).toBeInTheDocument();
    await waitFor(() => expect(menuItems()).toContain('Explain with AI'));
    expect(menuItems()).toEqual([
      'Open',
      'Open in new tab',
      'Timeline tab',
      'Add sensor…',
      'Apply template…',
      'Run all sensors now',
      'Pause',
      'Maintenance window…',
      'Explain with AI',
      'Copy IP address',
      'Copy name',
      'Edit…',
      'Delete…',
    ]);
  });

  it('viewer: only the safe actions', async () => {
    role = 'viewer';
    render(wrap(<TreeNav />));
    const link = await deviceLink();
    await waitFor(() => expect(screen.queryByText('Root')).toBeInTheDocument());
    // wait for /auth/me so the role is known
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    fireEvent.contextMenu(link, { clientX: 5, clientY: 5 });
    expect(menuItems()).toEqual([
      'Open',
      'Open in new tab',
      'Timeline tab',
      'Copy IP address',
      'Copy name',
    ]);
  });

  it('Shift+F10 / ContextMenu key open it; Esc closes and focus returns to the item', async () => {
    render(wrap(<TreeNav />));
    const link = await deviceLink();
    link.focus();
    fireEvent.keyDown(link, { key: 'F10', shiftKey: true });
    const menu = screen.getByRole('menu');
    expect(document.activeElement).toHaveTextContent('Open');
    fireEvent.keyDown(menu, { key: 'Escape' });
    expect(screen.queryByRole('menu')).toBeNull();
    expect(document.activeElement).toBe(link);
    fireEvent.keyDown(link, { key: 'ContextMenu' });
    expect(screen.getByRole('menu')).toBeInTheDocument();
    // the browser's echo contextmenu while that menu is open is ignored (same menu, still focused)
    fireEvent.contextMenu(link, { clientX: 300, clientY: 300 });
    expect(document.activeElement).toHaveTextContent('Open');
    // ...but a real right click right after the menu closed opens it again
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    fireEvent.contextMenu(link, { clientX: 5, clientY: 5 });
    expect(screen.getByRole('menu')).toBeInTheDocument();
  });

  it('long press (~500 ms) opens it on touch screens; a moving finger does not', async () => {
    render(wrap(<TreeNav />));
    const link = await deviceLink();
    vi.useFakeTimers();
    try {
      fireEvent.touchStart(link, { touches: [{ clientX: 30, clientY: 40 }] });
      fireEvent.touchMove(link, { touches: [{ clientX: 30, clientY: 80 }] });
      act(() => {
        vi.advanceTimersByTime(600);
      });
      expect(screen.queryByRole('menu')).toBeNull();
      fireEvent.touchStart(link, { touches: [{ clientX: 30, clientY: 40 }] });
      act(() => {
        vi.advanceTimersByTime(400);
      });
      expect(screen.queryByRole('menu')).toBeNull();
      act(() => {
        vi.advanceTimersByTime(150);
      });
      expect(screen.getByRole('menu')).toBeInTheDocument();
      // the click that follows the long press does not navigate
      const click = new MouseEvent('click', { bubbles: true, cancelable: true });
      link.dispatchEvent(click);
      expect(click.defaultPrevented).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it('sensor menu offers Acknowledge for an unacknowledged problem', async () => {
    render(wrap(<TreeNav />));
    await deviceLink();
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    const sensorLink = (await screen.findByText('Ping')).closest('a') as HTMLElement;
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    fireEvent.contextMenu(sensorLink, { clientX: 5, clientY: 5 });
    expect(screen.getByRole('menu', { name: 'Sensor Ping' })).toBeInTheDocument();
    expect(menuItems()).toContain('Acknowledge…');
    expect(menuItems()).toContain('Run now');
  });

  it('Copy IP writes the host to the clipboard; Pause patches the device', async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    render(wrap(<TreeNav />));
    const link = await deviceLink();
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    fireEvent.contextMenu(link, { clientX: 5, clientY: 5 });
    fireEvent.click(screen.getByRole('menuitem', { name: 'Copy IP address' }));
    expect(writeText).toHaveBeenCalledWith('10.1.2.3');
    expect(screen.queryByRole('menu')).toBeNull();
    expect(await screen.findByText('Copied to clipboard')).toBeInTheDocument();

    fireEvent.contextMenu(link, { clientX: 5, clientY: 5 });
    fireEvent.click(screen.getByRole('menuitem', { name: 'Pause' }));
    await waitFor(() =>
      expect(patches).toEqual([{ url: '/api/devices/3', body: { enabled: false } }]),
    );
  });

  it('Hebrew: labels are translated and the keyboard anchor is the inline start (right)', async () => {
    lang = 'he';
    setLanguage('he', false);
    await i18n.changeLanguage('he');
    render(wrap(<TreeNav />));
    const link = await deviceLink();
    link.getBoundingClientRect = () =>
      ({
        left: 20,
        right: 220,
        top: 0,
        bottom: 30,
        width: 200,
        height: 30,
        x: 20,
        y: 0,
      }) as DOMRect;
    fireEvent.keyDown(link, { key: 'F10', shiftKey: true });
    const menu = screen.getByRole('menu', { name: 'התקן core-sw' });
    expect(within(menu).getAllByRole('menuitem')[0]).toHaveTextContent('פתיחה');
    // jsdom has no layout (menu size 0): RTL places the menu's right edge at the item's right edge
    expect(menu.style.left).toBe('220px');
    expect(menu.style.top).toBe('30px');
  });
});

describe('Context menu on rows outside the tree (useEntityMenu)', () => {
  function Rows() {
    const menu = useEntityMenu();
    return (
      <ul>
        <li {...menu({ kind: 'device', id: 3 })} data-testid="dev-row">
          <Link to="/devices/3">core-sw row</Link>
          <input aria-label="note" />
        </li>
        <li {...menu({ kind: 'sensor', id: 7 })} data-testid="sensor-row">
          <Link {...menu({ kind: 'device', id: 3 })} to="/devices/3">
            device link
          </Link>
          <Link to="/sensors/7">Ping row</Link>
        </li>
        <li {...menu({ kind: 'device', id: 999 })} data-testid="unknown-row">
          gone
        </li>
      </ul>
    );
  }
  const view = () =>
    render(
      wrap(
        <TreeMenuProvider>
          <Rows />
        </TreeMenuProvider>,
      ),
    );
  const fire = (el: Element) => {
    const ev = new MouseEvent('contextmenu', {
      bubbles: true,
      cancelable: true,
      clientX: 20,
      clientY: 20,
    });
    act(() => {
      el.dispatchEvent(ev);
    });
    return ev;
  };

  it('a device row opens the same device menu as the tree (found by id)', async () => {
    view();
    const row = await screen.findByText('core-sw row');
    await waitFor(() => expect(fire(row).defaultPrevented).toBe(true));
    expect(screen.getByRole('menu', { name: 'Device core-sw' })).toBeInTheDocument();
    expect(menuItems()).toContain('Copy IP address');
    expect(menuItems()).toContain('Delete…');
  });

  it('a sensor row opens the sensor menu; the device link inside it opens the device menu', async () => {
    view();
    const sensorLink = await screen.findByText('Ping row');
    await waitFor(() => expect(fire(sensorLink).defaultPrevented).toBe(true));
    expect(screen.getByRole('menu', { name: 'Sensor Ping' })).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    fire(screen.getByText('device link'));
    expect(screen.getByRole('menu', { name: 'Device core-sw' })).toBeInTheDocument();
  });

  it('inputs and unknown entities keep the browser menu', async () => {
    view();
    await screen.findByText('core-sw row');
    await waitFor(() => expect(fire(screen.getByText('core-sw row')).defaultPrevented).toBe(true));
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    expect(fire(screen.getByLabelText('note')).defaultPrevented).toBe(false);
    expect(fire(screen.getByText('gone')).defaultPrevented).toBe(false);
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('Shift+F10 on a link in a row opens the menu; Esc returns focus to that link', async () => {
    view();
    const link = (await screen.findByText('Ping row')).closest('a')!;
    await waitFor(() => expect(screen.getByTestId('sensor-row')).toBeInTheDocument());
    // wait for the tree so the sensor is known
    await waitFor(() => {
      link.focus();
      fireEvent.keyDown(link, { key: 'F10', shiftKey: true });
      expect(screen.getByRole('menu', { name: 'Sensor Ping' })).toBeInTheDocument();
    });
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    expect(screen.queryByRole('menu')).toBeNull();
    expect(document.activeElement).toBe(link);
  });

  it('an action works from a row: Pause on the device row PATCHes the device', async () => {
    view();
    const row = await screen.findByText('core-sw row');
    await waitFor(() => expect(fire(row).defaultPrevented).toBe(true));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Pause' }));
    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).toEqual({ url: '/api/devices/3', body: { enabled: false } });
  });
});

describe('Tree inside the shell provider (the ☰ menu)', () => {
  it('uses the shell provider: exactly one menu opens', async () => {
    const { unmount } = render(
      wrap(
        <TreeMenuProvider>
          <TreeNav />
        </TreeMenuProvider>,
      ),
    );
    const link = await deviceLink();
    fireEvent.contextMenu(link, { clientX: 5, clientY: 5 });
    expect(screen.getAllByRole('menu')).toHaveLength(1);
    unmount();
  });
});
