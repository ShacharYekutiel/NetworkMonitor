import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import '../../i18n';
import { Wrap } from '../../test/v13';
import { mkConn, mockAwsFetch, okChain, raFailure, type Route } from '../../test/aws';
import { AwsConnectionsCard } from './AwsConnectionsCard';

afterEach(() => vi.unstubAllGlobals());

const assistantUses = [
  { kind: 'assistant' as const, id: null, name: 'Assistant', access_mode: 'direct' as const },
];
const list = [
  mkConn(1, 'Default', {
    is_default: true,
    used_by: assistantUses,
    chains: [{ consumer: 'assistant', hops: [], state: 'ok', error_code: null }],
    last_test: {
      ok: true,
      at: '2026-11-12T10:00:00Z',
      by: 'admin',
      error_code: null,
      account: '111111111111',
      arn: 'arn:aws:sts::111111111111:assumed-role/netmon-bedrock/1234',
    },
    warnings: ['cert_expiring'],
  }),
  mkConn(2, 'Network account', { auth_mode: 'access_key' }),
];
const asstSettings = (connectionId: number | null) => ({
  connection_id: connectionId,
  access: { mode: 'direct', role_arn: null, external_id: null },
});

function routes(extra: Route, assistantConnection: number | null = null): Route {
  return (m, url, body) => {
    const r = extra(m, url, body);
    if (r) return r;
    if (m === 'GET' && url === '/api/aws/connections') return { status: 200, body: list };
    if (m === 'GET' && url === '/api/assistant/settings')
      return { status: 200, body: asstSettings(assistantConnection) };
    return undefined;
  };
}

async function rowOf(name: string): Promise<HTMLElement> {
  let row: HTMLElement | null = null;
  await waitFor(() => {
    row = document.querySelector<HTMLElement>(`tr[data-connection="${name}"]`);
    expect(row).toBeTruthy();
  });
  return row!;
}

describe('[4] AwsConnectionsCard', () => {
  it('lists the connections: default badge, identity of the last test, used by, warnings', async () => {
    mockAwsFetch(routes(() => undefined));
    render(
      <Wrap>
        <AwsConnectionsCard assistantOn />
      </Wrap>,
    );
    const row = await rowOf('Default');
    expect(row.getAttribute('data-default')).toBe('true');
    expect(within(row).getByTestId('default-badge').textContent).toBe('Default');
    expect(row.textContent).toContain('111111111111');
    expect(row.textContent).toContain('Assistant · directly');
    expect(row.textContent).toContain('(working)');
    expect(row.querySelector('[data-warning="cert_expiring"]')).toBeTruthy();
    const other = document.querySelector<HTMLElement>('tr[data-connection="Network account"]')!;
    expect(within(other).queryByTestId('default-badge')).toBeNull();
    expect(within(other).getByTestId('last-test').textContent).toBe('never tested');
  });

  it('Delete is disabled with the reason while used, or while the default with others', async () => {
    mockAwsFetch(routes(() => undefined));
    render(
      <Wrap>
        <AwsConnectionsCard assistantOn={false} />
      </Wrap>,
    );
    const row = await rowOf('Default');
    const del = within(row).getByTestId('aws-delete') as HTMLButtonElement;
    expect(del.disabled).toBe(true);
    expect(within(row).getByTestId('delete-reason').textContent).toMatch(/In use by Assistant/);
    const other = document.querySelector<HTMLElement>('tr[data-connection="Network account"]')!;
    expect((within(other).getByTestId('aws-delete') as HTMLButtonElement).disabled).toBe(false);
  });

  it('Add opens the editor inline under the table', async () => {
    mockAwsFetch(routes(() => undefined));
    render(
      <Wrap>
        <AwsConnectionsCard assistantOn={false} />
      </Wrap>,
    );
    await rowOf('Default');
    fireEvent.click(screen.getByTestId('aws-add'));
    expect(screen.getByTestId('aws-connection-form')).toBeTruthy();
    expect(screen.getByText('New AWS connection')).toBeTruthy();
  });

  it('Set as default without an implicit ON consumer: no Test, no body', async () => {
    const calls = mockAwsFetch(
      routes((m, url) =>
        m === 'POST' && url === '/api/aws/connections/2/default'
          ? { status: 200, body: list }
          : undefined,
      ),
    );
    render(
      <Wrap>
        <AwsConnectionsCard assistantOn={false} />
      </Wrap>,
    );
    await rowOf('Default');
    fireEvent.click(screen.getByTestId('aws-set-default'));
    expect(screen.getByText(/Everything that uses/)).toBeTruthy();
    fireEvent.click(screen.getByTestId('set-default-confirm'));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('/2/default'))).toBe(true));
    expect(calls.some((c) => c.url === '/api/aws/connections/test')).toBe(false);
    expect(calls.find((c) => c.url.endsWith('/2/default'))!.body).toBeUndefined();
  });

  it('Set as default while the ON assistant uses the default implicitly: Test first, then test_id', async () => {
    let answer = raFailure;
    const calls = mockAwsFetch(
      routes((m, url) => {
        if (m === 'POST' && url === '/api/aws/connections/test')
          return { status: 200, body: answer };
        if (m === 'POST' && url === '/api/aws/connections/2/default')
          return { status: 200, body: list };
        return undefined;
      }, null),
    );
    render(
      <Wrap>
        <AwsConnectionsCard assistantOn />
      </Wrap>,
    );
    await rowOf('Default');
    await waitFor(() => expect(calls.some((c) => c.url === '/api/assistant/settings')).toBe(true));
    fireEvent.click(screen.getByTestId('aws-set-default'));
    expect(screen.getByText(/Test the connection first/)).toBeTruthy();
    fireEvent.click(screen.getByTestId('set-default-confirm'));
    // a failing Test: the steps with hints, the default is NOT changed
    const res = await screen.findByTestId('aws-test-result');
    expect(res.getAttribute('data-ok')).toBe('false');
    expect(calls.some((c) => c.url.endsWith('/default'))).toBe(false);
    answer = okChain;
    fireEvent.click(screen.getByTestId('set-default-confirm'));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('/2/default'))).toBe(true));
    expect(calls.find((c) => c.url.endsWith('/2/default'))!.body).toEqual({ test_id: 't-ok' });
    expect(calls.filter((c) => c.url === '/api/aws/connections/test')[0].body).toEqual({
      connection_id: 2,
      connection: null,
      access: null,
    });
  });
});
