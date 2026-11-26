// [profiles] Discovery UI: SNMP/SSH profile lists, the SSH check section, badges, save toggles.
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { useState } from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import i18n from '../../i18n';
import type { DiscoveryResult } from '../../api/types';
import { scanOptionsForm, type ScanOptionsForm } from '../../lib/discovery';
import { NO_SAVE, type SaveOn } from '../../lib/importSave';
import { authRoutes, mockFetch, Wrap } from '../../test/v13';
import { ReachabilityChips } from './Badges';
import { InheritMark, SaveCheckbox, SaveHeaderCheckbox } from './ImportSave';
import { moveItem } from './ProfileListPicker';
import { ScanOptionsEditor } from './ScanForm';

beforeAll(async () => {
  await i18n.changeLanguage('en');
});
afterEach(() => vi.unstubAllGlobals());

const prof = (id: number, kind: string, name: string) => ({
  id,
  kind,
  name,
  description: '',
  credentials: {},
  usage: { groups: 0, devices: 0, discovery_profiles: 0, devices_effective: 0 },
  created_at: '',
  updated_at: '',
});

function routes() {
  mockFetch((method, url) => {
    const a = authRoutes(method, url);
    if (a) return a;
    if (method === 'GET' && url.startsWith('/api/connection-profiles?')) {
      const items = url.includes('kind=ssh')
        ? [prof(7, 'ssh', 'Lab ssh')]
        : [prof(1, 'snmp', 'Core'), prof(2, 'snmp', 'Edge')];
      return { status: 200, body: { items, total: items.length, page: 1, page_size: 1000 } };
    }
    return undefined;
  });
}

function Editor({ onValue }: { onValue?: (v: ScanOptionsForm) => void }) {
  const [v, setV] = useState(scanOptionsForm());
  return (
    <ScanOptionsEditor
      value={v}
      onChange={(x) => {
        setV(x);
        onValue?.(x);
      }}
      errors={{}}
    />
  );
}

const result = (p: Partial<DiscoveryResult>) =>
  ({
    id: 1,
    ip: '10.0.0.1',
    ping_ok: true,
    ping_rtt_ms: 1,
    snmp_ok: true,
    snmp_version: 'v2c',
    snmp_credential: 'profile',
    snmp_profile_name: 'Core',
    open_ports: [22],
    ssh_attempted: false,
    ssh_ok: false,
    ...p,
  }) as DiscoveryResult;

describe('discovery with connection profiles', () => {
  it('moveItem reorders within bounds', () => {
    expect(moveItem([1, 2, 3], 2, 1)).toEqual([1, 3, 2]);
    expect(moveItem([1, 2, 3], 0, -1)).toEqual([1, 2, 3]);
  });

  it('SNMP profiles: add in order, reorder and remove', async () => {
    routes();
    let last: ScanOptionsForm | undefined;
    render(
      <Wrap auth>
        <Editor onValue={(v) => (last = v)} />
      </Wrap>,
    );
    const box = await screen.findByTestId('scan-snmp-profiles');
    const add = await within(box).findByRole('combobox', { name: 'Add a connection profile' });
    await waitFor(() =>
      expect(within(add).getByRole('option', { name: 'Edge' })).toBeInTheDocument(),
    );
    fireEvent.change(add, { target: { value: '2' } });
    fireEvent.change(within(box).getByRole('combobox'), { target: { value: '1' } });
    expect(last?.snmp_profile_ids).toEqual([2, 1]);
    fireEvent.click(within(box).getByRole('button', { name: 'Move Core up' }));
    expect(last?.snmp_profile_ids).toEqual([1, 2]);
    fireEvent.click(within(box).getByRole('button', { name: 'Remove Edge' }));
    expect(last?.snmp_profile_ids).toEqual([1]);
  });

  it('the SSH check is off by default; on shows the warning, profiles and typed credentials', async () => {
    routes();
    let last: ScanOptionsForm | undefined;
    render(
      <Wrap auth>
        <Editor onValue={(v) => (last = v)} />
      </Wrap>,
    );
    const sec = await screen.findByTestId('scan-ssh-check');
    expect(within(sec).queryByTestId('scan-ssh-warning')).toBeNull();
    fireEvent.click(within(sec).getByLabelText('Try to log in with SSH on the hosts found'));
    expect(within(sec).getByTestId('scan-ssh-warning')).toHaveTextContent(/lock accounts/);
    expect(within(sec).getByTestId('scan-ssh-warning')).toHaveTextContent(/never changes/);
    const add = await within(sec).findByRole('combobox', { name: 'Add a connection profile' });
    await waitFor(() =>
      expect(within(add).getByRole('option', { name: 'Lab ssh' })).toBeInTheDocument(),
    );
    fireEvent.change(add, { target: { value: '7' } });
    expect(last?.ssh_profile_ids).toEqual([7]);
    fireEvent.click(within(sec).getByLabelText('Also try typed SSH credentials'));
    expect(within(sec).getByTestId('ssh-fields')).toBeInTheDocument();
  });

  it('result badges: SNMP profile and SSH not tried / failed / logged in', () => {
    const { rerender } = render(<ReachabilityChips r={result({})} sshChecked />);
    expect(screen.getByTestId('result-snmp-profile')).toHaveTextContent('Core');
    expect(screen.getByTestId('result-ssh')).toHaveTextContent('SSH not tried');
    rerender(<ReachabilityChips r={result({ ssh_attempted: true })} sshChecked />);
    expect(screen.getByTestId('result-ssh')).toHaveTextContent('SSH ✗ failed');
    rerender(
      <ReachabilityChips
        r={result({
          ssh_attempted: true,
          ssh_ok: true,
          ssh_credential: 'profile',
          ssh_profile_name: 'Lab ssh',
        })}
        sshChecked
      />,
    );
    expect(screen.getByTestId('result-ssh')).toHaveTextContent('SSH ✓Lab ssh');
    rerender(
      <ReachabilityChips
        r={result({ ssh_attempted: true, ssh_ok: true, ssh_credential: 'typed' })}
        sshChecked
      />,
    );
    expect(screen.getByTestId('result-ssh')).toHaveTextContent('typed credentials');
    rerender(<ReachabilityChips r={result({})} />);
    expect(screen.queryByTestId('result-ssh')).toBeNull();
  });

  it('save toggles: unticked by default, per host and a tri-state column header', () => {
    const rows = [
      result({ id: 1, ssh_ok: true }),
      result({ id: 2, ip: '10.0.0.2' }),
      result({ id: 3, ip: '10.0.0.3', snmp_ok: false }),
    ];
    function T() {
      const [on, setOn] = useState<SaveOn>(NO_SAVE);
      return (
        <div>
          <SaveHeaderCheckbox rows={rows} kind="snmp" save={on} onChange={setOn} />
          {rows.map((r) => (
            <SaveCheckbox
              key={r.id}
              r={r}
              kind="snmp"
              save={on}
              onSave={(_k, v) =>
                setOn((o) => {
                  const s = new Set(o.snmp);
                  if (v) s.add(r.id);
                  else s.delete(r.id);
                  return { ...o, snmp: s };
                })
              }
            />
          ))}
        </div>
      );
    }
    render(<T />);
    const head = screen.getByRole('checkbox', { name: 'Save the SNMP settings that worked (all)' });
    const one = screen.getByRole('checkbox', {
      name: 'Save the SNMP settings that worked on 10.0.0.1',
    });
    const none = screen.getByRole('checkbox', {
      name: 'Save the SNMP settings that worked on 10.0.0.3',
    });
    expect(head).not.toBeChecked(); // owner decision 1.6.0: nothing saved by default
    expect(one).not.toBeChecked();
    expect(none).toBeDisabled();
    expect(none).not.toBeChecked();
    fireEvent.click(one);
    expect(one).toBeChecked();
    expect(head).not.toBeChecked();
    expect((head as HTMLInputElement).indeterminate).toBe(true);
    fireEvent.click(head); // mixed → all
    expect(one).toBeChecked();
    expect(
      screen.getByRole('checkbox', { name: 'Save the SNMP settings that worked on 10.0.0.2' }),
    ).toBeChecked();
    fireEvent.click(head); // all → none
    expect(one).not.toBeChecked();
    expect(
      screen.getByRole('checkbox', { name: 'Save the SNMP settings that worked on 10.0.0.2' }),
    ).not.toBeChecked();
  });

  it('inheritance mark: risk and same, short in the table, the full sentence in cards', () => {
    const { rerender } = render(<InheritMark kind="snmp" hint="risk" id={4} />);
    const m = screen.getByTestId('inherit-snmp-4');
    expect(m).toHaveAttribute('data-hint', 'risk');
    expect(m).toHaveTextContent('Group differs');
    expect(m).toHaveAttribute('title', expect.stringContaining('probably not answer SNMP'));
    rerender(<InheritMark kind="ssh" hint="same" id={4} long />);
    expect(screen.getByTestId('inherit-ssh-4')).toHaveTextContent(
      'The import group already gives the SSH login that worked: nothing needs saving.',
    );
    rerender(<InheritMark kind="snmp" hint="saved" id={4} />);
    expect(screen.queryByTestId('inherit-snmp-4')).toBeNull();
  });
});
