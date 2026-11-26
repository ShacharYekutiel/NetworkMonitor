// [1.5.1] Acceptance finding: "Apply template" on an HPE Comware device preselected the first
// template of the list (OPNsense) although the device page suggests "HPE Comware switch". Opened
// without a template, the dialog preselects the device's suggested template; the caller's
// template still wins, and the first template is the fallback only without a usable suggestion.
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import i18n from '../i18n';
import type { Device, DeviceTemplate } from '../api/types';
import { ApplyTemplateModal } from './Templates';
import { mockFetch, Wrap } from '../test/v13';

beforeAll(async () => {
  await i18n.changeLanguage('en');
});
afterEach(() => vi.unstubAllGlobals());

const tpl = (id: string, name: string): DeviceTemplate => ({
  id,
  name: { en: name, he: name },
  description: { en: `${name} template`, he: name },
  requires_snmp: true,
  priority: 1,
  match: [],
  entries: [],
});

const TEMPLATES = [
  tpl('opnsense', 'OPNsense / pfSense firewall'),
  tpl('hpe_comware', 'HPE Comware switch'),
  tpl('linux', 'Linux server'),
];

const device = (suggested: string | null) =>
  ({
    id: 7,
    name: 'core-sw',
    effective_snmp_source: 'device',
    identity: suggested === null ? null : { suggested_template: suggested },
  }) as unknown as Device;

function routes() {
  mockFetch((_method, url) => {
    if (url.startsWith('/api/device-templates')) return { status: 200, body: { items: TEMPLATES } };
    if (url.startsWith('/api/devices/7/apply-template/replaces'))
      return { status: 200, body: { items: [] } };
    return undefined;
  });
}

const select = () => screen.getByRole('combobox', { name: 'Template' }) as HTMLSelectElement;

describe('ApplyTemplateModal preselection', () => {
  it('preselects the suggested template when opened without one', async () => {
    routes();
    render(
      <Wrap>
        <ApplyTemplateModal open onClose={() => undefined} device={device('hpe_comware')} />
      </Wrap>,
    );
    await waitFor(() => expect(select().value).toBe('hpe_comware'));
    expect(select().selectedOptions[0].textContent).toBe('HPE Comware switch (suggested)');
  });

  it("keeps the caller's template over the suggestion", async () => {
    routes();
    render(
      <Wrap>
        <ApplyTemplateModal
          open
          onClose={() => undefined}
          device={device('hpe_comware')}
          initialTemplate="linux"
        />
      </Wrap>,
    );
    await waitFor(() => expect(select().value).toBe('linux'));
  });

  it('falls back to the first template without a (known) suggestion', async () => {
    routes();
    const { unmount } = render(
      <Wrap>
        <ApplyTemplateModal open onClose={() => undefined} device={device(null)} />
      </Wrap>,
    );
    await waitFor(() => expect(select().value).toBe('opnsense'));
    unmount();
    render(
      <Wrap>
        <ApplyTemplateModal open onClose={() => undefined} device={device('gone')} />
      </Wrap>,
    );
    await waitFor(() => expect(select().value).toBe('opnsense'));
  });

  it('starts from the suggestion again on every opening', async () => {
    routes();
    function Host() {
      const [open, setOpen] = useState(true);
      return (
        <>
          <button type="button" onClick={() => setOpen((o) => !o)}>
            toggle
          </button>
          <ApplyTemplateModal
            open={open}
            onClose={() => setOpen(false)}
            device={device('hpe_comware')}
          />
        </>
      );
    }
    render(
      <Wrap>
        <Host />
      </Wrap>,
    );
    await waitFor(() => expect(select().value).toBe('hpe_comware'));
    fireEvent.change(select(), { target: { value: 'linux' } });
    expect(select().value).toBe('linux');
    fireEvent.click(screen.getByRole('button', { name: 'toggle' }));
    await waitFor(() => expect(screen.queryByRole('combobox', { name: 'Template' })).toBeNull());
    fireEvent.click(screen.getByRole('button', { name: 'toggle' }));
    await waitFor(() => expect(select().value).toBe('hpe_comware'));
  });
});
