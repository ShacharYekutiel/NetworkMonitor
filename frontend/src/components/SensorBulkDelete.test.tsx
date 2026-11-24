import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import i18n from '../i18n';
import { DeviceSensorsCard } from '../pages/DevicePage';
import { mkSensor, mockFetch, Wrap } from '../test/v13';

afterEach(() => vi.unstubAllGlobals());

const sensors = [mkSensor(1, 'Ping'), mkSensor(2, 'Gi1/0/1'), mkSensor(3, 'Gi1/0/2')];

function card(isAdmin = true) {
  return render(
    <Wrap>
      <DeviceSensorsCard
        deviceId={7}
        sensors={sensors}
        loading={false}
        error={null}
        isAdmin={isAdmin}
      />
    </Wrap>,
  );
}

describe('sensor select all + bulk delete', () => {
  beforeAll(async () => {
    await i18n.changeLanguage('en');
  });

  it('select all is tri-state and toggles every shown sensor', () => {
    card();
    const all = screen.getByTestId('sensor-select-all') as HTMLInputElement;
    expect(all).toHaveAccessibleName('Select all');
    expect(all.checked).toBe(false);
    expect(all.indeterminate).toBe(false);
    fireEvent.click(screen.getAllByTestId('sensor-select')[0]);
    expect(all.indeterminate).toBe(true);
    expect(all.checked).toBe(false);
    fireEvent.click(all);
    expect(all.checked).toBe(true);
    expect(all.indeterminate).toBe(false);
    expect(
      screen.getAllByTestId('sensor-select').every((x) => (x as HTMLInputElement).checked),
    ).toBe(true);
    fireEvent.click(all);
    expect(screen.queryByTestId('sensor-bulk-bar')).toBeNull();
  });

  it('Delete… lists the names and the history warning, then posts bulk-delete', async () => {
    const calls = mockFetch((method, url) =>
      method === 'POST' && url === '/api/sensors/bulk-delete'
        ? { status: 200, body: { deleted: 3 } }
        : undefined,
    );
    card();
    fireEvent.click(screen.getByTestId('sensor-select-all'));
    fireEvent.click(screen.getByTestId('sensor-bulk-delete'));
    const dlg = screen.getByTestId('bulk-delete');
    expect(within(dlg).getByRole('heading', { name: 'Delete 3 sensors' })).toBeTruthy();
    expect(dlg.textContent).toContain('Delete these 3 sensors and their history?');
    const items = within(screen.getByTestId('bulk-delete-list')).getAllByRole('listitem');
    expect(items.map((x) => x.textContent)).toEqual(['Ping', 'Gi1/0/1', 'Gi1/0/2']);
    fireEvent.click(within(dlg).getByRole('button', { name: 'Delete 3 sensors' }));
    await waitFor(() => expect(calls.some((c) => c.url === '/api/sensors/bulk-delete')).toBe(true));
    const c = calls.find((x) => x.url === '/api/sensors/bulk-delete')!;
    expect(c.body).toEqual({ sensor_ids: [1, 2, 3] });
    await waitFor(() => expect(screen.queryByTestId('sensor-bulk-bar')).toBeNull());
  });

  it('Hebrew names the select-all checkbox and the delete sheet', async () => {
    await i18n.changeLanguage('he');
    try {
      card();
      expect(screen.getByTestId('sensor-select-all')).toHaveAccessibleName('בחירת הכול');
      fireEvent.click(screen.getAllByTestId('sensor-select')[0]);
      fireEvent.click(screen.getAllByTestId('sensor-select')[1]);
      fireEvent.click(screen.getByTestId('sensor-bulk-delete'));
      expect(screen.getByTestId('bulk-delete').textContent).toContain('מחיקת 2 חיישנים');
    } finally {
      await i18n.changeLanguage('en');
    }
  });

  it('viewers see neither the checkboxes nor the bar', () => {
    card(false);
    expect(screen.queryByTestId('sensor-select-all')).toBeNull();
    expect(screen.queryByTestId('sensor-select')).toBeNull();
    expect(screen.queryByTestId('sensor-bulk-delete')).toBeNull();
  });
});
