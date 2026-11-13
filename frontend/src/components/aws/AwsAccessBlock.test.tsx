import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import '../../i18n';
import type { AwsConnection } from '../../api/types';
import { accessErrors, normalizeAccess } from '../../lib/aws';
import { Wrap } from '../../test/v13';
import { BEDROCK_ROLE, mkConn, mockAwsFetch } from '../../test/aws';
import { AwsAccessBlock, type AccessValue } from './AwsAccessBlock';

afterEach(() => vi.unstubAllGlobals());

function Harness({ connections }: { connections: AwsConnection[] }) {
  const [v, setV] = useState<AccessValue>({
    connection_id: '',
    mode: 'direct',
    role_arn: '',
    external_id: '',
  });
  const sel = v.connection_id
    ? connections.find((c) => String(c.id) === v.connection_id)
    : connections.find((c) => c.is_default);
  const errors = accessErrors(
    normalizeAccess({ mode: v.mode, role_arn: v.role_arn, external_id: v.external_id }),
    sel?.auth_mode ?? null,
    sel?.ra_profile_arn,
  );
  return (
    <>
      <AwsAccessBlock value={v} connections={connections} errors={errors} onChange={setV} />
      <output data-testid="state">{JSON.stringify(v)}</output>
    </>
  );
}

const conns = [
  mkConn(1, 'Default', { is_default: true }),
  mkConn(2, 'Keys', { auth_mode: 'access_key' }),
];

describe('[4] AwsAccessBlock', () => {
  it('the default is marked; ra_role is offered only for a Roles Anywhere connection', () => {
    mockAwsFetch(() => undefined);
    render(
      <Wrap>
        <Harness connections={conns} />
      </Wrap>,
    );
    const sel = screen.getByTestId('access-connection') as HTMLSelectElement;
    expect([...sel.options].map((o) => o.textContent)).toEqual([
      'Default connection (Default)',
      'Default (Default)',
      'Keys',
    ]);
    expect(screen.getByTestId('access-ra_role')).toBeTruthy();
    fireEvent.click(screen.getByTestId('access-ra_role'));
    // switching to an access-key connection drops ra_role back to direct
    fireEvent.change(sel, { target: { value: '2' } });
    expect(screen.queryByTestId('access-ra_role')).toBeNull();
    expect(JSON.parse(screen.getByTestId('state').textContent!).mode).toBe('direct');
    expect(screen.getByTestId('manage-connections').getAttribute('href')).toBe('/settings?tab=aws');
  });

  it('Assume a role: role ARN pattern, doubled ARN, ExternalId Generate', async () => {
    mockAwsFetch((m, url) =>
      m === 'POST' && url === '/api/aws/external-id'
        ? { status: 200, body: { external_id: 'Z'.repeat(32) } }
        : undefined,
    );
    render(
      <Wrap>
        <Harness connections={conns} />
      </Wrap>,
    );
    fireEvent.click(screen.getByTestId('access-assume_role'));
    const role = screen.getByTestId('access-role-arn');
    fireEvent.change(role, { target: { value: 'netmon-bedrock' } });
    expect(screen.getByText('Not a valid ARN for this field')).toBeTruthy();
    fireEvent.change(role, { target: { value: `${BEDROCK_ROLE}${BEDROCK_ROLE}` } });
    expect(screen.getByText('The ARN appears twice — paste it once.')).toBeTruthy();
    fireEvent.change(role, { target: { value: BEDROCK_ROLE } });
    expect(screen.queryByText(/ARN appears twice|Not a valid ARN/)).toBeNull();
    fireEvent.click(screen.getByTestId('external-id-generate'));
    await waitFor(() =>
      expect(JSON.parse(screen.getByTestId('state').textContent!).external_id).toBe('Z'.repeat(32)),
    );
  });

  it('ra_role: the role must be in the profile account', () => {
    mockAwsFetch(() => undefined);
    render(
      <Wrap>
        <Harness connections={conns} />
      </Wrap>,
    );
    fireEvent.click(screen.getByTestId('access-ra_role'));
    expect(screen.queryByTestId('external-id')).toBeNull();
    fireEvent.change(screen.getByTestId('access-role-arn'), { target: { value: BEDROCK_ROLE } });
    expect(screen.getByText("The role must be in the profile's account")).toBeTruthy();
  });

  it('no connection yet: says where to add one', () => {
    mockAwsFetch(() => undefined);
    render(
      <Wrap>
        <Harness connections={[]} />
      </Wrap>,
    );
    expect(screen.getByTestId('no-connection').textContent).toMatch(/Settings → AWS/);
  });
});
