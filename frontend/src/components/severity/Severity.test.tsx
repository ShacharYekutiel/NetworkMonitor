// [severity] Alert severity UI: the tag, the mapping card, filters, forms, live cache, feature off.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient } from '@tanstack/react-query';
import i18n from '../../i18n';
import { SeverityTag } from '../kit';
import { SeverityChips, SeverityMappingEditor, SeverityMappingTable } from './Severity';
import { EditSensorModal } from '../SensorForms';
import { EventsPage } from '../../pages/EventsPage';
import { DashboardPage } from '../../pages/Dashboard';
import { authRoutes, mkSensor, mockFetch, Wrap, type Route } from '../../test/v13';
import type { SeverityMapping } from '../../api/severity';
import type { Dashboard, Sensor } from '../../api/types';
import { applySeverityToCache } from '../../lib/liveCache';
import { qk } from '../../api/endpoints';
import {
  automaticSeverity,
  highestSeverity,
  parseSeverities,
  severityCsv,
  severityRank,
  toggleSeverity,
} from '../../lib/severity';

const DEFAULTS: SeverityMapping = {
  down_critical: 'critical',
  down_warning: 'major',
  down_none: 'minor',
  warning_critical: 'warning',
  warning_warning: 'warning',
  warning_none: 'warning',
  info: 'info',
};
const LEVELS = ['critical', 'major', 'minor', 'warning', 'info'];

/** /api/settings/public with alert_severity on/off, the mapping, plus `more`. */
function routes(on: boolean, more?: Route, mapping: SeverityMapping = DEFAULTS): Route {
  return (method, url, body) => {
    const a = authRoutes(method, url);
    if (a) return a;
    if (url.startsWith('/api/settings/public'))
      return { status: 200, body: { default_language: 'en', features: { alert_severity: on } } };
    if (method === 'GET' && url.startsWith('/api/settings/severity-mapping'))
      return { status: 200, body: { mapping, defaults: DEFAULTS, levels: LEVELS } };
    return more?.(method, url, body);
  };
}

beforeEach(async () => {
  await i18n.changeLanguage('en');
});
afterEach(() => vi.unstubAllGlobals());

describe('[severity] helpers', () => {
  it('orders, parses and toggles levels', () => {
    expect(severityRank('critical')).toBe(5);
    expect(severityRank('info')).toBe(1);
    expect(severityRank(null)).toBe(0);
    expect(parseSeverities('minor,bogus,critical,minor')).toEqual(['critical', 'minor']);
    expect(severityCsv(['info', 'critical'])).toBe('critical,info');
    expect(toggleSeverity(['minor'], 'critical')).toEqual(['critical', 'minor']);
    expect(toggleSeverity(['critical', 'minor'], 'critical')).toEqual(['minor']);
    expect(highestSeverity([null, 'warning', 'major', 'info'])).toBe('major');
    expect(automaticSeverity(DEFAULTS, 'down', 'warning')).toBe('major');
    expect(automaticSeverity(null, 'down', 'warning')).toBeNull();
  });
});

describe('[severity] SeverityTag', () => {
  it('always carries its word, the level and an order glyph', () => {
    render(<SeverityTag severity="critical" />);
    const tag = screen.getByTestId('severity-tag');
    expect(tag.textContent).toBe('Critical');
    expect(tag.getAttribute('data-severity')).toBe('critical');
    expect(tag.getAttribute('title')).toBe('Severity: Critical');
    expect(tag.querySelectorAll('rect')).toHaveLength(4);
    expect(tag.className).toContain('bg-severity-critical');
  });

  it('renders nothing without a severity (feature off: the API sends null)', () => {
    const { container } = render(
      <>
        <SeverityTag severity={null} />
        <SeverityTag severity={undefined} />
      </>,
    );
    expect(container.innerHTML).toBe('');
  });

  it('Hebrew word, English in the tooltip; a fixed language for reports', async () => {
    await i18n.changeLanguage('he');
    render(
      <>
        <SeverityTag severity="major" />
        <SeverityTag severity="info" lng="en" testId="t2" />
      </>,
    );
    const tag = screen.getByTestId('severity-tag');
    expect(tag.textContent).toBe('חמור');
    expect(tag.getAttribute('title')).toBe('חומרה: חמור (Major)');
    expect(screen.getByTestId('t2').textContent).toBe('Information');
  });
});

describe('[severity] filter chips', () => {
  it('multi-select in level order', () => {
    const onChange = vi.fn();
    render(<SeverityChips label="Severity" value={['minor']} onChange={onChange} testId="f" />);
    const group = screen.getByTestId('f');
    expect(within(group).getAllByRole('checkbox')).toHaveLength(5);
    fireEvent.click(within(group).getByLabelText('Critical'));
    expect(onChange).toHaveBeenLastCalledWith(['critical', 'minor']);
    fireEvent.click(within(group).getByLabelText('Minor'));
    expect(onChange).toHaveBeenLastCalledWith([]);
  });
});

describe('[severity] mapping card', () => {
  it('one row per key, saves only the changed rows, shows the defaults', async () => {
    const calls = mockFetch(
      routes(true, (method, url, body) =>
        method === 'PUT' && url === '/api/settings/severity-mapping'
          ? {
              status: 200,
              body: {
                mapping: { ...DEFAULTS, ...(body as object) },
                defaults: DEFAULTS,
                levels: LEVELS,
              },
            }
          : undefined,
      ),
    );
    render(
      <Wrap>
        <SeverityMappingEditor />
      </Wrap>,
    );
    const rows = await screen.findAllByTestId('severity-mapping-row');
    expect(rows.map((r) => r.getAttribute('data-key'))).toEqual(Object.keys(DEFAULTS));
    expect(screen.getByTestId('severity-reclassify-note').textContent).toContain(
      're-classifies the alerts that are active now',
    );
    const save = screen.getByRole('button', { name: 'Save' });
    expect((save as HTMLButtonElement).disabled).toBe(true);
    const sel = within(rows[1]).getByRole('combobox', { name: 'Severity: Down · Warning' });
    fireEvent.change(sel, { target: { value: 'critical' } });
    expect(within(rows[1]).getByTestId('severity-changed')).toBeTruthy();
    expect(within(rows[1]).getByTestId('severity-default').textContent).toBe('Major');
    fireEvent.click(save);
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true));
    expect(calls.find((c) => c.method === 'PUT')!.body).toEqual({ down_warning: 'critical' });
  });

  it('reset asks first, then posts reset', async () => {
    const calls = mockFetch(
      routes(
        true,
        (method, url) =>
          method === 'POST' && url === '/api/settings/severity-mapping/reset'
            ? { status: 200, body: { mapping: DEFAULTS, defaults: DEFAULTS, levels: LEVELS } }
            : undefined,
        { ...DEFAULTS, info: 'minor' },
      ),
    );
    render(
      <Wrap>
        <SeverityMappingEditor />
      </Wrap>,
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Reset to defaults' }));
    const dlg = screen.getByRole('dialog');
    expect(calls.some((c) => c.url.endsWith('/reset'))).toBe(false);
    fireEvent.click(within(dlg).getByRole('button', { name: 'Reset to defaults' }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('/reset'))).toBe(true));
  });

  it('read-only: tags instead of selects, no Save', () => {
    render(<SeverityMappingTable value={DEFAULTS} defaults={DEFAULTS} readOnly />);
    expect(screen.queryAllByRole('combobox')).toHaveLength(0);
    expect(screen.getAllByTestId('severity-tag').map((x) => x.textContent)).toEqual([
      'Critical',
      'Major',
      'Minor',
      'Warning',
      'Warning',
      'Warning',
      'Information',
    ]);
  });
});

const PING_TYPES: Route = (_m, url) =>
  url.startsWith('/api/sensor-types')
    ? {
        status: 200,
        body: {
          items: [
            {
              type: 'ping',
              requires_snmp: false,
              primary_channel: 'value',
              config_fields: [],
              channels: [],
            },
          ],
        },
      }
    : undefined;

describe('[severity] sensor form', () => {
  const s = mkSensor(2, 'Ping', { impact: 'warning', severity_warning: 'minor' });
  const patchRoute =
    (more: Route): Route =>
    (method, url, body) =>
      method === 'PATCH' && url === '/api/sensors/2'
        ? { status: 200, body: { ...s, ...(body as object) } }
        : (PING_TYPES(method, url, body) ?? more(method, url, body));

  it('ON: two overrides, Automatic names what the mapping resolves to', async () => {
    const calls = mockFetch(
      routes(
        true,
        patchRoute(() => undefined),
      ),
    );
    render(
      <Wrap>
        <EditSensorModal open onClose={() => undefined} sensor={s} />
      </Wrap>,
    );
    const down = (await screen.findByTestId('severity-down')) as HTMLSelectElement;
    await waitFor(() =>
      expect(down.options[0].textContent).toBe('Automatic (by the mapping): Major'),
    );
    expect(down.value).toBe('');
    const warn = screen.getByTestId('severity-warning') as HTMLSelectElement;
    expect(warn.value).toBe('minor');
    expect(screen.getByText('Severity when down')).toBeTruthy();
    fireEvent.change(down, { target: { value: 'critical' } });
    fireEvent.change(warn, { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PATCH')).toBe(true));
    const body = calls.find((c) => c.method === 'PATCH')!.body as Record<string, unknown>;
    expect(body.severity_down).toBe('critical');
    expect(body.severity_warning).toBeNull();
  });

  it('OFF: no severity fields and no severity keys sent', async () => {
    const calls = mockFetch(
      routes(
        false,
        patchRoute(() => undefined),
      ),
    );
    render(
      <Wrap>
        <EditSensorModal open onClose={() => undefined} sensor={s} />
      </Wrap>,
    );
    await screen.findByTestId('impact-field');
    await waitFor(() =>
      expect(calls.some((c) => c.url.startsWith('/api/settings/public'))).toBe(true),
    );
    expect(screen.queryByTestId('severity-overrides')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PATCH')).toBe(true));
    const body = calls.find((c) => c.method === 'PATCH')!.body as Record<string, unknown>;
    expect('severity_down' in body).toBe(false);
    expect('severity_warning' in body).toBe(false);
  });
});

const EV = {
  id: 1,
  time: '2026-11-22T08:15:00Z',
  kind: 'status_change',
  sensor_id: 7,
  device_id: 3,
  group_id: 1,
  sensor_name: 'Ping',
  device_name: 'core-sw',
  old_status: 'up',
  new_status: 'down',
  message: 'Host unreachable',
  in_maintenance: false,
  severity: 'critical',
};
const eventsRoute: Route = (_m, url) => {
  if (url.startsWith('/api/events'))
    return { status: 200, body: { items: [EV], total: 1, page: 1, page_size: 50 } };
  if (url.startsWith('/api/tree')) return { status: 200, body: { groups: [] } };
  return undefined;
};

describe('[severity] Events page', () => {
  it('ON: tag on rows, the filter and the order go to the query', async () => {
    const calls = mockFetch(routes(true, eventsRoute));
    render(
      <Wrap auth>
        <EventsPage />
      </Wrap>,
    );
    const bar = await screen.findByTestId('event-severity-bar');
    expect((await screen.findByTestId('severity-tag')).textContent).toBe('Critical');
    fireEvent.click(within(bar).getByLabelText('Major'));
    fireEvent.click(within(bar).getByLabelText('Critical'));
    await waitFor(() =>
      expect(calls.some((c) => c.url.includes('severity=critical%2Cmajor'))).toBe(true),
    );
    fireEvent.click(within(bar).getByRole('tab', { name: 'Highest severity first' }));
    await waitFor(() => expect(calls.some((c) => c.url.includes('sort=severity'))).toBe(true));
  });

  it('OFF: no severity bar, no tags, no severity parameters', async () => {
    const calls = mockFetch(routes(false, eventsRoute));
    render(
      <Wrap auth>
        <EventsPage />
      </Wrap>,
    );
    await screen.findAllByTestId('event-entry');
    await waitFor(() =>
      expect(calls.some((c) => c.url.startsWith('/api/settings/public'))).toBe(true),
    );
    expect(screen.queryByTestId('event-severity-bar')).toBeNull();
    expect(screen.queryByTestId('severity-tag')).toBeNull();
    expect(calls.some((c) => /severity|sort=/.test(c.url))).toBe(false);
  });
});

function dash(on: boolean): Dashboard {
  const zero = { up: 0, warning: 0, down: 0, paused: 0, unknown: 0, unreachable: 0, total: 0 };
  return {
    sensor_counts: { ...zero, down: 1, warning: 1, total: 2 },
    device_counts: { ...zero, total: 1 },
    group_count: 1,
    recent_events: [],
    problem_sensors: [
      mkSensor(7, 'Ping', { status: 'down', severity: on ? 'critical' : null }),
      mkSensor(8, 'CPU', { status: 'warning', severity: on ? 'warning' : null }),
    ],
    acknowledged_sensors: [],
    acknowledged_count: 0,
    unreachable_devices: [],
    maintenance: { active_window_ids: [], device_count: 0 },
    severity_counts: on ? { critical: 1, major: 0, minor: 0, warning: 1, info: 0, total: 2 } : null,
    severity_counts_unacknowledged: on
      ? { critical: 1, major: 0, minor: 0, warning: 1, info: 0, total: 2 }
      : null,
  };
}
const dashRoute =
  (on: boolean): Route =>
  (_m, url) => {
    if (url.startsWith('/api/dashboard')) return { status: 200, body: dash(on) };
    if (url.startsWith('/api/sensors?'))
      return {
        status: 200,
        body: { items: [dash(on).problem_sensors[0]], total: 1, page: 1, page_size: 1000 },
      };
    if (url.startsWith('/api/tree')) return { status: 200, body: { groups: [] } };
    return undefined;
  };

describe('[severity] Home', () => {
  it('ON: counts as chips, a tag per alert; a chip filters the list', async () => {
    const calls = mockFetch(routes(true, dashRoute(true)));
    render(
      <Wrap auth>
        <DashboardPage />
      </Wrap>,
    );
    const chips = await screen.findByTestId('severity-counts');
    const counts = within(chips).getAllByTestId('severity-count');
    expect(counts.map((c) => c.getAttribute('data-severity'))).toEqual(LEVELS);
    expect(counts[0].textContent).toContain('1');
    expect(
      screen
        .getAllByTestId('problem-row')
        .map((r) => within(r).getByTestId('severity-tag').textContent),
    ).toEqual(['Critical', 'Warning']);
    fireEvent.click(counts[0]);
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.url.startsWith('/api/sensors?') &&
            c.url.includes('severity=critical') &&
            c.url.includes('sort=severity'),
        ),
      ).toBe(true),
    );
    await waitFor(() => expect(screen.getAllByTestId('problem-row')).toHaveLength(1));
    expect(counts[0].getAttribute('aria-pressed')).toBe('true');
  });

  it('OFF: no chips, no tags', async () => {
    mockFetch(routes(false, dashRoute(false)));
    render(
      <Wrap auth>
        <DashboardPage />
      </Wrap>,
    );
    expect(await screen.findAllByTestId('problem-row')).toHaveLength(2);
    expect(screen.queryByTestId('severity-counts')).toBeNull();
    expect(screen.queryByTestId('severity-tag')).toBeNull();
  });
});

describe('[severity] live cache', () => {
  it('sensor.severity patches the sensor, lists and the dashboard', () => {
    const qc = new QueryClient();
    const s: Sensor = mkSensor(7, 'Ping', { status: 'down', severity: 'minor' });
    qc.setQueryData(qk.sensor(7), s);
    qc.setQueryData(qk.sensors({ x: 1 }), { items: [s], total: 1, page: 1, page_size: 50 });
    qc.setQueryData(qk.dashboard, { ...dash(true), problem_sensors: [s] });
    applySeverityToCache(qc, 7, 'critical');
    expect(qc.getQueryData<Sensor>(qk.sensor(7))!.severity).toBe('critical');
    expect(qc.getQueryData<{ items: Sensor[] }>(qk.sensors({ x: 1 }))!.items[0].severity).toBe(
      'critical',
    );
    expect(qc.getQueryData<Dashboard>(qk.dashboard)!.problem_sensors[0].severity).toBe('critical');
  });
});
