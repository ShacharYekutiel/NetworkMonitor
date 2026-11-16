import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '../../i18n';
import { Wrap } from '../../test/v13';
import {
  BASE_ROLE,
  BEDROCK_ROLE,
  mkConn,
  mockAwsFetch,
  okChain,
  PROFILE,
  raFailure,
  TA,
} from '../../test/aws';
import { AwsConnectionForm } from './AwsConnectionForm';

afterEach(() => vi.unstubAllGlobals());

function form(conn = mkConn(1, 'Default'), assistantOn = false) {
  return render(
    <Wrap>
      <AwsConnectionForm
        connection={conn}
        connections={[conn]}
        assistantOn={assistantOn}
        onClose={() => undefined}
        onCreated={() => undefined}
      />
    </Wrap>,
  );
}

describe('[4] AwsConnectionForm', () => {
  it('fields per authentication mode', () => {
    mockAwsFetch(() => undefined);
    form(mkConn(1, 'Default'));
    expect((screen.getByTestId('aws-ra_trust_anchor_arn') as HTMLInputElement).value).toBe(TA);
    expect(screen.getByTestId('cert-card')).toBeTruthy();
    expect(screen.getByTestId('aws-policies')).toBeTruthy();
    fireEvent.click(screen.getByTestId('aws-auth-access_key'));
    expect(screen.queryByTestId('aws-ra_trust_anchor_arn')).toBeNull();
    expect(screen.getByTestId('aws-access_key_id')).toBeTruthy();
    expect(screen.getByText('Secret access key')).toBeTruthy();
    expect(screen.getByText('Session token')).toBeTruthy();
    fireEvent.click(screen.getByTestId('aws-auth-default_chain'));
    expect(screen.getByText(/IMDSv2/)).toBeTruthy();
    // a new connection: the certificate comes after the first save
    vi.unstubAllGlobals();
  });

  it('a new Roles Anywhere connection explains that the certificate follows the save', () => {
    mockAwsFetch(() => undefined);
    render(
      <Wrap>
        <AwsConnectionForm
          connection={null}
          connections={[]}
          assistantOn={false}
          onClose={() => undefined}
          onCreated={() => undefined}
        />
      </Wrap>,
    );
    expect(screen.getByTestId('save-first')).toBeTruthy();
    expect(screen.queryByTestId('cert-card')).toBeNull();
  });

  it('a doubled ARN is refused with "the ARN appears twice" and Save stays disabled (mistake 1)', () => {
    mockAwsFetch(() => undefined);
    form();
    fireEvent.change(screen.getByTestId('aws-ra_trust_anchor_arn'), {
      target: { value: `arn:aws:rolesanywhere:us-east-1:111111111111:trust-anchor/${TA}` },
    });
    expect(screen.getByText('The ARN appears twice — paste it once.')).toBeTruthy();
    expect((screen.getByTestId('aws-save') as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId('aws-test') as HTMLButtonElement).disabled).toBe(true);
  });

  it('endpoint override rows are validated', () => {
    mockAwsFetch(() => undefined);
    form();
    fireEvent.click(screen.getByTestId('add-override'));
    const row = screen.getByTestId('aws-overrides').querySelector('[data-override="0"]')!;
    const url = row.querySelectorAll('input')[1];
    fireEvent.change(url, { target: { value: 'http://vpce.example' } });
    expect(row.querySelector('[data-error="url"]')).toBeTruthy();
    fireEvent.change(url, { target: { value: 'https://vpce-1.sts.us-east-1.vpce.amazonaws.com' } });
    expect(row.querySelector('[data-error]')).toBeNull();
    fireEvent.click(screen.getByTestId('add-override'));
    const row2 = screen.getByTestId('aws-overrides').querySelector('[data-override="1"]')!;
    fireEvent.change(row2.querySelectorAll('input')[1], {
      target: { value: 'https://vpce-2.sts.us-east-1.vpce.amazonaws.com' },
    });
    expect(row2.querySelector('[data-error="overrideDup"]')).toBeTruthy();
  });

  it('Test: the failing step is highlighted with its hints and the exact values', async () => {
    const calls = mockAwsFetch((m, url) =>
      m === 'POST' && url === '/api/aws/connections/test'
        ? { status: 200, body: raFailure }
        : { status: 200, body: [] },
    );
    form();
    fireEvent.click(screen.getByTestId('aws-test'));
    const res = await screen.findByTestId('aws-test-result');
    expect(res.getAttribute('data-ok')).toBe('false');
    expect(calls.find((c) => c.url === '/api/aws/connections/test')!.body).toMatchObject({
      connection_id: 1,
      connection: { ra_trust_anchor_arn: TA, ra_profile_arn: PROFILE, ra_role_arn: BASE_ROLE },
      access: null,
    });
    const cred = res.querySelector('[data-step="credentials"]')!;
    expect(cred.getAttribute('data-ok')).toBe('false');
    expect(cred.getAttribute('data-hop')).toBe('0');
    const codes = [...cred.querySelectorAll('[data-hint]')].map((h) => h.getAttribute('data-hint'));
    expect(codes).toEqual(['ra_trust_policy', 'ra_subject_cn', 'ra_issuer_cn', 'ra_profile_role']);
    const trust = cred.querySelector('[data-hint="ra_trust_policy"]')!;
    expect(trust.textContent).toMatch(/exactly/);
    expect(trust.querySelector('[data-value="trust_anchor_arn"]')!.textContent).toBe(TA);
    expect(cred.querySelector('[data-value="cert_cn"]')!.textContent).toBe(
      'netmonitor.example.net',
    );
  });

  it('Test with a role hop sends the access; the hop step is labelled', async () => {
    const hopOk = {
      ...okChain,
      steps: [
        ...okChain.steps,
        {
          step: 'hop',
          hop: 1,
          ok: true,
          detail: `role ${BEDROCK_ROLE}`,
          ms: 9,
          error_code: null,
          hints: [],
        },
        {
          step: 'hop_identity',
          hop: 1,
          ok: true,
          detail: 'account 222222222222',
          ms: 5,
          error_code: null,
          hints: [],
        },
      ],
    };
    const calls = mockAwsFetch((m, url) =>
      m === 'POST' && url.startsWith('/api/aws/external-id')
        ? { status: 200, body: { external_id: 'X'.repeat(32) } }
        : m === 'POST' && url === '/api/aws/connections/test'
          ? { status: 200, body: hopOk }
          : { status: 200, body: [] },
    );
    form();
    fireEvent.click(screen.getByTestId('hop-assume_role'));
    fireEvent.change(screen.getByTestId('hop-role-arn'), { target: { value: BEDROCK_ROLE } });
    fireEvent.click(screen.getByTestId('external-id-generate'));
    await waitFor(() =>
      expect((screen.getByTestId('external-id') as HTMLInputElement).value).toBe('X'.repeat(32)),
    );
    fireEvent.click(screen.getByTestId('aws-test'));
    const res = await screen.findByTestId('aws-test-result');
    expect(calls.find((c) => c.url === '/api/aws/connections/test')!.body).toMatchObject({
      access: { mode: 'assume_role', role_arn: BEDROCK_ROLE, external_id: 'X'.repeat(32) },
    });
    expect(res.querySelector('[data-step="hop"]')!.textContent).toMatch(/Role hop.*hop/);
    expect(res.textContent).toMatch(/The connection works/);
  });

  it('used by the ON assistant: a credential change needs a successful Test, then PATCH sends test_id', async () => {
    const used = mkConn(1, 'Default', {
      used_by: [{ kind: 'assistant', id: null, name: 'Assistant', access_mode: 'direct' }],
    });
    const calls = mockAwsFetch((m, url) => {
      if (m === 'POST' && url === '/api/aws/connections/test')
        return { status: 200, body: okChain };
      if (m === 'PATCH') return { status: 200, body: used };
      return { status: 200, body: [] };
    });
    form(used, true);
    // the name is not a credential: saved without a test
    fireEvent.change(screen.getByTestId('aws-name'), { target: { value: 'Main' } });
    expect(screen.queryByTestId('needs-test')).toBeNull();
    fireEvent.change(screen.getByTestId('aws-region'), { target: { value: 'eu-central-1' } });
    expect(screen.getByTestId('needs-test')).toBeTruthy();
    expect((screen.getByTestId('aws-save') as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByTestId('aws-test'));
    await waitFor(() => expect(screen.queryByTestId('needs-test')).toBeNull());
    fireEvent.click(screen.getByTestId('aws-save'));
    await waitFor(() => expect(calls.some((c) => c.method === 'PATCH')).toBe(true));
    expect(calls.find((c) => c.method === 'PATCH')!.body).toMatchObject({
      name: 'Main',
      region: 'eu-central-1',
      test_id: 't-ok',
    });
  });

  it('shows connection warnings, the unresolved hosts in words', () => {
    mockAwsFetch(() => undefined);
    form(
      mkConn(1, 'Default', {
        warnings: ['cert_expiring', 'endpoint_unresolved'],
        unresolved_hosts: ['vpce-1.sts.us-east-1.vpce.amazonaws.com'],
      }),
    );
    expect(document.querySelector('[data-warning="cert_expiring"]')!.textContent).toMatch(
      /30 days/,
    );
    expect(document.querySelector('[data-warning="endpoint_unresolved"]')!.textContent).toMatch(
      /vpce-1\.sts\.us-east-1\.vpce\.amazonaws\.com\. Saved anyway/,
    );
  });
});
