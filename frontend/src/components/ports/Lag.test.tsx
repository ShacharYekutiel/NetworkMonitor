import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import '../../i18n';
import { mkPort } from '../../lib/portFixtures';
import { Faceplate } from './Faceplate';
import { PortList } from './PortList';

const ports = [
  mkPort(1, '1'),
  mkPort(23, '23', {
    lag: { if_index: 101, name: 'Trk1', attached: true },
    vlans_from_lag: true,
    pvid: 10,
  }),
  mkPort(24, '24', { lag: { if_index: 101, name: 'Trk1', attached: false } }),
  mkPort(101, 'Trk1', { kind: 'lag', lag_members: [23, 24] }),
];

describe('[2b-3] LAG in the ports view', () => {
  it('faceplate: bar under member tiles, legend and aria-label', () => {
    render(<Faceplate ports={ports} selected={null} onSelect={() => undefined} />);
    const legend = screen.getByTestId('lag-legend');
    expect(legend.textContent).toContain('Trk1: 23, 24');
    const tile = document.querySelector('[data-if-index="23"]') as HTMLElement;
    expect(tile.getAttribute('aria-label')).toContain('member of Trk1');
    expect(tile.querySelector('[data-lag="101"]')).not.toBeNull();
    const plain = document.querySelector('[data-if-index="1"]') as HTMLElement;
    expect(plain.querySelector('[data-lag]')).toBeNull();
  });
  it('list: member chip, LAG members and inherited VLAN marker', () => {
    render(
      <MemoryRouter>
        <PortList ports={ports} selected={null} onSelect={() => undefined} />
      </MemoryRouter>,
    );
    const chips = screen.getAllByTestId('lag-chip');
    expect(chips).toHaveLength(2);
    expect(chips[1].getAttribute('title')).toBe('Trk1 (not active)');
    expect(screen.getByTestId('lag-members').textContent).toBe('Members: 23, 24');
    const row = chips[0].closest('tr') as HTMLElement;
    expect(within(row).getByTitle('VLANs inherited from the LAG')).toBeTruthy();
  });
});
