import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import i18n from '../i18n';
import type { Device, DeviceTemplate, ReplaceCandidate } from '../api/types';
import { initialReplacePick } from '../lib/templates';
import { ApplyTemplateModal } from './Templates';
import { mockFetch, Wrap, type Call } from '../test/v13';

beforeAll(async () => {
  await i18n.changeLanguage('en');
});
afterEach(() => vi.unstubAllGlobals());

const device = { id: 7, name: 'access-1', effective_snmp_source: 'device' } as unknown as Device;

const tpl = (id: string, name: string): DeviceTemplate => ({
  id,
  name: { en: name, he: name },
  description: { en: `${id} template`, he: id },
  requires_snmp: true,
  priority: id === 'hpe_comware' ? 50 : 10,
  match: [],
  entries: [],
});

const candidates: ReplaceCandidate[] = [
  {
    sensor_id: 3,
    name: 'CPU Load',
    type: 'snmp_cpu',
    status: 'down',
    status_message: 'HOST-RESOURCES-MIB hrProcessorLoad not supported',
    reason: 'replaced',
    from_template: null,
    failing: true,
  },
  {
    sensor_id: 4,
    name: 'Memory',
    type: 'snmp_memory',
    status: 'down',
    status_message: 'HOST-RESOURCES-MIB hrStorageRam not found',
    reason: 'replaced',
    from_template: null,
    failing: true,
  },
  {
    sensor_id: 9,
    name: 'CPU',
    type: 'snmp_custom',
    status: 'up',
    status_message: '',
    reason: 'other_template',
    from_template: 'hpe_arubaos_switch',
    failing: false,
  },
];

function routes(items: ReplaceCandidate[]) {
  return mockFetch((method, url, body) => {
    if (url.startsWith('/api/device-templates'))
      return {
        status: 200,
        body: {
          items: [
            tpl('hpe_comware', 'HPE Comware'),
            tpl('hpe_arubaos_switch', 'HPE ArubaOS-Switch'),
          ],
        },
      };
    if (method === 'GET' && url.startsWith('/api/devices/7/apply-template/replaces'))
      return { status: 200, body: { items } };
    if (method === 'POST' && url === '/api/devices/7/apply-template') {
      const ids = ((body as { remove_sensor_ids?: number[] }).remove_sensor_ids ?? []).sort();
      return {
        status: 200,
        body: {
          device_id: 7,
          template_id: 'hpe_comware',
          created: [{ sensor_id: 20, name: 'CPU Board', type: 'snmp_custom' }],
          skipped: [],
          removed: [],
          replaced: items
            .filter((c) => ids.includes(c.sensor_id))
            .map((c) => ({ sensor_id: c.sensor_id, name: c.name, type: c.type })),
        },
      };
    }
    return undefined;
  });
}

const post = (calls: Call[]) =>
  calls.find((c) => c.method === 'POST' && c.url === '/api/devices/7/apply-template');

async function clickApply(templateId: string) {
  await waitFor(() => {
    expect(screen.getByRole('button', { name: 'Apply' })).toBeEnabled();
    expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe(templateId);
  });
  fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
}

const modal = () => (
  <Wrap>
    <ApplyTemplateModal open onClose={() => undefined} device={device} />
  </Wrap>
);

describe('[template-switch] apply template: sensors the template replaces', () => {
  it('pure: only the failing sensors start ticked', () => {
    expect(initialReplacePick(candidates)).toEqual([3, 4]);
    expect(initialReplacePick([])).toEqual([]);
  });

  it('strings in both languages (Hebrew plural forms)', () => {
    const he = i18n.getFixedT('he');
    expect(he('applyReplaces.title')).toBe('חיישנים שהתבנית מחליפה');
    expect(he('applyReplaces.deleteCount', { count: 1 })).toBe(
      'חיישן אחד יימחק, עם ההיסטוריה שלו.',
    );
    expect(he('applyReplaces.deleteCount', { count: 2 })).toBe(
      '2 חיישנים יימחקו, עם ההיסטוריה שלהם.',
    );
    expect(he('applyReplaces.deleteCount', { count: 5 })).toBe(
      '5 חיישנים יימחקו, עם ההיסטוריה שלהם.',
    );
    expect(i18n.getFixedT('en')('applyReplaces.resultTitle', { count: 2 })).toBe(
      'Removed 2 sensors this template replaces',
    );
  });

  it('lists the candidates, failing ones pre-ticked, and sends the ticked ids', async () => {
    const calls = routes(candidates);
    render(modal());
    const section = await screen.findByTestId('apply-replaces');
    expect(within(section).getByRole('heading').textContent).toBe('Sensors this template replaces');
    expect(calls.find((c) => c.url.includes('/replaces'))!.url).toBe(
      '/api/devices/7/apply-template/replaces?template_id=hpe_comware',
    );
    const rows = within(section).getAllByTestId('apply-replace-row');
    expect(rows).toHaveLength(3);
    expect(rows[0].textContent).toContain('CPU Load');
    expect(rows[0].textContent).toContain('This template has its own sensor for this');
    expect(rows[0].textContent).toContain('hrProcessorLoad not supported');
    expect(within(section).getAllByTestId('apply-replace-failing')).toHaveLength(2);
    expect(rows[2].textContent).toContain('Created by the template “HPE ArubaOS-Switch”');
    const box = (name: string) =>
      within(section).getByRole('checkbox', { name: `Delete ${name}` }) as HTMLInputElement;
    expect([box('CPU Load').checked, box('Memory').checked, box('CPU').checked]).toEqual([
      true,
      true,
      false,
    ]);
    const all = within(section).getByRole('checkbox', { name: 'Select all' }) as HTMLInputElement;
    expect(all.checked).toBe(false);
    expect(all.indeterminate).toBe(true);
    expect(screen.getByTestId('apply-replaces-count').textContent).toBe(
      '2 sensors will be deleted, with their history.',
    );
    fireEvent.click(all);
    expect(screen.getByTestId('apply-replaces-count').textContent).toContain('3 sensors');
    fireEvent.click(all);
    expect(screen.getByTestId('apply-replaces-count').textContent).toBe(
      'Nothing is ticked: these sensors are kept.',
    );
    fireEvent.click(box('Memory'));
    fireEvent.click(box('CPU Load'));
    await clickApply('hpe_comware');
    await waitFor(() => expect(post(calls)).toBeTruthy());
    expect(post(calls)!.body).toEqual({ template_id: 'hpe_comware', remove_sensor_ids: [3, 4] });
    const done = await screen.findByTestId('apply-replaced');
    expect(done.textContent).toContain('Removed 2 sensors this template replaces');
    expect(done.textContent).toContain('CPU Load');
    expect(done.textContent).toContain('Memory');
  });

  it('unticked: nothing is sent for removal', async () => {
    const calls = routes(candidates);
    render(modal());
    const section = await screen.findByTestId('apply-replaces');
    await waitFor(() =>
      expect(screen.getByTestId('apply-replaces-count').textContent).toContain('2 sensors'),
    );
    fireEvent.click(within(section).getByRole('checkbox', { name: 'Delete CPU Load' }));
    fireEvent.click(within(section).getByRole('checkbox', { name: 'Delete Memory' }));
    await clickApply('hpe_comware');
    await waitFor(() => expect(post(calls)).toBeTruthy());
    expect(post(calls)!.body).toEqual({ template_id: 'hpe_comware' });
    await screen.findByTestId('apply-result');
    expect(screen.queryByTestId('apply-replaced')).toBeNull();
  });

  it('no candidates: no section, the dialog as before', async () => {
    const calls = routes([]);
    render(modal());
    await screen.findByText('hpe_comware template');
    await waitFor(() => expect(calls.some((c) => c.url.includes('/replaces'))).toBe(true));
    expect(screen.queryByTestId('apply-replaces')).toBeNull();
  });
});
