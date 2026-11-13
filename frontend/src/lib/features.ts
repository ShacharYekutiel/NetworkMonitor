// [2b-3] Feature toggles (architecture.md § 12.0). Pure helpers.
import { isApiError } from '../api/client';
import type {
  FeatureName,
  Features,
  IngestStatus,
  ReceiverStatus,
  Settings,
  SettingsInput,
} from '../api/types';
import { FEATURE_NAMES } from '../api/types';

/** Every feature OFF (the default, also while /api/settings/public is loading). */
export const ALL_OFF: Features = Object.fromEntries(
  FEATURE_NAMES.map((f) => [f, false]),
) as Features;

/** Fills missing keys with false (older servers / partial payloads). */
export function normalizeFeatures(f: Partial<Features> | null | undefined): Features {
  const out = { ...ALL_OFF };
  if (f) for (const k of FEATURE_NAMES) out[k] = f[k] === true;
  return out;
}

export function featureSettingKey(name: FeatureName): keyof SettingsInput {
  return `feature_${name}_enabled` as keyof SettingsInput;
}

/** Current toggle values from the admin settings object. */
export function featuresFromSettings(s: Settings): Features {
  const out = { ...ALL_OFF };
  for (const k of FEATURE_NAMES)
    out[k] = (s as unknown as Record<string, unknown>)[`feature_${k}_enabled`] === true;
  return out;
}

/** Traps & Syslog page, device Logs tab, ingest endpoints and event rules need one of the two. */
export function ingestOn(f: Features): boolean {
  return f.traps || f.syslog;
}

/** The feature named by a `409 feature_disabled` error, else null. */
export function disabledFeature(e: unknown): FeatureName | null {
  if (!isApiError(e) || e.code !== 'feature_disabled') return null;
  const f = e.details?.find((d) => typeof d.feature === 'string')?.feature;
  return (FEATURE_NAMES as string[]).includes(f ?? '') ? (f as FeatureName) : null;
}

export type FeatureHint = { key: string; tone: 'warn' | 'info' };

/** Dependency hints of a feature row (i18n keys under `features.hint`). */
export function featureHints(name: FeatureName, f: Features, s?: Settings): FeatureHint[] {
  const out: FeatureHint[] = [];
  switch (name) {
    case 'event_alerts':
      if (!ingestOn(f)) out.push({ key: 'features.hint.needsIngest', tone: 'warn' });
      break;
    case 'siem_forwarding':
      if (!ingestOn(f)) out.push({ key: 'features.hint.needsIngest', tone: 'warn' });
      if (s && !(s.siem_targets ?? []).length)
        out.push({ key: 'features.hint.noTargets', tone: 'info' });
      break;
    case 'flapping':
      if (!f.traps) out.push({ key: 'features.hint.flappingTraps', tone: 'info' });
      break;
    case 'ldap':
      if (s?.ldap && ldapMissing(s.ldap).length)
        out.push({ key: 'features.hint.ldapConfig', tone: 'warn' });
      break;
    case 'timeline':
      if (!f.audit || !ingestOn(f))
        out.push({ key: 'features.hint.timelineSources', tone: 'info' });
      break;
    case 'lag':
      out.push({ key: 'features.hint.lagPorts', tone: 'info' });
      break;
    // [3]
    case 'config_backup':
      out.push({ key: 'features.hint.sshCredentials', tone: 'info' });
      if (!f.syslog) out.push({ key: 'features.hint.backupSyslog', tone: 'info' });
      break;
    case 'flows':
      out.push({ key: 'features.hint.flowExporters', tone: 'info' });
      break;
    case 'new_device_alerts':
      out.push({ key: 'features.hint.newdevFdb', tone: 'info' });
      break;
    case 'sso_kerberos':
      if (!f.ldap) out.push({ key: 'features.hint.kerberosLdap', tone: 'warn' });
      out.push({ key: 'features.hint.keytab', tone: 'info' });
      break;
    case 'sso_oidc':
      out.push({ key: 'features.hint.oidcSetup', tone: 'info' });
      break;
    case 'reports':
      out.push({ key: 'features.hint.reportsDelivery', tone: 'info' });
      break;
    case 'chat_ack':
      if (s && !s.public_base_url) out.push({ key: 'features.hint.publicUrl', tone: 'warn' });
      out.push({ key: 'features.hint.telegramButtons', tone: 'info' });
      break;
    // [3b] dependency hints (§ 14.0.1); a feature ON with a missing dependency idles
    case 'assistant':
      if (!f.audit) out.push({ key: 'features.hint.assistantAudit', tone: 'warn' });
      break;
    case 'ai_explain_alert':
    case 'ai_correlation':
    case 'ai_template_drafts':
      if (!f.assistant) out.push({ key: 'features.hint.needsAssistant', tone: 'warn' });
      break;
    case 'ai_alert_notes':
      if (!f.assistant) out.push({ key: 'features.hint.needsAssistant', tone: 'warn' });
      out.push({ key: 'features.hint.alertNotes', tone: 'info' });
      break;
    case 'ai_config_diff':
      if (!f.assistant) out.push({ key: 'features.hint.needsAssistant', tone: 'warn' });
      if (!f.config_backup) out.push({ key: 'features.hint.needsConfigBackup', tone: 'warn' });
      out.push({ key: 'features.hint.configExcerpts', tone: 'info' });
      break;
    case 'ai_syslog_digest':
      if (!f.assistant) out.push({ key: 'features.hint.needsAssistant', tone: 'warn' });
      if (!f.syslog) out.push({ key: 'features.hint.needsSyslog', tone: 'warn' });
      break;
    case 'ai_rule_drafts':
      if (!f.assistant) out.push({ key: 'features.hint.needsAssistant', tone: 'warn' });
      out.push({ key: 'features.hint.ruleDraftsIngest', tone: 'info' });
      break;
    case 'ai_knowledge':
      if (!f.assistant) out.push({ key: 'features.hint.needsAssistant', tone: 'warn' });
      out.push({ key: 'features.hint.kbAws', tone: 'info' });
      break;
    case 'ai_chat_ask':
      if (!f.assistant) out.push({ key: 'features.hint.needsAssistant', tone: 'warn' });
      if (!f.chat_ack) out.push({ key: 'features.hint.needsChatAck', tone: 'warn' });
      out.push({ key: 'features.hint.chatAskChannel', tone: 'info' });
      break;
    case 'ai_report_summary':
      if (!f.assistant) out.push({ key: 'features.hint.needsAssistant', tone: 'warn' });
      if (!f.reports) out.push({ key: 'features.hint.needsReports', tone: 'warn' });
      break;
  }
  return out;
}

/** [3] Headings of the Features page (architecture.md § 13.0): 17 features in 5 groups (Teams and API tokens are not toggles). */
export const FEATURE_GROUPS: { key: string; names: FeatureName[] }[] = [
  { key: 'monitoring', names: ['flapping', 'timeline', 'reports', 'noc'] },
  { key: 'logs', names: ['traps', 'syslog', 'event_alerts', 'siem_forwarding', 'audit'] },
  { key: 'network', names: ['lag', 'config_backup', 'flows', 'new_device_alerts'] },
  { key: 'access', names: ['ldap', 'sso_kerberos', 'sso_oidc'] },
  { key: 'notifications', names: ['chat_ack'] },
  // [3b]
  {
    key: 'assistant',
    names: [
      'assistant',
      'ai_explain_alert',
      'ai_correlation',
      'ai_alert_notes',
      'ai_config_diff',
      'ai_syslog_digest',
      'ai_rule_drafts',
      'ai_template_drafts',
      'ai_knowledge',
      'ai_chat_ask',
      'ai_report_summary',
    ],
  },
];

/** [3] Phase-3 features (their status lines come from their own status endpoints). */
export const PHASE3_FEATURES: FeatureName[] = [
  'config_backup',
  'flows',
  'new_device_alerts',
  'sso_kerberos',
  'sso_oidc',
  'reports',
  'noc',
  'chat_ack',
];

/** Fields that turning `ldap` ON would reject (data-model § LDAP settings; 422 otherwise). */
export function ldapMissing(l: NonNullable<Settings['ldap']>): string[] {
  const miss: string[] = [];
  if (!l.bind_dn.trim()) miss.push('bind_dn');
  if (!l.bind_password_set || l.bind_password_unreadable) miss.push('bind_password');
  if (!l.servers.length && !l.domain.trim()) miss.push('servers');
  if (!l.base_dn.trim() && !l.domain.trim()) miss.push('base_dn');
  if (!l.admin_group_dn.trim() && !l.viewer_group_dn.trim()) miss.push('group_dn');
  return miss;
}

export type StatusLine = {
  key: string;
  params?: Record<string, string | number>;
  tone: 'ok' | 'error' | 'muted';
};

function receiverLine(r: ReceiverStatus | undefined, proto: 'UDP' | 'TCP'): StatusLine | null {
  if (!r) return null;
  if (r.state === 'listening')
    return { key: 'features.status.listening', params: { proto, port: r.port }, tone: 'ok' };
  if (r.state === 'error')
    return {
      key: 'features.status.error',
      params: { proto, port: r.port, error: r.error ?? '' },
      tone: 'error',
    };
  return r.port === 0 || !r.enabled
    ? { key: 'features.status.socketOff', params: { proto }, tone: 'muted' }
    : { key: 'features.status.stopped', params: { proto, port: r.port }, tone: 'muted' };
}

/** Status lines of a feature row ("Listening on UDP 162", a start error, …). */
export function featureStatus(
  name: FeatureName,
  f: Features,
  ingest?: IngestStatus | null,
  s?: Settings | null,
): StatusLine[] {
  if (!f[name]) return [];
  const rx = (k: ReceiverStatus['kind']) => ingest?.receivers.find((r) => r.kind === k);
  switch (name) {
    case 'traps':
      return [receiverLine(rx('trap'), 'UDP')].filter((x): x is StatusLine => !!x);
    case 'syslog':
      return [receiverLine(rx('syslog_udp'), 'UDP'), receiverLine(rx('syslog_tcp'), 'TCP')].filter(
        (x): x is StatusLine => !!x,
      );
    case 'siem_forwarding': {
      const fw = ingest?.forwarders ?? [];
      const bad = fw.filter((x) => x.state === 'error').length;
      if (bad) return [{ key: 'features.status.forwardErrors', params: { n: bad }, tone: 'error' }];
      const n = (s?.siem_targets ?? []).filter((x) => x.enabled).length;
      return [{ key: 'features.status.targets', params: { n }, tone: 'ok' }];
    }
    default:
      return [{ key: 'features.status.on', tone: 'ok' }];
  }
}

/** Settings tabs (`/settings?tab=…`). */
export const SETTINGS_TABS = [
  'general',
  'features',
  'ingest',
  'siem',
  'ldap',
  'sso', // [3]
  'backup',
  'flows',
  'newDevices',
  'reports',
  'noc',
  'chatAck',
  'audit',
  'assistant', // [3b]
] as const;
export type SettingsTab = (typeof SETTINGS_TABS)[number];

/**
 * Owner decision: a feature that has a settings tab of its own is turned on/off at the top of that
 * tab (and only there); the others stay on the Features tab.
 */
export const FEATURE_TABS: Partial<Record<FeatureName, SettingsTab>> = {
  traps: 'ingest',
  syslog: 'ingest',
  event_alerts: 'ingest',
  flapping: 'ingest',
  siem_forwarding: 'siem',
  ldap: 'ldap',
  audit: 'audit',
  // [3]
  sso_kerberos: 'sso',
  sso_oidc: 'sso',
  config_backup: 'backup',
  flows: 'flows',
  new_device_alerts: 'newDevices',
  reports: 'reports',
  noc: 'noc',
  chat_ack: 'chatAck',
  // [3b] every assistant toggle lives in Settings → Assistant (configure-first + capabilities)
  assistant: 'assistant',
  ai_explain_alert: 'assistant',
  ai_correlation: 'assistant',
  ai_alert_notes: 'assistant',
  ai_config_diff: 'assistant',
  ai_syslog_digest: 'assistant',
  ai_rule_drafts: 'assistant',
  ai_template_drafts: 'assistant',
  ai_knowledge: 'assistant',
  ai_chat_ask: 'assistant',
  ai_report_summary: 'assistant',
};

/** The settings tab holding the feature's toggle. */
export function featureTab(name: FeatureName): SettingsTab {
  return FEATURE_TABS[name] ?? 'features';
}

/** Features toggled on the given tab, in FEATURE_NAMES order. */
export function featuresOfTab(tab: SettingsTab): FeatureName[] {
  return FEATURE_NAMES.filter((n) => featureTab(n) === tab);
}

/** Features toggled in a tab of their own (the read-only overview on the Features tab). */
export function movedFeatures(): FeatureName[] {
  return FEATURE_NAMES.filter((n) => featureTab(n) !== 'features');
}
