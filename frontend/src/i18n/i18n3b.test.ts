import { describe, expect, it } from 'vitest';
import en from './locales/en.json';
import he from './locales/he.json';
import {
  AI_FEATURES,
  ASSISTANT_STATES,
  ASSISTANT_TEST_STEPS,
  ASSISTANT_WARNINGS,
  AUTH_MODES,
  INCIDENT_STATES,
  PROXY_MODES,
  REPORT_AI_STATUSES,
  WALK_ERROR_CODES,
  WALK_OUTCOMES,
  WALK_PRESETS,
} from '../api/types3b';
import { EXAMPLES } from '../lib/assistant';

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

const TOOLS = [
  'search_devices',
  'get_device',
  'list_problems',
  'search_sensors',
  'get_sensor_history',
  'list_events',
  'list_maintenance',
  'topology_neighbors',
  'topology_path',
  'get_device_ports',
  'find_mac_or_ip',
  'availability',
  'list_groups',
  'get_timeline',
  'search_syslog',
  'syslog_stats',
  'search_traps',
  'flow_top',
  'flow_interface_series',
  'list_new_devices',
  'config_versions',
  'config_diff',
  'config_search',
  'list_notification_channels',
  'search_knowledge',
];
const ERROR_CODES = [
  'assistant_test_required',
  'feature_dependency',
  'feature_idle',
  'assistant_busy',
  'assistant_cap_reached',
  'conversation_full',
  'invalid_certificate',
  'invalid_private_key',
  'key_mismatch',
  'bedrock_throttled',
  'bedrock_quota',
  'bedrock_model_access',
  'bedrock_access_denied',
  'bedrock_model_invalid',
  'bedrock_unavailable',
  'bedrock_validation',
  'aws_auth_failed',
  'aws_token_expired',
  'aws_session_token_required',
  'aws_clock_skew',
  'rolesanywhere_failed',
  'aws_network',
  'assistant_timeout',
  'kb_unavailable',
  'walk_running',
  'template_exists',
  'invalid_template',
];
const RISK_RULES = [
  'snmp_community_added',
  'mgmt_acl_removed',
  'mgmt_opened',
  'acl_permit_any',
  'acl_rule_removed',
  'expert_password_changed',
  'privileged_user_added',
  'local_user_added',
  'aaa_changed',
  'syslog_host_removed',
  'ntp_changed',
  'interface_shutdown',
  'interface_enabled',
  'vlan_removed',
  'vlan_removed_cmd',
  'routing_changed',
];

describe('i18n [3b]', () => {
  it('covers every dynamic key of phase 3b in both languages', () => {
    const keys = [
      'nav.assistant',
      'settings.tab.assistant',
      'features.group.assistant',
      ...['assistant', ...AI_FEATURES].flatMap((f) => [`features.name.${f}`, `features.desc.${f}`]),
      ...TOOLS.map((x) => `assistant.tool.${x}`),
      ...ERROR_CODES.map((c) => `errors.${c}`),
      ...RISK_RULES.map((r) => `diffRisk.${r}`),
      ...['high', 'medium', 'info'].map((s) => `diffRisk.severity.${s}`),
      ...['added', 'removed'].map((s) => `diffRisk.side.${s}`),
      ...ASSISTANT_STATES.map((s) => `assistant.state.${s}`),
      ...ASSISTANT_WARNINGS.map((w) => `assistant.warning.${w}`),
      ...ASSISTANT_TEST_STEPS.map((s) => `assistant.test.step.${s}`),
      ...AUTH_MODES.flatMap((m) => [
        `assistant.settings.auth.${m}`,
        `assistant.settings.authHint.${m}`,
      ]),
      ...PROXY_MODES.flatMap((m) => [
        `assistant.settings.proxy.${m}`,
        `assistant.settings.proxyHint.${m}`,
      ]),
      ...['us', 'eu', 'apac', 'jp', 'au', 'ca', 'us-gov', 'global', 'regional', 'arn'].map(
        (g) => `assistant.settings.geo.${g}`,
      ),
      ...EXAMPLES.map((e) => `assistant.example.${e.key}`),
      ...['limit_reached', 'truncated_context', 'retrying', 'kb_unavailable'].map(
        (n) => `assistant.notice.${n}`,
      ),
      ...['user_daily', 'monthly_tokens', 'monthly_cost', 'purpose_daily', 'chat_hourly'].map(
        (c) => `assistant.cap.${c}`,
      ),
      ...['chat', 'explain', 'rule_draft', 'template_draft'].map((k) => `assistant.kind.${k}`),
      ...INCIDENT_STATES.map((s) => `incident.state.${s}`),
      'incident.unit.devices',
      'incident.unit.sensors',
      ...['ok', 'failed', 'skipped', 'running'].map((s) => `digest.st.${s}`),
      ...WALK_PRESETS.map((p) => `tdraft.preset.${p}`),
      ...['running', 'ok', 'partial', 'failed', 'cancelled'].map((s) => `tdraft.walkStatus.${s}`),
      ...WALK_OUTCOMES.map((s) => `tdraft.outcome.${s}`),
      ...WALK_ERROR_CODES.map((c) => `tdraft.walkError.${c}`),
      'tdraft.walkErrorDetail',
      ...REPORT_AI_STATUSES.map((s) => `reports.aiStatus.${s}`),
      ...['low', 'medium', 'high'].map((c) => `confidence3.${c}`),
    ];
    for (const k of keys) {
      expect(has(enFlat, k), `en:${k}`).toBe(true);
      expect(has(heFlat, k), `he:${k}`).toBe(true);
    }
  });

  it('labels the AI text and keeps nm-free plain strings', () => {
    expect(enFlat['assistant.mayBeWrong']).toMatch(/AI-generated/);
    expect(heFlat['assistant.aiGenerated']).toBe('נוצר ע״י AI');
  });
});
