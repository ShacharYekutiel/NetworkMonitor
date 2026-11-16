import { describe, expect, it } from 'vitest';
import { SseParser } from './sse';
import {
  applyStreamEvent,
  compactTokens,
  connErrors,
  connInput,
  connKey,
  connMissing,
  copyableMarkdown,
  daysUntil,
  EMPTY_LIVE,
  formatCost,
  isRetryable,
  nmRoute,
  parseCidrs,
  profileScope,
  resolveNmLink,
  shiftMonth,
  toStreamEvent,
  visibleExamples,
  type ConnForm,
} from './assistant';
import { renderAnswer } from './markdown';
import { normalizeFeatures } from './features';
import type { AssistantMessage, StreamEvent } from '../api/types';
import {
  alertDraftToForm,
  eventDraftRuleForm,
  eventDraftToForm,
  normalizeDraftMatch,
} from './drafts';
import { formToInput } from './eventRules';

describe('SseParser', () => {
  const sample =
    'id: 1\nevent: start\ndata: {"conversation_id":12,"message_id":340,"language":"en","redaction":false}\n\n' +
    ': keepalive\n\n' +
    'id: 2\nevent: delta\ndata: {"text":"Two"}\n\n';

  it('parses events, ignores comments', () => {
    const p = new SseParser();
    const ev = p.push(sample);
    expect(ev.map((e) => e.event)).toEqual(['start', 'delta']);
    expect(ev[1]).toEqual({ event: 'delta', data: '{"text":"Two"}', id: '2' });
  });

  it('handles chunks split at every byte offset and CRLF', () => {
    const crlf = sample.replace(/\n/g, '\r\n');
    for (const src of [sample, crlf]) {
      for (let i = 1; i < src.length; i++) {
        const p = new SseParser();
        const out = [...p.push(src.slice(0, i)), ...p.push(src.slice(i))];
        expect(out.map((e) => e.id)).toEqual(['1', '2']);
      }
    }
  });

  it('joins multi-line data and drops an unterminated event', () => {
    const p = new SseParser();
    expect(p.push('data: a\ndata: b\n\n')[0].data).toBe('a\nb');
    expect(p.push('event: x\ndata: y')).toEqual([]);
    expect(p.end()).toEqual([]);
  });
});

describe('nm links', () => {
  it('maps every ref kind of § 14.3.7', () => {
    expect(nmRoute('device/12')).toBe('/devices/12');
    expect(nmRoute('sensor/7')).toBe('/sensors/7');
    expect(nmRoute('group/3')).toBe('/groups/3');
    expect(nmRoute('port/3/10101')).toBe('/devices/3?tab=ports&port=10101');
    expect(nmRoute('event/9')).toBe('/events?id=9');
    expect(nmRoute('syslog/5')).toBe('/logs?tab=syslog&id=5');
    expect(nmRoute('trap/5')).toBe('/logs?tab=traps&id=5');
    expect(nmRoute('maintenance/2')).toBe('/maintenance?id=2');
    expect(nmRoute('config/3/88')).toBe('/devices/3?tab=configuration&version=88');
    expect(nmRoute('config-diff/3/80/88')).toBe('/devices/3?tab=configuration&from=80&to=88');
    expect(nmRoute('flows/device_id=3&kind=talkers')).toBe('/flows?device_id=3&kind=talkers');
    expect(nmRoute('topology/4')).toBe('/topology?focus=4');
    expect(nmRoute('incident/1')).toBe('/incidents/1');
  });

  it('rejects malformed refs', () => {
    for (const r of ['device/x', 'device/1/2', 'evil/1', 'device', '', 'flows/a b', 'port/1'])
      expect(nmRoute(r)).toBeNull();
  });

  it('kb refs resolve to https sources only; allowedRefs restricts while streaming', () => {
    const sources = [
      { ref: 'kb/1', label: 'Runbook', url: 'https://sp.example/runbook' },
      { ref: 'kb/2', label: 'x.pdf', url: 's3://bucket/x.pdf' },
    ];
    expect(resolveNmLink('nm:kb/1', { sources })).toEqual({
      kind: 'external',
      href: 'https://sp.example/runbook',
    });
    expect(resolveNmLink('nm:kb/2', { sources })).toBeNull();
    expect(resolveNmLink('https://evil.example')).toBeNull();
    expect(resolveNmLink('nm:device/1', { allowedRefs: new Set(['device/2']) })).toBeNull();
    expect(resolveNmLink('nm:device/2', { allowedRefs: new Set(['device/2']) })?.href).toBe(
      '/devices/2',
    );
  });

  it('copyable markdown uses absolute app URLs', () => {
    expect(copyableMarkdown('See [core](nm:device/3) and [x](nm:bad/1).', 'https://nm')).toBe(
      'See [core](https://nm/devices/3) and x.',
    );
  });
});

describe('renderAnswer (safety)', () => {
  it('renders nm links as internal links and drops everything else', () => {
    const h = renderAnswer(
      '[core](nm:device/3) [ev](https://evil.example/?d=1) ![img](https://evil.example/x.png) <img src=x onerror=alert(1)> <script>x</script>',
    );
    expect(h).toContain('<a href="/devices/3" data-nm="1">core</a>');
    expect(h).not.toContain('<img');
    expect(h).not.toContain('<script');
    expect(h).not.toContain('href="https://evil');
    expect(h).toContain('&lt;img');
  });

  it('forged refs render as text while streaming', () => {
    const h = renderAnswer('[x](nm:device/9)', { allowedRefs: new Set(['device/3']) });
    expect(h).not.toContain('<a');
    expect(h).toContain('x');
  });

  it('keeps addresses and code LTR, blocks dir=auto', () => {
    const h = renderAnswer('השרת 10.1.2.3 נפל\n\n```\nshow ip int\n```');
    expect(h).toContain('<span dir="ltr">10.1.2.3</span>');
    expect(h).toContain('<p dir="auto">');
    expect(h).toContain('<pre dir="ltr">');
  });

  it('renders tables and lists', () => {
    const h = renderAnswer('| a | b |\n|---|---|\n| 1 | 2 |\n\n- x\n- y');
    expect(h).toContain('<table>');
    expect(h).toContain('<li dir="auto">');
  });
});

describe('stream reducer', () => {
  const ev = (e: string, id: number, data: unknown) =>
    toStreamEvent({ event: e, id: String(id), data: JSON.stringify(data) }) as StreamEvent;
  it('assembles steps, rounds, refs and the final message', () => {
    let s = EMPTY_LIVE;
    s = applyStreamEvent(
      s,
      ev('start', 1, { conversation_id: 12, message_id: 340, language: 'en', redaction: false }),
    );
    const step = {
      call_id: 't1',
      round: 1,
      tool: 'list_problems',
      label_key: 'assistant.tool.list_problems',
      args: {},
      state: 'running',
      ok: null,
      rows: null,
      truncated: false,
      ms: null,
      error: null,
    };
    s = applyStreamEvent(s, ev('delta', 2, { text: 'Checking…' }));
    s = applyStreamEvent(s, ev('round_end', 3, { round: 1, reason: 'tool_use' }));
    s = applyStreamEvent(s, ev('step', 4, step));
    s = applyStreamEvent(
      s,
      ev('step', 5, { ...step, state: 'done', ok: true, rows: 2, ms: 41, refs: ['device/3'] }),
    );
    s = applyStreamEvent(s, ev('delta', 6, { text: 'Two sensors' }));
    // a replayed event is ignored
    s = applyStreamEvent(s, ev('delta', 6, { text: 'Two sensors' }));
    expect(s.messageId).toBe(340);
    expect(s.notes).toEqual([{ round: 1, text: 'Checking…' }]);
    expect(s.steps).toHaveLength(1);
    expect(s.steps[0].rows).toBe(2);
    expect(s.refs).toEqual(['device/3']);
    expect(s.text).toBe('Two sensors');
    const msg = { id: 340, conversation_id: 12, status: 'complete' } as AssistantMessage;
    s = applyStreamEvent(s, ev('done', 7, { message: msg }));
    expect(s.final?.status).toBe('complete');
  });
  it('ignores unknown events and bad JSON', () => {
    expect(toStreamEvent({ event: 'zzz', id: null, data: '{}' })).toBeNull();
    expect(toStreamEvent({ event: 'delta', id: null, data: '{' })).toBeNull();
  });
  it('retryable codes', () => {
    expect(isRetryable('bedrock_throttled')).toBe(true);
    expect(isRetryable('bedrock_model_access')).toBe(false);
  });
});

describe('connection form', () => {
  // [4] the authentication moved to the AWS connection: the form holds the Bedrock fields, the
  // connection ('' = the default) and the access
  const base: ConnForm = {
    region: 'us-east-1',
    model_id: 'us.anthropic.claude-x-v1:0',
    model_id_light: '',
    endpoint_url: '',
    control_endpoint_url: '',
    prompt_caching: true,
    connection_id: '',
    access_mode: 'direct',
    role_arn: '',
    external_id: '',
  };
  const hop: ConnForm = {
    ...base,
    connection_id: '2',
    access_mode: 'assume_role',
    role_arn: 'arn:aws:iam::222222222222:role/netmon-bedrock',
    external_id: 'AbCdEf0123456789AbCdEf0123456789',
  };
  it('validates', () => {
    expect(connErrors(base)).toEqual({});
    expect(connErrors({ ...base, region: 'Frankfurt' }).region).toBeTruthy();
    expect(connErrors({ ...base, endpoint_url: 'http://x' }).endpoint_url).toBeTruthy();
    expect(
      connErrors({
        ...base,
        endpoint_url: 'https://vpce-1.bedrock-runtime.us-east-1.vpce.amazonaws.com',
      }).endpoint_url,
    ).toBeUndefined();
    expect(connErrors(hop)).toEqual({});
    expect(connErrors({ ...hop, role_arn: 'role' }).role_arn).toBe('arn');
    expect(connErrors({ ...hop, role_arn: '' }).role_arn).toBe('required');
    expect(
      connErrors({
        ...hop,
        role_arn: 'arn:aws:iam::222222222222:role/arn:aws:iam::222222222222:role/x',
      }).role_arn,
    ).toBe('arnTwice');
    expect(connErrors({ ...hop, external_id: 'bad id!' }).external_id).toBe('externalId');
    // ra_role: only with a Roles Anywhere connection, role in the profile's account
    const ra: ConnForm = { ...hop, access_mode: 'ra_role', external_id: '' };
    expect(connErrors(ra, 'access_key').access_mode).toBe('raOnly');
    const prof = 'arn:aws:rolesanywhere:us-east-1:111111111111:profile/p-1';
    expect(connErrors(ra, 'roles_anywhere', prof).role_arn).toBe('profileAccount');
    expect(
      connErrors(
        { ...ra, role_arn: 'arn:aws:iam::111111111111:role/other' },
        'roles_anywhere',
        prof,
      ),
    ).toEqual({});
  });
  it('the request body carries connection_id and access (never an auth field)', () => {
    expect(connInput(base)).toMatchObject({
      connection_id: null,
      access: { mode: 'direct', role_arn: null, external_id: null },
    });
    const body = connInput(hop);
    expect(body).toMatchObject({
      connection_id: 2,
      access: { mode: 'assume_role', role_arn: hop.role_arn, external_id: hop.external_id },
    });
    for (const k of ['auth_mode', 'proxy_mode', 'access_key_id', 'ra_role_arn'])
      expect(k in body, k).toBe(false);
    // a direct access drops a typed role
    expect(connInput({ ...hop, access_mode: 'direct' }).access).toEqual({
      mode: 'direct',
      role_arn: null,
      external_id: null,
    });
  });
  it('any connection edit changes the key', () => {
    expect(connKey(base)).toBe(connKey({ ...base }));
    expect(connKey(base)).not.toBe(connKey({ ...base, region: 'il-central-1' }));
    expect(connKey(base)).not.toBe(connKey({ ...base, connection_id: '3' }));
    expect(connKey(hop)).not.toBe(connKey({ ...hop, external_id: 'other-value' }));
    expect(connKey(base, 'a')).not.toBe(connKey(base, 'b'));
  });
  it('missing: no AWS connection at all', () => {
    expect(connMissing(base, [])).toEqual(['connection']);
    expect(connMissing(base, [{ id: 1 }])).toEqual([]);
    expect(connMissing(base, undefined)).toEqual([]);
  });
  it('profile scope', () => {
    expect(profileScope('us.anthropic.claude')).toBe('us');
    expect(profileScope('global.anthropic.claude')).toBe('global');
    expect(profileScope('anthropic.claude-x')).toBe('regional');
    expect(profileScope('arn:aws:bedrock:…')).toBe('arn');
    expect(profileScope('')).toBe('none');
  });
});

describe('misc helpers', () => {
  it('cost, tokens, days, months, cidrs', () => {
    expect(formatCost(0.0421, 'USD')).toBe('$0.042');
    expect(formatCost(12.5, 'EUR')).toBe('12.50 EUR');
    expect(formatCost(null, 'USD')).toBe('—');
    expect(compactTokens(1_234_567)).toBe('1.2M');
    expect(compactTokens(12_345)).toBe('12.3k');
    expect(daysUntil('2026-01-11T00:00:00Z', Date.parse('2026-01-01T00:00:00Z'))).toBe(10);
    expect(shiftMonth('2026-01', -1)).toBe('2025-12');
    expect(parseCidrs('10.0.0.0/8, 300.1.1.1/8 fd00::/8')).toEqual({
      items: ['10.0.0.0/8', 'fd00::/8'],
      bad: ['300.1.1.1/8'],
    });
  });
  it('examples hide chips of OFF features', () => {
    const off = visibleExamples(normalizeFeatures({}));
    expect(off.find((e) => e.key === 'talkers')).toBeUndefined();
    const on = visibleExamples(
      normalizeFeatures({ flows: true, syslog: true, config_backup: true }),
    );
    expect(on.find((e) => e.key === 'talkers')).toBeTruthy();
  });
});

describe('rule drafts → forms', () => {
  it('alert rule draft fills only known fields', () => {
    const f = alertDraftToForm({
      name: 'Core down',
      sensor_id: null,
      on_statuses: ['down', 'up'],
      delay_s: 120,
      channel_ids: [2],
      ai_note: true,
      bogus: 1,
    });
    expect(f).toEqual({
      name: 'Core down',
      sensor_id: null,
      on_statuses: ['down', 'up'],
      delay_s: 120,
      channel_ids: [2],
      ai_note: true,
    });
  });
  it('drops wrongly typed values', () => {
    expect(alertDraftToForm({ name: 3, delay_s: 'x', on_statuses: ['down', 'nope'] })).toEqual({
      on_statuses: ['down'],
    });
    expect(eventDraftToForm({ name: 'r', kind: 'syslog', x: 1 })).toEqual({
      name: 'r',
      kind: 'syslog',
    });
  });
});

describe('[3b owner] event-rule draft text/regex reaches the form', () => {
  it('keeps text + text_regex from a well-formed draft', () => {
    const f = eventDraftRuleForm({
      name: 'כשלי SSH',
      kind: 'syslog',
      match: {
        severity_max: null,
        facilities: [],
        programs: ['SSHS'],
        text: '[Ff]ail(ed|ure)?',
        text_regex: true,
      },
      action: 'warning',
      clear_after_min: 30,
    });
    expect(f.match.text).toBe('[Ff]ail(ed|ure)?');
    expect(f.match.textRegex).toBe(true);
    expect(f.match.programs).toBe('SSHS');
    const input = formToInput(f);
    expect(input.match).toMatchObject({ text: '[Ff]ail(ed|ure)?', text_regex: true });
  });
  it('repairs the pattern put into text_regex and a single program (owner bug)', () => {
    const f = eventDraftRuleForm({
      name: 'SSH',
      kind: 'syslog',
      match: { program: 'sshd', text_regex: 'Failed password for .* from' },
      action: 'warning',
    });
    expect(f.match.text).toBe('Failed password for .* from');
    expect(f.match.textRegex).toBe(true);
    expect(f.match.programs).toBe('sshd');
    expect(formToInput(f).match).toMatchObject({
      programs: ['sshd'],
      text: 'Failed password for .* from',
      text_regex: true,
    });
  });
  it('normalises aliases, leaves trap matches alone', () => {
    expect(normalizeDraftMatch({ pattern: 'a|b' })).toEqual({
      text: 'a|b',
      text_regex: true,
      severity_max: null,
      facilities: [],
      programs: [],
    });
    expect(normalizeDraftMatch({ message: 'down', text_regex: 'false' })).toMatchObject({
      text: 'down',
      text_regex: false,
    });
    const trap = { trap_oids: ['1.3.6.1.6.3.1.1.5.3'], varbinds: [], ports: 'any' };
    expect(normalizeDraftMatch(trap, 'trap')).toBe(trap);
  });
});
