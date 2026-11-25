import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { ToastProvider } from '../toast';
import { SyslogProtectionCard } from './IngestSettings';
import type { Settings, SyslogFilter, TopTalker } from '../../api/types';
import {
  emptyFilterForm,
  filterFormErrors,
  filterInput,
  formFromFilter,
  formFromTalker,
  hasErrors,
} from '../../lib/syslogFilters';

const base = {
  syslog_dedup_enabled: true,
  syslog_dedup_window_s: 60,
  syslog_rate_limit_enabled: true,
  syslog_rate_limit_per_min: 3000,
} as unknown as Settings;

afterEach(() => vi.unstubAllGlobals());

describe('[syslog-protection] settings card', () => {
  it('shows the defaults, validates and saves the four keys', async () => {
    const f = vi.fn(
      async () =>
        new Response(JSON.stringify(base), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', f);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <ToastProvider>
            <SyslogProtectionCard settings={base} />
          </ToastProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    );
    const win = screen.getByTestId('dedup-window') as HTMLInputElement;
    const rate = screen.getByTestId('rate-per-min') as HTMLInputElement;
    expect(win.value).toBe('60');
    expect(rate.value).toBe('3000');
    const boxes = screen.getAllByRole('checkbox') as HTMLInputElement[];
    expect(boxes.map((b) => b.checked)).toEqual([true, true]);
    const save = screen.getByRole('button', { name: /save|שמירה/i }) as HTMLButtonElement;
    fireEvent.change(win, { target: { value: '2' } });
    expect(save.disabled).toBe(true);
    fireEvent.change(win, { target: { value: '120' } });
    fireEvent.click(boxes[1]); // rate limit off
    expect(rate.disabled).toBe(true);
    fireEvent.click(save);
    await waitFor(() => expect(f).toHaveBeenCalled());
    const [, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({
      syslog_dedup_enabled: true,
      syslog_dedup_window_s: 120,
      syslog_rate_limit_enabled: false,
      syslog_rate_limit_per_min: 3000,
    });
  });
});

describe('[syslog-protection] filter rule form', () => {
  it('needs a name and a condition; validates sources and keep N', () => {
    const e = filterFormErrors(emptyFilterForm());
    expect(e.name && e.noCondition).toBe(true);
    const f = {
      ...emptyFilterForm(),
      name: 'x',
      sources: '10.0.0.0/8\nnope',
      action: 'keep' as const,
      keepN: '0',
    };
    const e2 = filterFormErrors(f);
    expect(e2.sources).toEqual(['nope']);
    expect(e2.keepN).toBe(true);
    expect(e2.noCondition).toBeUndefined();
    expect(hasErrors(filterFormErrors({ ...f, sources: '10.0.0.0/8', keepN: '50' }))).toBe(false);
  });

  it('builds the request body and round-trips a stored rule', () => {
    const body = filterInput({
      ...emptyFilterForm(),
      name: ' SNMP ',
      deviceId: 4,
      program: 'SNMP',
      severityMin: 6,
    });
    expect(body).toEqual({
      name: 'SNMP',
      enabled: true,
      sources: [],
      device_ids: [4],
      program: 'SNMP',
      program_match: 'exact',
      msgid: '',
      severity_min: 6,
      message_contains: '',
      action: 'drop',
      keep_n: undefined,
    });
    const stored = {
      ...body,
      id: 1,
      keep_n: 100,
      devices: [{ id: 4, name: 'sw' }],
    } as unknown as SyslogFilter;
    expect(filterInput(formFromFilter(stored))).toEqual(body);
  });

  it('pre-fills a rule from a top talker (device, else its address)', () => {
    const talker: TopTalker = {
      source_ip: '10.0.0.9',
      device_id: null,
      device_name: null,
      program: 'SNMP',
      severity: 6,
      rows: 84000,
      messages: 84000,
      last_seen: null,
    };
    const f = formFromTalker(talker, 'SNMP info');
    expect([f.name, f.sources, f.deviceId, f.program, f.severityMin]).toEqual([
      'SNMP info',
      '10.0.0.9',
      null,
      'SNMP',
      6,
    ]);
    expect(formFromTalker({ ...talker, device_id: 3 }, 'x').sources).toBe('');
  });
});
