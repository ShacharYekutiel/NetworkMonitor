import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import i18n from '../../i18n';
import type { EffectiveEventNoise, NoisySensor } from '../../api/types';
import { DeviceSensorsCard } from '../../pages/DevicePage';
import { EditSensorModal } from '../SensorForms';
import { NoisySensorsCard } from '../NoisySensors';
import { authRoutes, mkSensor, mockFetch, Wrap } from '../../test/v13';
import { NoiseOverrideCard, NoiseOverrideFields } from './NoiseOverride';
import { noiseFormFrom } from '../../lib/noiseOverride';

beforeAll(async () => {
  await i18n.changeLanguage('en');
});
afterEach(async () => {
  vi.unstubAllGlobals();
  await i18n.changeLanguage('en');
});

const g = <T,>(value: T) => ({
  value,
  source: 'global' as const,
  source_id: null,
  source_name: null,
});
const EFF: EffectiveEventNoise = {
  debounce: { value: false, source: 'sensor', source_id: 2, source_name: 'Gi1/0/1' },
  flap: { value: true, source: 'group', source_id: 4, source_name: 'Floor 2' },
  debounce_polls: g(1),
  debounce_min_s: g(0),
};
const ALL_GLOBAL: EffectiveEventNoise = {
  debounce: g(true),
  flap: g(true),
  debounce_polls: g(1),
  debounce_min_s: g(0),
};
const OWN = {
  debounce: 'off' as const,
  flap: 'inherit' as const,
  debounce_polls: null,
  debounce_min_s: null,
};

describe('[if-noise] override card', () => {
  it('shows the effective values and where they come from', () => {
    render(
      <Wrap>
        <NoiseOverrideCard effective={EFF} level="sensor" selfId={2} onEdit={() => undefined} />
      </Wrap>,
    );
    const card = screen.getByTestId('noise-override-card');
    expect(within(card).getByTestId('noise-override-debounce').textContent).toBe(
      'Wait until a change holdsOff set on this sensor',
    );
    expect(within(card).getByTestId('noise-override-flap').textContent).toContain(
      'from group “\u2068Floor 2\u2069”',
    );
    // values still from the global settings are not listed
    expect(within(card).queryByTestId('noise-override-debounce_polls')).toBeNull();
    expect(screen.getByTestId('noise-override-edit')).toBeTruthy();
  });

  it('renders nothing when the global settings decide everything', () => {
    const { container } = render(
      <Wrap>
        <NoiseOverrideCard effective={ALL_GLOBAL} level="device" />
      </Wrap>,
    );
    expect(container.querySelector('[data-testid="noise-override-card"]')).toBeNull();
  });

  it('reads naturally in Hebrew', async () => {
    await i18n.changeLanguage('he');
    render(
      <Wrap>
        <NoiseOverrideCard effective={EFF} level="device" selfId={7} />
      </Wrap>,
    );
    const card = screen.getByTestId('noise-override-card');
    expect(card.textContent).toContain('מהחיישן „\u2068Gi1/0/1\u2069”');
    expect(card.textContent).toContain('מהקבוצה „\u2068Floor 2\u2069”');
    expect(card.textContent).toContain('כבוי');
  });
});

describe('[if-noise] override editor', () => {
  it('hints the inherited value and hides the values while debounce is off', () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <Wrap>
        <NoiseOverrideFields
          value={noiseFormFrom(null)}
          onChange={onChange}
          level="device"
          selfId={7}
          effective={ALL_GLOBAL}
        />
      </Wrap>,
    );
    expect(screen.getAllByText('Now: On · global setting')).toHaveLength(2);
    expect((screen.getByTestId('noise-override-polls') as HTMLInputElement).placeholder).toBe(
      'Inherit (1)',
    );
    fireEvent.change(screen.getByTestId('noise-override-debounce'), { target: { value: 'off' } });
    expect(onChange).toHaveBeenCalledWith({
      debounce: 'off',
      flap: 'inherit',
      polls: '',
      minS: '',
    });
    rerender(
      <Wrap>
        <NoiseOverrideFields
          value={{ ...noiseFormFrom(null), debounce: 'off' }}
          onChange={onChange}
          level="device"
        />
      </Wrap>,
    );
    expect(screen.queryByTestId('noise-override-polls')).toBeNull();
  });

  it('the sensor form sends only the changed keys', async () => {
    const sensor = mkSensor(2, 'Gi1/0/1', { event_noise: OWN, effective_event_noise: EFF });
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
        return { status: 200, body: { ...sensor, ...(body as object) } };
      return undefined;
    });
    render(
      <Wrap>
        <EditSensorModal open onClose={() => undefined} sensor={sensor} />
      </Wrap>,
    );
    const flap = await screen.findByTestId('noise-override-flap');
    fireEvent.change(flap, { target: { value: 'off' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PATCH')).toBe(true));
    const c = calls.find((x) => x.method === 'PATCH')!;
    expect((c.body as { event_noise: unknown }).event_noise).toEqual({ flap: 'off' });
  });

  it('event sensors have no editor', async () => {
    mockFetch((_m, url) =>
      url.startsWith('/api/sensor-types')
        ? {
            status: 200,
            body: {
              items: [
                {
                  type: 'trap',
                  passive: true,
                  requires_snmp: false,
                  primary_channel: 'count',
                  config_fields: [],
                  channels: [],
                },
              ],
            },
          }
        : undefined,
    );
    render(
      <Wrap>
        <EditSensorModal
          open
          onClose={() => undefined}
          sensor={mkSensor(3, 'Traps', { type: 'trap' })}
        />
      </Wrap>,
    );
    await screen.findByRole('button', { name: 'Save' });
    expect(screen.queryByTestId('noise-override')).toBeNull();
  });
});

describe('[if-noise] bulk and quick action', () => {
  it('"Noise protection…" in the sensor bulk bar posts bulk-event-noise', async () => {
    const calls = mockFetch((method, url) =>
      method === 'POST' && url === '/api/sensors/bulk-event-noise'
        ? { status: 200, body: { updated: 2 } }
        : undefined,
    );
    render(
      <Wrap>
        <DeviceSensorsCard
          deviceId={7}
          sensors={[mkSensor(1, 'a'), mkSensor(2, 'b')]}
          loading={false}
          error={null}
          isAdmin
        />
      </Wrap>,
    );
    fireEvent.click(screen.getByTestId('sensor-select-all'));
    fireEvent.click(screen.getByTestId('sensor-bulk-noise'));
    const dlg = screen.getByTestId('bulk-noise');
    const apply = within(dlg).getByRole('button', { name: 'Apply' }) as HTMLButtonElement;
    expect(apply.disabled).toBe(true); // nothing chosen yet
    fireEvent.change(within(dlg).getByTestId('bulk-noise-flap'), { target: { value: 'off' } });
    fireEvent.change(within(dlg).getByTestId('bulk-noise-polls-mode'), {
      target: { value: 'set' },
    });
    fireEvent.change(within(dlg).getByTestId('bulk-noise-polls'), { target: { value: '3' } });
    fireEvent.click(apply);
    await waitFor(() =>
      expect(calls.some((c) => c.url === '/api/sensors/bulk-event-noise')).toBe(true),
    );
    const c = calls.find((x) => x.url === '/api/sensors/bulk-event-noise')!;
    expect(c.body).toEqual({
      sensor_ids: [1, 2],
      event_noise: { flap: 'off', debounce_polls: 3 },
    });
  });

  it('the noisiest sensors table sets one sensor’s override', async () => {
    const item: NoisySensor = {
      sensor_id: 9,
      sensor_name: 'GE1/0/12',
      sensor_type: 'snmp_interface',
      status: 'up',
      device_id: 3,
      device_name: 'sw-floor2',
      changes: 40,
      shown: 2,
      hidden: 38,
      last_change: null,
      flapping: true,
      event_noise: OWN,
      effective_event_noise: { ...ALL_GLOBAL, debounce: EFF.debounce },
    };
    const calls = mockFetch((method, url, body) => {
      const a = authRoutes(method, url);
      if (a) return a;
      if (url.startsWith('/api/events/noisy-sensors'))
        return { status: 200, body: { hours: 24, total_changes: 40, items: [item] } };
      if (method === 'PATCH' && url === '/api/sensors/9')
        return { status: 200, body: { ...mkSensor(9, 'GE1/0/12'), ...(body as object) } };
      return undefined;
    });
    render(
      <Wrap auth>
        <NoisySensorsCard />
      </Wrap>,
    );
    expect(await screen.findByTestId('noise-override-tag')).toBeTruthy();
    fireEvent.click(await screen.findByTestId('noisy-protection'));
    const dlg = screen.getByTestId('sensor-noise-modal');
    fireEvent.change(within(dlg).getByTestId('noise-override-debounce'), {
      target: { value: 'inherit' },
    });
    fireEvent.change(within(dlg).getByTestId('noise-override-flap'), { target: { value: 'off' } });
    fireEvent.click(within(dlg).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PATCH')).toBe(true));
    expect(calls.find((c) => c.method === 'PATCH')!.body).toEqual({
      event_noise: { debounce: 'inherit', flap: 'off' },
    });
  });
});
