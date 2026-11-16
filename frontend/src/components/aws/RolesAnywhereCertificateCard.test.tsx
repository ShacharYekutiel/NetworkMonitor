import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '../../i18n';
import { Wrap } from '../../test/v13';
import { mkConn, mockAwsFetch } from '../../test/aws';
import { RolesAnywhereCertificateCard } from './RolesAnywhereCertificateCard';

afterEach(() => vi.unstubAllGlobals());

const pending = {
  subject: 'CN=netmonitor.example.net',
  created_at: '2026-11-10T08:00:00Z',
  pem: '-----BEGIN CERTIFICATE REQUEST-----\nMIIB\n-----END CERTIFICATE REQUEST-----',
  key_unreadable: false,
};

function file(name: string, text = '-----BEGIN CERTIFICATE-----\nMII\n') {
  return new File([text], name, { type: 'application/octet-stream' });
}

describe('[4] RolesAnywhereCertificateCard', () => {
  it('shows subject CN, issuer CN and the source identity prominently (owner mistake 2)', () => {
    mockAwsFetch(() => undefined);
    render(
      <Wrap>
        <RolesAnywhereCertificateCard c={mkConn(1, 'Default')} connections={[]} locked={false} />
      </Wrap>,
    );
    expect(screen.getByTestId('cert-cn').textContent).toBe('netmonitor.example.net');
    expect(screen.getByTestId('cert-issuer-cn').textContent).toBe('Example Issuing CA');
    expect(screen.getByTestId('cert-source-identity').textContent).toBe(
      'CN=netmonitor.example.net',
    );
    expect(screen.getByTestId('cert-cn').getAttribute('dir')).toBe('ltr');
  });

  it('CSR pending: one file field, no key or password, upload enabled with the file alone (mistake 4)', async () => {
    const calls = mockAwsFetch((m, url) => {
      if (m === 'POST' && url === '/api/aws/connections/1/cert')
        return { status: 200, body: mkConn(1, 'Default').certificate };
      return { status: 200, body: [] };
    });
    render(
      <Wrap>
        <RolesAnywhereCertificateCard
          c={mkConn(1, 'Default', { certificate: null, csr: pending })}
          connections={[]}
          locked={false}
        />
      </Wrap>,
    );
    expect(screen.getByTestId('cert-card').getAttribute('data-mode')).toBe('csr-pending');
    expect(screen.queryByTestId('cert-key')).toBeNull();
    expect(screen.queryByTestId('cert-password')).toBeNull();
    const input = screen.getByTestId('cert-file') as HTMLInputElement;
    expect(input.accept).toBe('.cer,.crt,.pem,.der,.p7b,.p7c');
    expect(screen.getByText(/The private key stays on the server/)).toBeTruthy();
    const btn = screen.getByTestId('cert-upload') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    fireEvent.change(input, { target: { files: [file('netmonitor.p7b')] } });
    await waitFor(() => expect(btn.disabled).toBe(false));
    fireEvent.click(btn);
    await waitFor(() =>
      expect(calls.some((c) => c.url === '/api/aws/connections/1/cert')).toBe(true),
    );
    const fd = calls.find((c) => c.url === '/api/aws/connections/1/cert')!.body as FormData;
    expect([...fd.keys()]).toEqual(['file']);
  });

  it('CSR pending: a private key is refused in words; "own key" reveals the key fields', async () => {
    mockAwsFetch(() => ({ status: 200, body: [] }));
    render(
      <Wrap>
        <RolesAnywhereCertificateCard
          c={mkConn(1, 'Default', { certificate: null, csr: pending })}
          connections={[]}
          locked={false}
        />
      </Wrap>,
    );
    const input = screen.getByTestId('cert-file');
    fireEvent.change(input, { target: { files: [file('netmonitor.key')] } });
    expect(await screen.findByTestId('not-a-key')).toBeTruthy();
    expect((screen.getByTestId('cert-upload') as HTMLButtonElement).disabled).toBe(true);
    // a key saved with a certificate extension is recognised by its content
    fireEvent.change(input, {
      target: { files: [file('netmonitor.pem', '-----BEGIN PRIVATE KEY-----\nMIIE')] },
    });
    await waitFor(() => expect(screen.queryByTestId('not-a-key')).toBeTruthy());
    fireEvent.click(screen.getByTestId('own-key'));
    expect(screen.getByTestId('cert-key')).toBeTruthy();
    expect(screen.getByTestId('cert-password')).toBeTruthy();
    expect(screen.getByTestId('cert-card').getAttribute('data-mode')).not.toBe('csr-pending');
  });

  it('key_mismatch names the pending request and the certificate', async () => {
    mockAwsFetch((m, url) => {
      if (m === 'POST' && url.endsWith('/cert'))
        return {
          status: 422,
          body: {
            error: {
              code: 'key_mismatch',
              message: 'The key does not match',
              details: [
                {
                  csr_subject: 'CN=netmonitor.example.net',
                  csr_created_at: '2026-11-10T08:00:00Z',
                  cert_subject: 'CN=other-host',
                },
              ],
            },
          },
        };
      return { status: 200, body: [] };
    });
    render(
      <Wrap>
        <RolesAnywhereCertificateCard
          c={mkConn(1, 'Default', { certificate: null, csr: pending })}
          connections={[]}
          locked={false}
        />
      </Wrap>,
    );
    fireEvent.change(screen.getByTestId('cert-file'), {
      target: { files: [file('issued.cer')] },
    });
    await waitFor(() =>
      expect((screen.getByTestId('cert-upload') as HTMLButtonElement).disabled).toBe(false),
    );
    fireEvent.click(screen.getByTestId('cert-upload'));
    const err = await screen.findByTestId('cert-error');
    expect(err.getAttribute('data-code')).toBe('key_mismatch');
    expect(err.textContent).toMatch(/not issued for the pending request/);
    expect(err.textContent).toContain('CN=netmonitor.example.net');
    expect(err.textContent).toContain('CN=other-host');
  });

  it('certreq command with the template NAME; a display name with spaces is flagged (mistake 3)', () => {
    mockAwsFetch(() => undefined);
    render(
      <Wrap>
        <RolesAnywhereCertificateCard
          c={mkConn(1, 'Default', { certificate: null, csr: pending })}
          connections={[]}
          locked={false}
        />
      </Wrap>,
    );
    const cmd = screen.getByTestId('certreq-command');
    expect(cmd.textContent).toContain('CertificateTemplate:<TemplateName>');
    fireEvent.change(screen.getByTestId('template-name'), {
      target: { value: 'NetMonClientAuth' },
    });
    expect(cmd.textContent).toBe(
      'certreq -submit -attrib "CertificateTemplate:NetMonClientAuth" netmonitor.csr netmonitor.cer',
    );
    expect(screen.queryByTestId('template-spaces')).toBeNull();
    expect(screen.getByText(/0x80094800/)).toBeTruthy();
    fireEvent.change(screen.getByTestId('template-name'), {
      target: { value: 'NetMon Client Auth' },
    });
    expect(screen.getByTestId('template-spaces')).toBeTruthy();
  });

  it('generate a CSR: a CN with a space is warned about; copy from another connection', async () => {
    const calls = mockAwsFetch((m, url) => {
      if (m === 'POST' && url.endsWith('/cert/csr')) return { status: 200, body: pending };
      if (m === 'POST' && url.endsWith('/cert/copy')) return { status: 200, body: mkConn(2, 'x') };
      return { status: 200, body: [] };
    });
    render(
      <Wrap>
        <RolesAnywhereCertificateCard
          c={mkConn(2, 'Network account', { certificate: null })}
          connections={[mkConn(1, 'Default'), mkConn(2, 'Network account', { certificate: null })]}
          locked={false}
        />
      </Wrap>,
    );
    fireEvent.click(screen.getByLabelText('Generate a CSR'));
    fireEvent.change(screen.getByTestId('csr-cn'), { target: { value: 'NetMonitor Bedrock' } });
    expect(screen.getByTestId('cn-unsafe')).toBeTruthy();
    fireEvent.change(screen.getByTestId('csr-cn'), { target: { value: 'netmonitor.example.net' } });
    expect(screen.queryByTestId('cn-unsafe')).toBeNull();
    fireEvent.click(screen.getByTestId('csr-generate'));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('/cert/csr'))).toBe(true));
    expect(calls.find((c) => c.url.endsWith('/cert/csr'))!.body).toEqual({
      common_name: 'netmonitor.example.net',
      key_type: 'rsa',
    });
    // copy: only connections with an uploaded certificate are offered
    const sel = screen.getByTestId('cert-copy').querySelector('select')!;
    expect([...sel.options].map((o) => o.textContent)).toEqual(['Choose a connection…', 'Default']);
    fireEvent.change(sel, { target: { value: '1' } });
    fireEvent.click(screen.getByText('Copy'));
    await waitFor(() =>
      expect(calls.find((c) => c.url.endsWith('/cert/copy'))?.body).toEqual({
        from_connection_id: 1,
      }),
    );
  });
});
