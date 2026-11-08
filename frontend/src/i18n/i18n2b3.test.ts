import { describe, expect, it } from 'vitest';
import en from './locales/en.json';
import he from './locales/he.json';
import {
  DROP_REASONS,
  EVENT_RULE_ACTIONS,
  EVENT_SENSOR_TYPES,
  FEATURE_NAMES,
  LDAP_STEPS,
  LOGIN_FAILURE_REASONS,
  TIMELINE_KINDS,
  VARBIND_OPS,
} from '../api/types';
import { AUDIT_ACTION_PREFIXES, AUDIT_ENTITY_TYPES } from '../lib/audit';
import { TIME_PRESETS } from '../lib/logs';

type Tree = { [k: string]: string | Tree };
function flatten(obj: Tree, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (typeof v === 'string') out[key] = v;
    else Object.assign(out, flatten(v, key));
  }
  return out;
}
const enFlat = flatten(en as Tree);
const heFlat = flatten(he as Tree);

describe('i18n [2b-3]', () => {
  it('covers every dynamic key of the phase in both languages', () => {
    const keys = [
      ...FEATURE_NAMES.flatMap((f) => [`features.name.${f}`, `features.desc.${f}`]),
      ...[
        'needsIngest',
        'noTargets',
        'flappingTraps',
        'ldapConfig',
        'timelineSources',
        'lagPorts',
      ].map((h) => `features.hint.${h}`),
      ...['on', 'listening', 'error', 'socketOff', 'stopped', 'forwardErrors', 'targets'].map(
        (s) => `features.status.${s}`,
      ),
      ...Array.from({ length: 8 }, (_, i) => `syslogSeverity.${i}`),
      ...Array.from({ length: 24 }, (_, i) => `syslogFacility.${i}`),
      ...TIME_PRESETS.map((p) => `logs.preset.${p}`),
      ...['invalid', 'order', 'tooLong'].map((e) => `logs.rangeErr.${e}`),
      ...['syslog', 'traps', 'unknown', 'rules'].map((x) => `logs.tab.${x}`),
      ...VARBIND_OPS.map((o) => `eventRules.op.${o}`),
      ...EVENT_RULE_ACTIONS.flatMap((a) => [
        `eventRules.actions.${a}`,
        `eventRules.actionHint.${a}`,
      ]),
      ...['raise', 'clear', 'ignore'].map((r) => `eventRules.result.${r}`),
      ...['trap', 'syslog_udp', 'syslog_tcp'].map((r) => `ingest.receiver.${r}`),
      ...['listening', 'disabled', 'error'].map((s) => `ingest.state.${s}`),
      ...DROP_REASONS.map((r) => `ingest.drop.${r}`),
      ...['store', 'drop'].map((x) => `ingestSettings.unknownHint.${x}`),
      ...['any', 'known'].map((x) => `ingestSettings.checkHint.${x}`),
      ...['important', 'all', 'importantHint', 'allHint', 'label', 'title'].map(
        (x) => `ingestSettings.linkScope.${x}`,
      ),
      ...['connected', 'connecting', 'idle', 'error', 'disabled'].map((s) => `siem.fw.${s}`),
      ...['ldaps', 'starttls'].map((s) => `ldap.securityHint.${s}`),
      ...LOGIN_FAILURE_REASONS.map((r) => `ldap.reason.${r}`),
      ...LDAP_STEPS.map((s) => `ldap.step.${s}`),
      ...AUDIT_ACTION_PREFIXES.map((a) => `audit.actionGroup.${a.slice(0, -1)}`),
      ...AUDIT_ENTITY_TYPES.map((e) => `audit.entityType.${e}`),
      ...['user', 'token', 'system', 'anonymous'].map((a) => `audit.actorType.${a}`),
      ...['local', 'ldap', 'token'].map((s) => `audit.source.${s}`),
      ...TIMELINE_KINDS.map((k) => `timeline.kind.${k}`),
      ...['active', 'expired', 'revoked'].map((s) => `tokens.states.${s}`),
      ...['trap', 'syslog', 'port_flap'].map((s) => `conditions.intro.${s}`),
      ...['general', 'features', 'ingest', 'siem', 'ldap', 'audit'].map((s) => `settings.tab.${s}`),
      ...['ports', 'sensors', 'logs', 'timeline'].map((s) => `devices.tab.${s}`),
      ...['local', 'ldap'].map((s) => `user.source.${s}`),
      ...EVENT_SENSOR_TYPES.map((s) => `sensorType.${s}`),
      'eventKind.notification_sent',
      'eventKind.port_change',
      'channels.teams',
      'channel.active',
      ...[
        'feature_disabled',
        'sensor_passive',
        'not_event_sensor',
        'builtin_rule',
        'ldap_managed',
        'directory_unavailable',
        'invalid_cursor',
        'teams_error',
        'forward_error',
      ].map((c) => `errors.${c}`),
    ];
    for (const k of keys) {
      expect(enFlat[k], `en:${k}`).toBeTruthy();
      expect(heFlat[k], `he:${k}`).toBeTruthy();
    }
  });

  it('has the login AD hint with the domain placeholders', () => {
    expect(enFlat['login.adHint']).toContain('{{netbios}}\\user');
    expect(heFlat['login.adHint']).toContain('{{domain}}');
  });
});
