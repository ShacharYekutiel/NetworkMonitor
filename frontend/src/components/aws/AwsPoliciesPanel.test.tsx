import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '../../i18n';
import { Wrap } from '../../test/v13';
import { BASE_ROLE, BEDROCK_ROLE, mockAwsFetch, TA } from '../../test/aws';
import { AwsPoliciesPanel } from './AwsPoliciesPanel';

afterEach(() => vi.unstubAllGlobals());

const policies = {
  base_trust_policy: {
    Version: '2012-10-17',
    Statement: [
      {
        Effect: 'Allow',
        Principal: { Service: 'rolesanywhere.amazonaws.com' },
        Action: ['sts:AssumeRole', 'sts:TagSession', 'sts:SetSourceIdentity'],
        Condition: {
          ArnEquals: { 'aws:SourceArn': TA },
          StringEquals: { 'aws:PrincipalTag/x509Subject/CN': 'netmonitor.example.net' },
        },
      },
    ],
  },
  hop_caller_policy: {
    Version: '2012-10-17',
    Statement: [{ Effect: 'Allow', Action: ['sts:AssumeRole'], Resource: BEDROCK_ROLE }],
  },
  hop_trust_policy: {
    Version: '2012-10-17',
    Statement: [{ Effect: 'Allow', Principal: { AWS: BASE_ROLE }, Action: ['sts:AssumeRole'] }],
  },
  notes: ['hop_trust_policy: no ExternalId (recommended against the confused deputy)'],
};

describe('[4] AwsPoliciesPanel', () => {
  it('fetched when opened (with the hop), rendered with copy buttons and translated notes', async () => {
    const calls = mockAwsFetch((m, url) =>
      m === 'GET' && url.startsWith('/api/aws/connections/3/policies')
        ? { status: 200, body: policies }
        : undefined,
    );
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
    render(
      <Wrap>
        <AwsPoliciesPanel connectionId={3} roleArn={BEDROCK_ROLE} externalId="Ext123" />
      </Wrap>,
    );
    expect(calls).toHaveLength(0); // collapsed: nothing fetched
    fireEvent.click(screen.getByText('Expected IAM policies'));
    await screen.findByText('Trust policy of the Roles Anywhere role');
    const url = new URL(calls[0].url, 'http://x');
    expect(url.searchParams.get('role_arn')).toBe(BEDROCK_ROLE);
    expect(url.searchParams.get('external_id')).toBe('Ext123');
    const base = document.querySelector('[data-policy="base_trust_policy"] pre')!;
    // the trust anchor ARN appears exactly once
    expect(base.textContent!.split('arn:aws:rolesanywhere').length - 1).toBe(1);
    expect(base.getAttribute('dir')).toBe('ltr');
    expect(document.querySelector('[data-policy="hop_trust_policy"]')).toBeTruthy();
    expect(document.querySelector('[data-note="noExternalId"]')!.textContent).toMatch(
      /confused-deputy/,
    );
    fireEvent.click(screen.getByTestId('copy-base_trust_policy'));
    await waitFor(() => expect(writeText).toHaveBeenCalled());
    expect(JSON.parse(String((writeText.mock.calls[0] as unknown[])[0]))).toEqual(
      policies.base_trust_policy,
    );
  });
});
