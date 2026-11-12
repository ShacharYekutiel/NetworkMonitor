// [1.3.2] a named-map status message is shown in the viewer's language, not the server's
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import i18n from '../i18n';
import { useSensorTypes } from '../hooks/queries';
import { setNamedValueMaps } from '../lib/valueMaps';
import { AuthProvider } from '../hooks/auth';
import { ToastProvider } from '../components/toast';
import { authRoutes, mkSensor, mockFetch } from '../test/v13';
import { SensorPage } from './SensorPage';

const VALUE_MAPS = {
  hh3c_error_status: {
    title: { en: 'HPE Comware hardware fault codes', he: 'קודי תקלת חומרה של HPE Comware' },
    codes: [
      {
        value: '51',
        status: 'down',
        mib: 'psuError',
        text: { en: 'Power supply fault', he: 'ספק כוח תקול' },
      },
    ],
    unknown: { en: 'Unknown fault code ({code})', he: 'קוד תקלה לא מוכר ({code})' },
  },
};

const sensor = mkSensor(5, 'PSU 2', {
  type: 'snmp_custom',
  status: 'down',
  status_message: 'PSU 2: ספק כוח תקול (psuError, 51)',
  config: { value_type: 'enum', value_map: {}, value_map_ref: 'hh3c_error_status' },
});

/** As Layout: loads the sensor types (and with them the named value maps) once. */
function Shell() {
  useSensorTypes();
  return <SensorPage />;
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter initialEntries={['/sensors/5']}>
          <AuthProvider>
            <Routes>
              <Route path="/sensors/:id" element={<Shell />} />
            </Routes>
          </AuthProvider>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

describe('[1.3.2] SensorPage status message language', () => {
  beforeEach(async () => {
    setNamedValueMaps({});
    await i18n.changeLanguage('en');
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    setNamedValueMaps({});
  });

  it('an English viewer sees the English text of a Hebrew status_message', async () => {
    let releaseTypes: () => void = () => {};
    const typesGate = new Promise<void>((r) => (releaseTypes = r));
    mockFetch((method, url) => {
      const a = authRoutes(method, url);
      if (a) return a;
      if (url.startsWith('/api/sensors/5/history'))
        return { status: 200, body: { channels: [], points: [], items: [] } };
      if (url.startsWith('/api/sensors/5')) return { status: 200, body: sensor };
      if (url.startsWith('/api/events'))
        return { status: 200, body: { items: [], total: 0, page: 1, page_size: 50 } };
      return undefined;
    });
    const base = globalThis.fetch;
    vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).startsWith('/api/sensor-types')) {
        await typesGate;
        return new Response(JSON.stringify({ items: [], value_maps: VALUE_MAPS }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return base(input, init);
    });

    renderPage();
    // maps not loaded yet: the stored (server-language) message
    expect(await screen.findByText('PSU 2: ספק כוח תקול (psuError, 51)')).toBeInTheDocument();
    releaseTypes();
    // … and the page re-renders in the viewer's language once they arrive
    expect(await screen.findByText('PSU 2: Power supply fault (psuError, 51)')).toBeInTheDocument();
    expect(screen.queryByText(/ספק כוח תקול \(psuError/)).not.toBeInTheDocument();
  });
});
