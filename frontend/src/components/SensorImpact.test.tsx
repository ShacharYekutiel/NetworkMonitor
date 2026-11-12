import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import i18n from '../i18n';
import { DeviceSensorsCard } from '../pages/DevicePage';
import { EditSensorModal } from './SensorForms';
import { SensorTable } from './SensorTable';
import { mkSensor, mockFetch, Wrap } from '../test/v13';

beforeAll(async () => {
  await i18n.changeLanguage('en');
});
afterEach(() => vi.unstubAllGlobals());

const sensors = [
  mkSensor(1, 'Ping'),
  mkSensor(2, 'Gi1/0/1', { impact: 'warning' }),
  mkSensor(3, 'Gi1/0/2', { impact: 'none' }),
];

describe('[1.3] A. sensor impact', () => {
  it('table tags warning/none only (critical is unmarked)', () => {
    render(
      <Wrap>
        <SensorTable sensors={sensors} />
      </Wrap>,
    );
    const tags = screen.getAllByTestId('impact-tag');
    expect(tags.map((x) => x.textContent)).toEqual(['warning only', 'no impact']);
    expect(screen.queryByTestId('sensor-select')).toBeNull();
  });

  it('select all + "Set impact…" posts bulk-impact and clears the selection', async () => {
    const calls = mockFetch((method, url) =>
      method === 'POST' && url === '/api/sensors/bulk-impact'
        ? { status: 200, body: { updated: 3 } }
        : undefined,
    );
    render(
      <Wrap>
        <DeviceSensorsCard deviceId={7} sensors={sensors} loading={false} error={null} isAdmin />
      </Wrap>,
    );
    expect(screen.queryByTestId('sensor-bulk-bar')).toBeNull();
    fireEvent.click(screen.getAllByTestId('sensor-select')[1]);
    expect(screen.getByTestId('sensor-bulk-bar').textContent).toContain('1 selected');
    fireEvent.click(screen.getByTestId('sensor-select-all'));
    expect(screen.getByTestId('sensor-bulk-bar').textContent).toContain('3 selected');
    fireEvent.click(screen.getByRole('button', { name: 'Set impact…' }));
    const dlg = screen.getByRole('dialog');
    fireEvent.click(within(dlg).getByLabelText('None'));
    fireEvent.click(within(dlg).getByRole('button', { name: 'Apply' }));
    await waitFor(() => expect(calls.some((c) => c.url === '/api/sensors/bulk-impact')).toBe(true));
    const c = calls.find((x) => x.url === '/api/sensors/bulk-impact')!;
    expect(c.method).toBe('POST');
    expect(c.body).toEqual({ sensor_ids: [1, 2, 3], impact: 'none' });
    await waitFor(() => expect(screen.queryByTestId('sensor-bulk-bar')).toBeNull());
  });

  it('viewers get no checkboxes', () => {
    render(
      <Wrap>
        <DeviceSensorsCard
          deviceId={7}
          sensors={sensors}
          loading={false}
          error={null}
          isAdmin={false}
        />
      </Wrap>,
    );
    expect(screen.queryByTestId('sensor-select-all')).toBeNull();
  });

  it('edit form sends the chosen impact in PATCH', async () => {
    const calls = mockFetch((method, url, body) => {
      if (url.startsWith('/api/sensor-types'))
        return {
          status: 200,
          body: {
            items: [
              {
                type: 'ping',
                requires_snmp: false,
                primary_channel: 'value',
                config_fields: [],
                channels: [],
              },
            ],
          },
        };
      if (method === 'PATCH' && url === '/api/sensors/2')
        return { status: 200, body: { ...sensors[1], ...(body as object) } };
      return undefined;
    });
    render(
      <Wrap>
        <EditSensorModal open onClose={() => undefined} sensor={sensors[1]} />
      </Wrap>,
    );
    const field = await screen.findByTestId('impact-field');
    expect((within(field).getByLabelText('Warning') as HTMLInputElement).checked).toBe(true);
    expect(screen.getByText('Impact on device status')).toBeTruthy();
    fireEvent.click(within(field).getByLabelText('Critical'));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PATCH')).toBe(true));
    const c = calls.find((x) => x.method === 'PATCH')!;
    expect((c.body as { impact: string }).impact).toBe('critical');
  });
});
