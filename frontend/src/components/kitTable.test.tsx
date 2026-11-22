import { afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import i18n from '../i18n';
import { ColumnsMenu, DataTable, labelOf, useDataTable, type DataColumn } from './kitTable';

type Row = { id: number; name: string; msg: string };
const rows: Row[] = [
  { id: 1, name: 'Ping', msg: 'ok' },
  { id: 2, name: 'Portal', msg: 'Keyword not found: '.padEnd(300, 'x') },
];

function T() {
  const cols: DataColumn<Row>[] = [
    { id: 'name', label: 'Name', width: 150, cell: (r) => r.name },
    { id: 'type', label: 'Type', width: 100, cell: () => 'Ping' },
    { id: 'msg', label: 'Message', title: (r) => r.msg, cell: (r) => r.msg },
    {
      id: 'actions',
      label: 'Actions',
      pin: 'end',
      fixed: true,
      width: 100,
      cell: () => <button type="button">Run</button>,
    },
  ];
  const layout = useDataTable('unit', cols);
  return (
    <>
      <ColumnsMenu layout={layout} labelOf={(id) => labelOf(cols, id)} />
      <DataTable layout={layout} columns={layout.columns} rows={rows} rowKey={(r) => r.id} />
    </>
  );
}

const heads = () =>
  Array.from(document.querySelectorAll('thead th')).map((th) => th.getAttribute('data-col') ?? '');
const stored = () => JSON.parse(localStorage.getItem('netmon.table.unit') ?? 'null');
const sep = (name: string) => screen.getByRole('separator', { name: `Resize column ${name}` });

function rects() {
  // jsdom has no layout: give the headers 100px each, left to right in DOM order
  document.querySelectorAll<HTMLElement>('thead th').forEach((th, i) => {
    const rtl = th.closest('[dir=rtl]') !== null;
    const n = document.querySelectorAll('thead th').length;
    const left = (rtl ? n - 1 - i : i) * 100;
    th.getBoundingClientRect = () =>
      ({
        left,
        right: left + 100,
        width: 100,
        top: 0,
        bottom: 30,
        height: 30,
        x: left,
        y: 0,
      }) as DOMRect;
  });
}

beforeAll(async () => {
  await i18n.changeLanguage('en');
});
beforeEach(() => localStorage.clear());
afterEach(() => localStorage.clear());

describe('kit table columns', () => {
  it('fixed layout, a truncated long text with its full text on hover, the actions pinned at the end', () => {
    render(<T />);
    expect(heads()).toEqual(['name', 'type', 'msg', 'actions']);
    const table = screen.getByRole('table');
    expect(table.className).toContain('table-fixed');
    const cell = screen.getAllByRole('row')[2].querySelectorAll('td')[2];
    expect(cell.className).toContain('text-ellipsis');
    expect(cell.getAttribute('title')).toBe(rows[1].msg);
    const act = screen.getAllByRole('row')[2].querySelectorAll('td')[3];
    expect(act.className).toContain('sticky');
    expect(act.className).toContain('end-0');
    // the selection / actions columns are neither movable nor resizable
    expect(screen.queryByRole('separator', { name: /Actions/ })).toBeNull();
    expect(document.querySelector('th[data-col=actions]')!.hasAttribute('data-col-movable')).toBe(
      false,
    );
  });

  it('keyboard resize (arrows), min width, Enter resets; remembered per browser', () => {
    const { unmount } = render(<T />);
    const s = sep('Name');
    expect(s).toHaveAttribute('aria-valuenow', '150');
    fireEvent.keyDown(s, { key: 'ArrowRight' });
    expect(sep('Name')).toHaveAttribute('aria-valuenow', '166');
    expect(stored().widths).toEqual({ name: 166 });
    for (let i = 0; i < 20; i++) fireEvent.keyDown(sep('Name'), { key: 'ArrowLeft' });
    expect(sep('Name')).toHaveAttribute('aria-valuenow', '56');
    unmount();
    render(<T />);
    expect(sep('Name')).toHaveAttribute('aria-valuenow', '56');
    expect(document.querySelector('col[data-col=name]')!.getAttribute('style')).toContain('56px');
    fireEvent.keyDown(sep('Name'), { key: 'Enter' });
    expect(sep('Name')).toHaveAttribute('aria-valuenow', '150');
    expect(stored()).toBeNull();
  });

  it('double-click on the edge resets that column', () => {
    render(<T />);
    fireEvent.keyDown(sep('Type'), { key: 'ArrowRight', shiftKey: true });
    expect(sep('Type')).toHaveAttribute('aria-valuenow', '148');
    fireEvent.doubleClick(sep('Type'));
    expect(sep('Type')).toHaveAttribute('aria-valuenow', '100');
  });

  it('pointer resize of the end edge: mirrored in RTL', () => {
    render(
      <div dir="rtl" style={{ direction: 'rtl' }}>
        <T />
      </div>,
    );
    rects();
    const s = sep('Name');
    fireEvent.pointerDown(s, { button: 0, clientX: 300 });
    act(() => {
      window.dispatchEvent(new MouseEvent('pointermove', { clientX: 260 }));
      window.dispatchEvent(new MouseEvent('pointerup', { clientX: 260 }));
    });
    // dragged 40px to the left = towards the end edge in RTL: wider
    expect(stored().widths.name).toBe(140);
    fireEvent.keyDown(sep('Name'), { key: 'ArrowLeft' });
    expect(stored().widths.name).toBe(156);
  });

  it('drag a header onto another: the drop line shows, the order changes and is remembered', () => {
    const { unmount } = render(<T />);
    rects();
    const msg = document.querySelector<HTMLElement>('th[data-col=msg]')!;
    fireEvent.pointerDown(msg, { button: 0, clientX: 250, clientY: 10 });
    act(() => {
      window.dispatchEvent(new MouseEvent('pointermove', { clientX: 200, clientY: 10 }));
      window.dispatchEvent(new MouseEvent('pointermove', { clientX: 20, clientY: 10 }));
    });
    expect(document.querySelector('th[data-col=name]')!.getAttribute('data-drop')).toBe('before');
    expect(msg.hasAttribute('data-dragging')).toBe(true);
    act(() => {
      window.dispatchEvent(new MouseEvent('pointerup', { clientX: 20, clientY: 10 }));
    });
    expect(heads()).toEqual(['msg', 'name', 'type', 'actions']);
    expect(stored().order).toEqual(['msg', 'name', 'type']);
    unmount();
    render(<T />);
    expect(heads()).toEqual(['msg', 'name', 'type', 'actions']);
  });

  it('RTL drop: the right half of a header is its start ("before")', () => {
    render(
      <div dir="rtl" style={{ direction: 'rtl' }}>
        <T />
      </div>,
    );
    rects(); // RTL: name is the right-most (300..400), type 200..300, msg 100..200
    const type = document.querySelector<HTMLElement>('th[data-col=type]')!;
    fireEvent.pointerDown(type, { button: 0, clientX: 250, clientY: 10 });
    act(() => {
      window.dispatchEvent(new MouseEvent('pointermove', { clientX: 390, clientY: 10 }));
      window.dispatchEvent(new MouseEvent('pointerup', { clientX: 390, clientY: 10 }));
    });
    expect(heads()).toEqual(['type', 'name', 'msg', 'actions']);
  });

  it('a click (no movement) on a header does not move anything', () => {
    render(<T />);
    rects();
    const th = document.querySelector<HTMLElement>('th[data-col=type]')!;
    fireEvent.pointerDown(th, { button: 0, clientX: 150, clientY: 10 });
    act(() => {
      window.dispatchEvent(new MouseEvent('pointerup', { clientX: 151, clientY: 10 }));
    });
    expect(heads()).toEqual(['name', 'type', 'msg', 'actions']);
    expect(stored()).toBeNull();
  });

  it('Columns popover: move earlier / later without a mouse, then Reset columns', () => {
    render(<T />);
    fireEvent.click(screen.getByRole('button', { name: 'Columns' }));
    const menu = screen.getByTestId('columns-menu');
    expect(within(menu).getByRole('button', { name: 'Move Name earlier' })).toBeDisabled();
    fireEvent.click(within(menu).getByRole('button', { name: 'Move Name later' }));
    expect(heads()).toEqual(['type', 'name', 'msg', 'actions']);
    fireEvent.click(within(menu).getByRole('button', { name: 'Move Message earlier' }));
    expect(heads()).toEqual(['type', 'msg', 'name', 'actions']);
    fireEvent.keyDown(sep('Name'), { key: 'ArrowRight' });
    expect(stored()).toEqual({ order: ['type', 'msg', 'name'], widths: { name: 166 } });
    fireEvent.click(within(menu).getByTestId('columns-reset'));
    expect(heads()).toEqual(['name', 'type', 'msg', 'actions']);
    expect(stored()).toBeNull();
    // the actions column is not listed (not movable)
    expect(within(menu).queryByText('Actions')).toBeNull();
  });
});
