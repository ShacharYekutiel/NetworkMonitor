import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '../toast';
import { TrapCollapseCard } from './IngestSettings';
import type { Settings } from '../../api/types';

const base = {
  trap_collapse_enabled: false,
  trap_collapse_keep: 10,
  trap_collapse_oids: ['1.3.6.1.4.1.25506.2.38.1.6.3.0.1'],
} as unknown as Settings;

function renderCard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <TrapCollapseCard settings={base} />
      </ToastProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe('[1.2.1] repetitive traps card', () => {
  it('names known OIDs, blocks a bad OID and saves the three keys', async () => {
    const f = vi.fn(
      async () =>
        new Response(JSON.stringify(base), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', f);
    renderCard();
    expect(screen.getByText('hh3cPeriodicalTrap')).toBeTruthy();
    const area = screen.getByTestId('trap-collapse-settings').querySelector('textarea')!;
    fireEvent.change(area, { target: { value: 'not-an-oid' } });
    const save = screen.getByRole('button', { name: /save|שמירה/i }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.change(area, { target: { value: '1.3.6.1.2.1.192.0.1' } });
    fireEvent.click(screen.getByRole('checkbox'));
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    await waitFor(() => expect(f).toHaveBeenCalled());
    const [, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({
      trap_collapse_enabled: true,
      trap_collapse_keep: 10,
      trap_collapse_oids: ['1.3.6.1.2.1.192.0.1'],
    });
  });
});
