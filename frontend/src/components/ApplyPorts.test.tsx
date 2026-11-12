import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import i18n from '../i18n';
import type { ApplyInterface, Device, DeviceTemplate } from '../api/types';
import { applyOptions, sensorsToRemove } from '../lib/templates';
import { ApplyTemplateModal } from './Templates';
import { mockFetch, Wrap, type Call } from '../test/v13';

beforeAll(async () => {
  await i18n.changeLanguage('en');
});
afterEach(() => vi.unstubAllGlobals());

const device = {
  id: 7,
  name: 'core-sw',
  effective_snmp_source: 'device',
} as unknown as Device;

const tpl = (id: string, withIf: boolean): DeviceTemplate => ({
  id,
  name: { en: id, he: id },
  description: { en: `${id} template`, he: id },
  requires_snmp: true,
  priority: withIf ? 10 : 1,
  match: [],
  entries: withIf
    ? [
        {
          kind: 'snmp_interfaces',
          oper_up_only: true,
          exclude_if_types: [],
          include_name_regex: null,
          exclude_name_regex: null,
          max: 64,
          channels: [],
        },
      ]
    : [],
});

const ifs: ApplyInterface[] = [
  {
    if_index: 1,
    name: 'GE1/0/1',
    alias: 'to-core',
    oper_status: 'up',
    speed_bps: 1e10,
    kind: 'physical',
    important: true,
    reasons: ['uplink', 'lag'],
    has_sensor: true,
    sensor_id: 11,
  },
  {
    if_index: 2,
    name: 'GE1/0/2',
    alias: 'printer',
    oper_status: 'up',
    speed_bps: 1e9,
    kind: 'physical',
    important: false,
    reasons: [],
    has_sensor: true,
    sensor_id: 12,
  },
  {
    if_index: 3,
    name: 'GE1/0/3',
    alias: '',
    oper_status: 'down',
    speed_bps: null,
    kind: 'physical',
    important: false,
    reasons: [],
    has_sensor: false,
    sensor_id: null,
  },
];

function routes(inventory: 'ok' | 'missing' = 'ok') {
  return mockFetch((method, url) => {
    if (url.startsWith('/api/device-templates'))
      return { status: 200, body: { items: [tpl('comware', true), tpl('plain', false)] } };
    if (method === 'GET' && url.startsWith('/api/devices/7/apply-template/interfaces'))
      return { status: 200, body: { items: ifs, inventory } };
    if (method === 'POST' && url === '/api/devices/7/apply-template')
      return {
        status: 200,
        body: {
          device_id: 7,
          template_id: 'comware',
          created: [],
          skipped: [],
          removed: [{ sensor_id: 12, name: 'GE1/0/2 traffic' }],
        },
      };
    return undefined;
  });
}

const post = (calls: Call[]) =>
  calls.find((c) => c.method === 'POST' && c.url === '/api/devices/7/apply-template');

/**
 * Click the modal's Apply button deterministically.
 *
 * `ApplyTemplateModal` seeds its template `id` from an effect once the templates
 * query resolves, and `useMutation` only re-applies the mutation's `mutationFn`
 * (which closes over that `id`) in a *passive effect*. So there is a commit→effect
 * window in which the Apply button is already enabled and the select already shows
 * the template, yet `apply.mutate()` would still run the previous render's
 * `mutationFn` — closing over `id===''` — and POST `{ template_id: '' }`.
 *
 * Waiting here forces an extra act() cycle that flushes that pending passive effect
 * (and asserts the committed signal that `id` is the expected template), then we
 * re-query the button so the click always hits the current handle. No timeouts.
 */
async function clickApply(templateId: string) {
  await waitFor(() => {
    expect(screen.getByRole('button', { name: 'Apply' })).toBeEnabled();
    expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe(templateId);
  });
  fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
}

describe('[1.3] B. apply template: ports', () => {
  it('[1.3.1] labels the irf reason in both languages', () => {
    expect(i18n.getFixedT('en')('applyPorts.reason.irf')).toBe('IRF port');
    expect(i18n.getFixedT('he')('applyPorts.reason.irf')).toBe('פורט IRF');
  });

  it('pure helpers: removal count and request options', () => {
    expect(sensorsToRemove(ifs, 'important', new Set()).map((i) => i.if_index)).toEqual([2]);
    expect(sensorsToRemove(ifs, 'none', new Set()).map((i) => i.if_index)).toEqual([1, 2]);
    expect(sensorsToRemove(ifs, 'all', new Set())).toEqual([]);
    expect(sensorsToRemove(ifs, 'choose', new Set([2]))).toHaveLength(1);
    expect(applyOptions('choose', new Set([3, 1]), true)).toEqual({
      interfaces: [1, 3],
      remove_unselected_interfaces: true,
    });
    expect(applyOptions('all', new Set(), true)).toEqual({
      interfaces: 'all',
      remove_unselected_interfaces: false,
    });
  });

  it('defaults to important ports and sends them', async () => {
    const calls = routes();
    render(
      <Wrap>
        <ApplyTemplateModal open onClose={() => undefined} device={device} />
      </Wrap>,
    );
    const section = await screen.findByTestId('apply-ports');
    expect((within(section).getByLabelText('Important ports') as HTMLInputElement).checked).toBe(
      true,
    );
    // the list is loaded only on demand
    expect(calls.some((c) => c.url.includes('/apply-template/interfaces'))).toBe(false);
    await clickApply('comware');
    await waitFor(() => expect(post(calls)).toBeTruthy());
    expect(post(calls)!.body).toEqual({
      template_id: 'comware',
      interfaces: 'important',
      remove_unselected_interfaces: false,
    });
    const removed = await screen.findByTestId('apply-removed');
    expect(removed.textContent).toContain('GE1/0/2 traffic');
  });

  it('choose ports: checklist, search, buttons, removal warning', async () => {
    const calls = routes('missing');
    render(
      <Wrap>
        <ApplyTemplateModal open onClose={() => undefined} device={device} />
      </Wrap>,
    );
    const section = await screen.findByTestId('apply-ports');
    fireEvent.click(within(section).getByLabelText('Choose ports'));
    await screen.findByTestId('apply-ports-list');
    const url = calls.find((c) => c.url.includes('/apply-template/interfaces'))!.url;
    expect(url).toBe('/api/devices/7/apply-template/interfaces?template_id=comware');
    expect(screen.getByTestId('apply-ports-missing')).toBeTruthy();
    // preselected: important + monitored
    expect(screen.getByTestId('apply-ports-count').textContent).toBe('2 of 3 selected');
    const rows = screen.getAllByTestId('apply-port-row');
    expect(rows[0].textContent).toContain('Uplink');
    expect(rows[0].textContent).toContain('LAG');
    expect(rows[0].textContent).toContain('10G');
    expect(screen.getAllByTestId('has-sensor')).toHaveLength(2);
    fireEvent.change(within(section).getByLabelText('Search name or alias…'), {
      target: { value: 'print' },
    });
    expect(screen.getAllByTestId('apply-port-row')).toHaveLength(1);
    fireEvent.change(within(section).getByLabelText('Search name or alias…'), {
      target: { value: '' },
    });
    fireEvent.click(within(section).getByRole('button', { name: 'Clear' }));
    expect(screen.getByTestId('apply-ports-count').textContent).toBe('0 of 3 selected');
    fireEvent.click(within(section).getByRole('button', { name: 'Select all' }));
    expect(screen.getByTestId('apply-ports-count').textContent).toBe('3 of 3 selected');
    fireEvent.click(within(section).getByRole('button', { name: 'Select important' }));
    expect(screen.getByTestId('apply-ports-count').textContent).toBe('1 of 3 selected');
    fireEvent.click(within(section).getByLabelText(/Remove interface sensors/));
    expect(screen.getByTestId('apply-ports-remove-count').textContent).toBe(
      '1 existing interface sensor will be deleted, with its history.',
    );
    // add GE1/0/3
    fireEvent.click(within(screen.getAllByTestId('apply-port-row')[2]).getByRole('checkbox'));
    await clickApply('comware');
    await waitFor(() => expect(post(calls)).toBeTruthy());
    expect(post(calls)!.body).toEqual({
      template_id: 'comware',
      interfaces: [1, 3],
      remove_unselected_interfaces: true,
    });
  });

  it('no Ports section (and the old body) for a template without interfaces', async () => {
    const calls = routes();
    render(
      <Wrap>
        <ApplyTemplateModal
          open
          onClose={() => undefined}
          device={device}
          initialTemplate="plain"
        />
      </Wrap>,
    );
    await screen.findByText('plain template');
    expect(screen.queryByTestId('apply-ports')).toBeNull();
    await clickApply('plain');
    await waitFor(() => expect(post(calls)).toBeTruthy());
    expect(post(calls)!.body).toEqual({ template_id: 'plain' });
  });
});
