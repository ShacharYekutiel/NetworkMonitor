import { afterAll, describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import i18n from '../../i18n';
import type { SnmpWalk } from '../../api/types';
import { WalkResult } from './TemplateDraftWizard';

const base: SnmpWalk = {
  id: 1,
  device_id: 3,
  status: 'failed',
  subtrees: ['1.3.6.1.2.1.1', '1.3.6.1.2.1.47.1.1.1'],
  counts: { '1.3.6.1.2.1.1': 0 },
  total: 0,
  outcomes: [
    {
      subtree: '1.3.6.1.2.1.1',
      status: 'error',
      count: 0,
      error_code: 'auth_failed',
      error: 'Wrong SNMP PDU digest',
    },
    {
      subtree: '1.3.6.1.2.1.47.1.1.1',
      status: 'skipped',
      count: 0,
      error_code: 'auth_failed',
      error: 'not walked: Wrong SNMP PDU digest',
    },
  ],
  error_code: 'auth_failed',
  error: 'Wrong SNMP PDU digest',
  started_at: '2026-11-13T10:00:00Z',
  finished_at: '2026-11-13T10:00:01Z',
};

afterAll(async () => {
  await i18n.changeLanguage('en');
});

describe('[maint-1.4] walk result: per-subtree outcome and the real SNMP error', () => {
  it('auth failure: the cause, not a timeout, plus the device text (en)', async () => {
    await i18n.changeLanguage('en');
    render(<WalkResult walk={base} />);
    const err = screen.getByTestId('walk-error');
    expect(err.dataset.code).toBe('auth_failed');
    expect(err.textContent).toMatch(/rejected the SNMP credentials/);
    expect(err.textContent).toMatch(/Wrong SNMP PDU digest/);
    expect(err.textContent).not.toMatch(/timeout|too slowly/i);
    const items = screen.getByTestId('walk-subtrees').querySelectorAll('li');
    expect([...items].map((li) => li.dataset.outcome)).toEqual(['error', 'skipped']);
    expect(items[1].textContent).toMatch(/not walked/);
  });

  it('Hebrew texts', async () => {
    await i18n.changeLanguage('he');
    render(
      <WalkResult
        walk={{
          ...base,
          error_code: 'unreachable',
          error: 'No SNMP response received before timeout',
        }}
      />,
    );
    expect(screen.getByTestId('walk-error').textContent).toMatch(/ההתקן לא ענה ל-SNMP/);
  });

  it('partial walk: the vendor subtree ran out of time, the rest complete', async () => {
    await i18n.changeLanguage('en');
    const w: SnmpWalk = {
      ...base,
      status: 'partial',
      total: 12,
      outcomes: [
        { subtree: '1.3.6.1.2.1.1', status: 'ok', count: 9, error_code: null, error: null },
        {
          subtree: '1.3.6.1.4.1.25506',
          status: 'timeout',
          count: 3,
          error_code: 'timeout',
          error: 'no complete walk within 45 s',
        },
      ],
      error_code: 'timeout',
      error: 'no complete walk within 45 s',
    };
    render(<WalkResult walk={w} />);
    expect(screen.getByTestId('walk-status').dataset.status).toBe('partial');
    expect(screen.getByText('time budget ran out (partial)')).toBeTruthy();
    expect(screen.getByTestId('walk-error').textContent).toMatch(/too slowly/);
  });

  it('a walk stored before maint-1.4 (no outcomes) still lists the counts', async () => {
    await i18n.changeLanguage('en');
    const old: SnmpWalk = { ...base };
    delete old.outcomes;
    delete old.error_code;
    render(
      <WalkResult
        walk={{ ...old, status: 'ok', counts: { '1.3.6.1.2.1.1': 4 }, total: 4, error: null }}
      />,
    );
    expect(screen.getByTestId('walk-subtrees').textContent).toContain('1.3.6.1.2.1.1: 4');
    expect(screen.queryByTestId('walk-error')).toBeNull();
  });
});
