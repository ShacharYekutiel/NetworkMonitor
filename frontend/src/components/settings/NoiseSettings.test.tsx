import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import i18n from '../../i18n';
import { ToastProvider } from '../toast';
import { ImportantPortsCard, SensorEventNoiseCard } from './NoiseSettings';
import type { Settings } from '../../api/types';

beforeAll(async () => {
  await i18n.changeLanguage('en');
});
afterEach(() => vi.unstubAllGlobals());

function mount(node: React.ReactNode) {
  const f = vi.fn(
    async () =>
      new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }),
  );
  vi.stubGlobal('fetch', f);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <ToastProvider>{node}</ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return f;
}

const body = (f: ReturnType<typeof vi.fn>) =>
  JSON.parse(String((f.mock.calls[0] as unknown as [string, RequestInit])[1].body));

describe('[if-noise] sensor event noise card', () => {
  it('defaults ON (also for a server without the keys), validates, saves both switches', async () => {
    const f = mount(<SensorEventNoiseCard settings={{} as Settings} />);
    const boxes = screen.getAllByRole('checkbox') as HTMLInputElement[];
    expect(boxes.map((b) => b.checked)).toEqual([true, true]);
    expect(screen.getByTestId('debounce-example').textContent).toBe(
      'With a 60-second check interval, a change is recorded after about 60 seconds, once the next checks confirmed it; a change back within 90 seconds is not recorded.',
    );
    const polls = screen.getByTestId('debounce-polls') as HTMLInputElement;
    const save = screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;
    fireEvent.change(polls, { target: { value: '11' } });
    expect(save.disabled).toBe(true);
    fireEvent.change(polls, { target: { value: '2' } });
    expect(screen.getByTestId('debounce-example').textContent).toContain('after about 120 seconds');
    expect(screen.getByTestId('debounce-example').textContent).toContain('within 150 seconds');
    fireEvent.click(boxes[1]); // flapping off
    fireEvent.click(save);
    await waitFor(() => expect(f).toHaveBeenCalled());
    expect(body(f)).toEqual({
      sensor_event_debounce_enabled: true,
      sensor_event_debounce_polls: 2,
      sensor_event_debounce_min_s: 0,
      sensor_event_flap_enabled: false,
      flap_threshold: 5,
      flap_window_min: 10,
      flap_quiet_min: 15,
    });
  });
});

describe('[if-noise] important ports card', () => {
  it('shows the upgrade notice while the old meaning is on, saves the criteria', async () => {
    const f = mount(
      <ImportantPortsCard
        settings={
          {
            important_port_criteria: ['uplink', 'lag', 'trunk', 'irf', 'critical_sensor'],
            important_port_description_pattern: '',
          } as Settings
        }
      />,
    );
    expect(screen.getByTestId('important-legacy-notice')).toBeTruthy();
    fireEvent.click(screen.getByLabelText(/critical interface sensor/));
    fireEvent.change(screen.getByTestId('important-pattern'), { target: { value: '(a)\\1' } });
    const save = screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.change(screen.getByTestId('important-pattern'), { target: { value: ' uplink ' } });
    fireEvent.click(save);
    await waitFor(() => expect(f).toHaveBeenCalled());
    expect(body(f)).toEqual({
      important_port_criteria: ['uplink', 'lag', 'trunk', 'irf'],
      important_port_description_pattern: 'uplink',
    });
  });

  it('every new string exists in Hebrew', () => {
    const he = i18n.getFixedT('he');
    for (const k of [
      'noise.title',
      'noise.intro',
      'noise.debounce',
      'noise.flap',
      'noise.hidden.debounced',
      'noisy.title',
      'importantPorts.legacyNotice',
      'importantPorts.criterion.critical_sensor',
      'applyFilter.preview',
      'portImportance.title',
      'snmp.defaultWarning',
      'eventKind.sensor_flapping',
    ])
      expect(he(k)).not.toBe(k);
    expect(he('noise.flapTitle', { count: 2 })).toBe('מהבהב · 2 שינויים');
    expect(he('noisy.total', { count: 1 })).toBe('שינוי סטטוס אחד בסך הכול');
  });
});
