import { describe, expect, it } from 'vitest';
import en from './locales/en.json';
import he from './locales/he.json';
import i18n from './index';
import {
  BACKUP_MODES,
  BACKUP_REASONS,
  BACKUP_RESULTS,
  BACKUP_TRIGGERS,
  FLOW_DROP_REASONS,
  KIOSK_WIDGETS,
  KNOWN_MAC_STATES,
  REPORT_SCHEDULES,
  REPORT_SECTIONS,
  SSH_STEPS,
  SSO_ERRORS,
} from '../api/types3';
import { EVENT_RULE_ACTIONS, FEATURE_NAMES, SENSOR_TYPES, TIMELINE_KINDS } from '../api/types';
import { AUDIT_ACTION_PREFIXES } from '../lib/audit';
import { FEATURE_GROUPS } from '../lib/features';
import { FLOW_PRESETS } from '../lib/flows';

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
const has = (flat: Record<string, string>, k: string) => k in flat || `${k}_other` in flat;

describe('i18n [3]', () => {
  it('covers every dynamic key of phase 3 in both languages', () => {
    const keys = [
      ...FEATURE_NAMES.flatMap((f) => [`features.name.${f}`, `features.desc.${f}`]),
      ...FEATURE_GROUPS.map((g) => `features.group.${g.key}`),
      ...[
        'sshCredentials',
        'backupSyslog',
        'flowExporters',
        'newdevFdb',
        'kerberosLdap',
        'keytab',
        'oidcSetup',
        'reportsDelivery',
        'publicUrl',
        'telegramButtons',
      ].map((h) => `features.hint.${h}`),
      ...[
        'nextBackup',
        'backupRunning',
        'driverErrors',
        'learningUntil',
        'watching',
        'keytab',
        'noKeytab',
        'kerberosUnsupported',
        'kerberosError',
        'noPoller',
        'pollerOk',
        'pollerIdle',
        'pollerError',
        'unknown',
      ].map((s) => `features.status.${s}`),
      ...BACKUP_MODES.flatMap((m) => [`configBackup.modes.${m}`, `configBackup.modeHint.${m}`]),
      ...BACKUP_RESULTS.map((r) => `configBackup.result.${r}`),
      ...BACKUP_TRIGGERS.map((r) => `configBackup.trigger.${r}`),
      ...BACKUP_REASONS.map((r) => `configBackup.reason.${r}`),
      ...['override', 'template', 'sys_object_id'].map((s) => `driver.source.${s}`),
      ...SSH_STEPS.map((s) => `ssh.step.${s}`),
      ...['device', 'group', 'none'].map((s) => `ssh.source.${s}`),
      ...FLOW_PRESETS.map((p) => `flows.preset.${p}`),
      ...FLOW_DROP_REASONS.map((r) => `flows.drop.${r}`),
      ...['top', 'interfaces', 'exporters', 'status'].map((x) => `flows.tab.${x}`),
      ...['talkers', 'apps', 'conversations'].map((x) => `flows.col.${x}`),
      ...['both', 'src', 'dst'].map((x) => `flows.sides.${x}`),
      ...['none', 'app', 'host'].map((x) => `flows.splits.${x}`),
      ...['in', 'out'].map((x) => `flows.dir.${x}`),
      ...['netflow', 'ipfix', 'sflow'].map((x) => `flows.receiver.${x}`),
      ...['cagg', 'table'].map((x) => `flows.rollup.${x}`),
      ...['accepted', 'pending', 'ignored'].map((x) => `exporter.states.${x}`),
      ...['netflow5', 'netflow9', 'ipfix', 'sflow5'].map((x) => `exporter.proto.${x}`),
      ...KNOWN_MAC_STATES.map((s) => `newDevices.states.${s}`),
      ...['detected', 'known', 'allowlist'].map((x) => `newDevices.tab.${x}`),
      ...['ready', 'unavailable', 'disabled', 'error'].map((s) => `kerberos.state.${s}`),
      ...['rc4_only', 'no_http_principal'].map((s) => `kerberos.warning.${s}`),
      ...['app_roles', 'groups'].map((s) => `oidc.valuesHint.${s}`),
      ...SSO_ERRORS.map((e) => `sso.error.${e}`),
      ...['running', 'ok', 'partial', 'failed'].map((s) => `reports.status.${s}`),
      ...[1, 2, 3, 4, 5, 6, 7].map((d) => `reports.weekday.${d}`),
      ...REPORT_SCHEDULES.map((s) => `reports.schedules.${s}`),
      ...['schedule', 'manual'].map((s) => `reports.triggers.${s}`),
      ...['feature_off', 'no_data'].map((s) => `reports.note.${s}`),
      ...REPORT_SECTIONS.map((s) => `reportSection.${s}`),
      ...KIOSK_WIDGETS.map((w) => `kiosk.widget.${w}`),
      ...['idle', 'polling', 'error'].map((s) => `chatAck.pollerState.${s}`),
      ...['backup_failed', 'host_key', 'config_changed', 'new_mac'].map(
        (s) => `conditions.source.${s}`,
      ),
      ...EVENT_RULE_ACTIONS.flatMap((a) => [
        `eventRules.actions.${a}`,
        `eventRules.actionHint.${a}`,
      ]),
      ...SENSOR_TYPES.map((s) => `sensorType.${s}`),
      ...TIMELINE_KINDS.map((k) => `timeline.kind.${k}`),
      ...AUDIT_ACTION_PREFIXES.map((a) => `audit.actionGroup.${a.slice(0, -1)}`),
      ...['sso', 'backup', 'flows', 'newDevices', 'reports', 'noc', 'chatAck'].map(
        (s) => `settings.tab.${s}`,
      ),
      'devices.tab.config',
      'user.source.oidc',
      'eventKind.config_change',
      'eventKind.new_device',
      'newDevices.badge',
      ...[
        'backup_running',
        'host_key_mismatch',
        'no_host_key_change',
        'invalid_private_key',
        'ssh_error',
        'invalid_keytab',
        'sso_failed',
        'sso_ntlm',
        'sso_conflict',
        'oidc_error',
        'report_running',
        'ack_expired',
        'ack_invalid',
        'ack_stale',
      ].map((c) => `errors.${c}`),
    ];
    const missing = keys.flatMap((k) => [
      ...(has(enFlat, k) ? [] : [`en:${k}`]),
      ...(has(heFlat, k) ? [] : [`he:${k}`]),
    ]);
    expect(missing).toEqual([]);
  });

  it('renders the Hebrew plural of the new-devices badge', async () => {
    await i18n.changeLanguage('he');
    expect(i18n.t('newDevices.badge', { count: 1 })).toBe('התקן חדש אחד');
    expect(i18n.t('newDevices.badge', { count: 2 })).toBe('שני התקנים חדשים');
    expect(i18n.t('newDevices.badge', { count: 5 })).toBe('5 התקנים חדשים');
    await i18n.changeLanguage('en');
    expect(i18n.t('newDevices.badge', { count: 1 })).toBe('1 new device');
    expect(i18n.t('newDevices.badge', { count: 3 })).toBe('3 new devices');
  });
});
