// [1.3.1] Settings search (architecture.md, Settings page): a declarative index of the settings
// of every Settings tab. Entries hold i18n keys only; the texts are resolved in the UI language
// (plus English) at search time. Cards and some fields carry `data-anchor` attributes in the DOM
// so a result can be scrolled to without rendering hidden tabs.
import type { FeatureName, Features } from '../api/types';
import { FEATURE_NAMES } from '../api/types';
import { featureTab, type SettingsTab } from './features';

export type SettingsFieldDef = {
  labelKey: string;
  hintKey?: string;
  /** More i18n texts that should match (option names, a section heading, …). */
  extraKeys?: string[];
  /** Language-neutral search terms (technical words people type in English). */
  terms?: string[];
  /** `data-anchor` of the field, when it has one; else the card is scrolled to. */
  anchor?: string;
};

export type SettingsCardDef = {
  /** `data-anchor="settings-<id>"` on the card. */
  id: string;
  tab: SettingsTab;
  titleKey: string;
  hintKey?: string;
  /** The card is rendered only while one of these features is on. */
  feature?: FeatureName[];
  terms?: string[];
  fields: SettingsFieldDef[];
};

export function cardAnchor(id: string): string {
  return `settings-${id}`;
}

export const SETTINGS_CARDS: SettingsCardDef[] = [
  // --- General
  {
    id: 'general',
    tab: 'general',
    titleKey: 'settings.general',
    fields: [
      { labelKey: 'settings.retentionDays', hintKey: 'settings.retentionHint' },
      { labelKey: 'settings.eventRetentionDays' },
      { labelKey: 'settings.defaultInterval', hintKey: 'settings.defaultIntervalHint' },
      { labelKey: 'settings.defaultLanguage', hintKey: 'settings.defaultLanguageHint' },
      { labelKey: 'settings.publicBaseUrl', hintKey: 'settings.publicBaseUrlHint', terms: ['url'] },
      { labelKey: 'settings.timezone', hintKey: 'settings.timezoneHint' },
      {
        labelKey: 'settings.topologyRefresh',
        hintKey: 'settings.topologyRefreshHint',
        terms: ['lldp', 'cdp'],
      },
    ],
  },
  {
    id: 'ports',
    tab: 'general',
    titleKey: 'settings.ports.title',
    hintKey: 'settings.ports.intro',
    fields: [
      { labelKey: 'settings.ports.interval', hintKey: 'settings.ports.intervalHint' },
      {
        labelKey: 'settings.ports.threshold',
        hintKey: 'settings.ports.thresholdHint',
        terms: ['uplink', 'mac'],
      },
      { labelKey: 'settings.ports.retention', hintKey: 'settings.ports.retentionHint' },
      {
        labelKey: 'settings.ports.reverseDns',
        hintKey: 'settings.ports.reverseDnsHint',
        terms: ['dns', 'ptr'],
      },
    ],
  },
  {
    id: 'smtp',
    tab: 'general',
    titleKey: 'settings.smtp',
    terms: ['smtp', 'email', 'mail'],
    fields: [
      { labelKey: 'settings.smtpHost' },
      { labelKey: 'settings.smtpPort' },
      { labelKey: 'settings.smtpSecurity', terms: ['tls', 'ssl', 'starttls'] },
      { labelKey: 'settings.smtpUsername' },
      { labelKey: 'settings.smtpPassword' },
      { labelKey: 'settings.fromAddress' },
      { labelKey: 'settings.fromName' },
      { labelKey: 'settings.smtpTest', hintKey: 'settings.smtpTestHint' },
    ],
  },
  // --- Traps & syslog
  {
    id: 'ingest',
    tab: 'ingest',
    titleKey: 'ingestSettings.title',
    feature: ['traps', 'syslog'],
    terms: ['trap', 'syslog'],
    fields: [
      { labelKey: 'ingestSettings.trapRetention' },
      { labelKey: 'ingestSettings.syslogRetention' },
      {
        labelKey: 'ingestSettings.unknownPolicy',
        extraKeys: [
          'ingestSettings.unknown.store',
          'ingestSettings.unknown.drop',
          'ingestSettings.unknownHint.store',
          'ingestSettings.unknownHint.drop',
        ],
      },
      {
        labelKey: 'ingestSettings.communityCheck',
        extraKeys: [
          'ingestSettings.trapAuth',
          'ingestSettings.check.any',
          'ingestSettings.check.known',
        ],
        terms: ['community', 'snmp'],
      },
      { labelKey: 'ingestSettings.communities', terms: ['community', 'snmp'] },
      {
        labelKey: 'ingestSettings.v3.title',
        hintKey: 'ingestSettings.v3.hint',
        terms: ['snmpv3', 'snmp', 'v3'],
      },
      { labelKey: 'ingestSettings.ignored', hintKey: 'ingestSettings.ignoredHint' },
      {
        labelKey: 'ingestSettings.linkScope.label',
        extraKeys: [
          'ingestSettings.linkScope.title',
          'ingestSettings.linkScope.important',
          'ingestSettings.linkScope.importantHint',
          'ingestSettings.linkScope.all',
          'ingestSettings.linkScope.allHint',
        ],
        terms: ['link down', 'linkdown', 'link alerts', 'uplink', 'lag'],
        anchor: 'setting-link-scope',
      },
      {
        labelKey: 'ingestSettings.trunks.include',
        hintKey: 'ingestSettings.trunks.includeHint',
        extraKeys: ['ingestSettings.linkScope.title'],
        terms: ['trunk', 'link down', 'link alerts'],
        anchor: 'setting-trunk-scope',
      },
      {
        labelKey: 'ingestSettings.trunks.minTagged',
        hintKey: 'ingestSettings.trunks.minTaggedHint',
        extraKeys: ['ingestSettings.linkScope.title'],
        terms: ['trunk', 'vlan', 'tagged'],
        anchor: 'setting-trunk-scope',
      },
    ],
  },
  {
    id: 'trapCollapse',
    tab: 'ingest',
    titleKey: 'trapCollapse.title',
    hintKey: 'trapCollapse.intro',
    feature: ['traps'],
    fields: [
      { labelKey: 'trapCollapse.enabled' },
      { labelKey: 'trapCollapse.keep' },
      { labelKey: 'trapCollapse.oids', hintKey: 'trapCollapse.oidsHint', terms: ['oid'] },
    ],
  },
  {
    id: 'flapping',
    tab: 'ingest',
    titleKey: 'flapping.title',
    feature: ['flapping'],
    terms: ['flap'],
    fields: [
      { labelKey: 'flapping.threshold' },
      { labelKey: 'flapping.window' },
      { labelKey: 'flapping.quiet' },
    ],
  },
  // --- SIEM forwarding
  {
    id: 'siem',
    tab: 'siem',
    titleKey: 'siem.title',
    hintKey: 'siem.intro',
    feature: ['siem_forwarding'],
    terms: ['siem', 'syslog', 'rfc 5424'],
    fields: [
      { labelKey: 'siem.add' },
      { labelKey: 'siem.protocol', terms: ['udp', 'tcp', 'tls'] },
      { labelKey: 'siem.host' },
      { labelKey: 'siem.kinds' },
      { labelKey: 'siem.minSeverity', hintKey: 'siem.minSeverityHint' },
      { labelKey: 'siem.sources', hintKey: 'siem.sourcesHint' },
      { labelKey: 'siem.tlsVerify', terms: ['tls', 'certificate'] },
      { labelKey: 'siem.includeUnknown' },
    ],
  },
  // --- Active Directory
  {
    id: 'ldap',
    tab: 'ldap',
    titleKey: 'ldap.title',
    hintKey: 'ldap.intro',
    terms: ['ldap', 'ad', 'active directory'],
    fields: [
      { labelKey: 'ldap.domain', hintKey: 'ldap.domainHint' },
      { labelKey: 'ldap.netbios', hintKey: 'ldap.netbiosHint' },
      { labelKey: 'ldap.servers', hintKey: 'ldap.serversHint', terms: ['dc'] },
      { labelKey: 'ldap.security', terms: ['ldaps', 'starttls', 'tls'] },
      { labelKey: 'ldap.tlsVerify', terms: ['certificate'] },
      { labelKey: 'ldap.timeout' },
      { labelKey: 'ldap.baseDn', terms: ['dn'] },
      { labelKey: 'ldap.bindDn', hintKey: 'ldap.bindDnHint', terms: ['bind'] },
      { labelKey: 'ldap.bindPassword', hintKey: 'ldap.bindPasswordHint' },
      { labelKey: 'ldap.adminGroup', hintKey: 'ldap.groupsHint' },
      { labelKey: 'ldap.viewerGroup', hintKey: 'ldap.groupsHint' },
      { labelKey: 'ldap.testConnection', hintKey: 'ldap.testHint' },
    ],
  },
  {
    id: 'ldapTest',
    tab: 'ldap',
    titleKey: 'ldap.testUserTitle',
    terms: ['ldap'],
    fields: [{ labelKey: 'ldap.testUser', hintKey: 'ldap.testUserHint' }],
  },
  // --- Single sign-on (configure-first: the cards are always shown)
  {
    id: 'kerberos',
    tab: 'sso',
    titleKey: 'kerberos.title',
    hintKey: 'kerberos.intro',
    terms: ['kerberos', 'sso', 'spnego'],
    fields: [
      { labelKey: 'kerberos.realms', hintKey: 'kerberos.realmsHint' },
      { labelKey: 'kerberos.autoLogin' },
      { labelKey: 'kerberos.keytab', extraKeys: ['kerberos.upload'], terms: ['keytab'] },
    ],
  },
  {
    id: 'oidc',
    tab: 'sso',
    titleKey: 'oidc.title',
    hintKey: 'oidc.intro',
    terms: ['oidc', 'openid', 'entra', 'azure', 'sso', 'microsoft'],
    fields: [
      { labelKey: 'oidc.tenant' },
      { labelKey: 'oidc.client' },
      { labelKey: 'oidc.secret' },
      { labelKey: 'oidc.secretExpires', hintKey: 'oidc.secretExpiresHint' },
      { labelKey: 'oidc.roleSource' },
      { labelKey: 'oidc.buttonLabel', hintKey: 'oidc.buttonLabelHint' },
      { labelKey: 'oidc.adminValues' },
      { labelKey: 'oidc.viewerValues' },
      { labelKey: 'oidc.redirectUri', hintKey: 'oidc.redirectUriHint' },
      { labelKey: 'oidc.test' },
    ],
  },
  // --- Backups
  {
    id: 'backup',
    tab: 'backup',
    titleKey: 'settings3.backupTitle',
    feature: ['config_backup'],
    terms: ['backup', 'config', 'ssh'],
    fields: [
      {
        labelKey: 'settings3.config_backup_time',
        hintKey: 'settings3.config_backup_timeHint',
      },
      { labelKey: 'settings3.config_backup_window_min' },
      { labelKey: 'settings3.config_backup_concurrency' },
      { labelKey: 'settings3.config_backup_timeout_s' },
      { labelKey: 'settings3.config_backup_max_versions' },
      { labelKey: 'settings3.config_backup_min_versions' },
      { labelKey: 'settings3.config_backup_retention_days' },
      { labelKey: 'settings3.config_backup_fail_threshold' },
      { labelKey: 'settings3.config_change_clear_min' },
      { labelKey: 'settings3.config_backup_mask_secrets' },
      { labelKey: 'settings3.config_change_alert' },
    ],
  },
  // --- Flows
  {
    id: 'flows',
    tab: 'flows',
    titleKey: 'settings3.flowsTitle',
    feature: ['flows'],
    terms: ['netflow', 'sflow', 'ipfix', 'flow'],
    fields: [
      { labelKey: 'settings3.flow_max_records_per_s' },
      { labelKey: 'settings3.flow_top_conversations' },
      { labelKey: 'settings3.flow_top_hosts' },
      { labelKey: 'settings3.flow_top_apps' },
      { labelKey: 'settings3.flow_raw_retention_hours' },
      { labelKey: 'settings3.flow_minute_retention_hours' },
      { labelKey: 'settings3.flow_rollup_retention_days' },
      { labelKey: 'settings3.flow_app_overrides', hintKey: 'settings3.overridesHint' },
    ],
  },
  // --- New devices
  {
    id: 'newDevices',
    tab: 'newDevices',
    titleKey: 'settings3.newdevTitle',
    feature: ['new_device_alerts'],
    terms: ['mac'],
    fields: [
      { labelKey: 'settings3.newdev_learning_days' },
      { labelKey: 'settings3.newdev_condition_clear_min' },
      { labelKey: 'settings3.newdev_forget_after_days' },
      { labelKey: 'settings3.newdev_ignore_random_macs', terms: ['mac', 'random'] },
      { labelKey: 'newDevices.scope', hintKey: 'newDevices.scopeHint' },
    ],
  },
  // --- Reports
  {
    id: 'reports',
    tab: 'reports',
    titleKey: 'settings3.reportsTitle',
    feature: ['reports'],
    fields: [{ labelKey: 'settings3.report_retention_days' }],
  },
  // --- NOC screens
  {
    id: 'noc',
    tab: 'noc',
    titleKey: 'kiosk.title',
    hintKey: 'kiosk.intro',
    feature: ['noc'],
    terms: ['noc', 'kiosk', 'wall'],
    fields: [
      { labelKey: 'kiosk.add' },
      { labelKey: 'kiosk.sources', hintKey: 'kiosk.sourcesHint' },
      { labelKey: 'kiosk.widgets', hintKey: 'kiosk.widgetsHint' },
      { labelKey: 'kiosk.refresh', hintKey: 'kiosk.refreshHint' },
      { labelKey: 'kiosk.showAddresses' },
      { labelKey: 'kiosk.regenerate' },
    ],
  },
  // --- Chat acknowledge
  {
    id: 'chatAck',
    tab: 'chatAck',
    titleKey: 'settings3.chatAckTitle',
    feature: ['chat_ack'],
    terms: ['telegram', 'teams', 'ack'],
    fields: [
      { labelKey: 'settings3.chat_ack_ttl_h' },
      { labelKey: 'settings3.telegram_poll_timeout_s', terms: ['telegram'] },
    ],
  },
  {
    id: 'chatAckPollers',
    tab: 'chatAck',
    titleKey: 'chatAck.pollers',
    feature: ['chat_ack'],
    terms: ['telegram'],
    fields: [],
  },
  {
    id: 'chatAckLinks',
    tab: 'chatAck',
    titleKey: 'chatAck.allLinks',
    feature: ['chat_ack'],
    terms: ['telegram'],
    fields: [],
  },
  // --- Audit
  {
    id: 'audit',
    tab: 'audit',
    titleKey: 'audit.settingsTitle',
    feature: ['audit'],
    fields: [{ labelKey: 'audit.retention', hintKey: 'audit.retentionHint' }],
  },
];

/** Card title of the place where a feature's switch is (see FeatureToggleCard / inline switches). */
export function toggleCardTitleKey(name: FeatureName): string {
  switch (name) {
    case 'ldap':
      return 'ldap.title';
    case 'sso_kerberos':
      return 'kerberos.title';
    case 'sso_oidc':
      return 'oidc.title';
  }
  const tab = featureTab(name);
  if (tab === 'features') return 'features.title';
  return FEATURE_NAMES.filter((n) => featureTab(n) === tab).length > 1
    ? 'features.toggleMany'
    : 'features.toggleOne';
}

export type SettingsEntry = {
  id: string;
  tab: SettingsTab;
  cardTitleKey: string;
  labelKey: string;
  hintKey?: string;
  extraKeys: string[];
  terms: string[];
  feature?: FeatureName[];
  /** `data-anchor` / `data-feature` selectors to try in order. */
  selectors: string[];
};

const anchorSel = (a: string) => `[data-anchor="${a}"]`;
export const featureSel = (n: FeatureName) => `[data-feature="${n}"]`;

/** All searchable entries: every card, its fields, and every feature switch. */
export function settingsEntries(): SettingsEntry[] {
  const out: SettingsEntry[] = [];
  for (const c of SETTINGS_CARDS) {
    const card = anchorSel(cardAnchor(c.id));
    out.push({
      id: `${c.id}`,
      tab: c.tab,
      cardTitleKey: c.titleKey,
      labelKey: c.titleKey,
      hintKey: c.hintKey,
      extraKeys: [],
      terms: c.terms ?? [],
      feature: c.feature,
      selectors: [card],
    });
    c.fields.forEach((f, i) =>
      out.push({
        id: `${c.id}.${i}`,
        tab: c.tab,
        cardTitleKey: c.titleKey,
        labelKey: f.labelKey,
        hintKey: f.hintKey,
        extraKeys: f.extraKeys ?? [],
        terms: [...(f.terms ?? []), ...(c.terms ?? [])],
        feature: c.feature,
        selectors: f.anchor ? [anchorSel(f.anchor), card] : [card],
      }),
    );
  }
  for (const n of FEATURE_NAMES)
    out.push({
      id: `feature.${n}`,
      tab: featureTab(n),
      cardTitleKey: toggleCardTitleKey(n),
      labelKey: `features.name.${n}`,
      hintKey: `features.desc.${n}`,
      extraKeys: [],
      terms: [n.replace(/_/g, ' ')],
      selectors: [featureSel(n)],
    });
  return out;
}

/** Every i18n key the index uses (for the drift test). */
export function settingsIndexKeys(): string[] {
  const keys = new Set<string>();
  for (const e of settingsEntries()) {
    keys.add(e.cardTitleKey);
    keys.add(e.labelKey);
    if (e.hintKey) keys.add(e.hintKey);
    e.extraKeys.forEach((k) => keys.add(k));
    keys.add(`settings.tab.${e.tab}`);
  }
  return [...keys];
}

// ---------------------------------------------------------------- matching

type T = (key: string) => string;

export type SettingsDoc = {
  entry: SettingsEntry;
  tab: string;
  card: string;
  label: string;
  /** Lower-cased texts by weight: label, extra, hint, card, tab (current language + English). */
  hay: { text: string; w: number }[];
};

const norm = (s: string) => s.toLocaleLowerCase();

/** Resolves the entries' texts with `t` (UI language) and, when given, `en` (English). */
export function buildDocs(entries: SettingsEntry[], t: T, en?: T): SettingsDoc[] {
  const ts = en ? [t, en] : [t];
  return entries.map((e) => {
    const hay: SettingsDoc['hay'] = [];
    const add = (key: string | undefined, w: number) => {
      if (!key) return;
      for (const f of ts) hay.push({ text: norm(f(key)), w });
    };
    add(e.labelKey, 8);
    e.extraKeys.forEach((k) => add(k, 4));
    e.terms.forEach((x) => hay.push({ text: norm(x), w: 4 }));
    add(e.hintKey, 2);
    add(e.cardTitleKey, 1.5);
    add(`settings.tab.${e.tab}`, 1);
    return {
      entry: e,
      tab: t(`settings.tab.${e.tab}`),
      card: t(e.cardTitleKey),
      label: t(e.labelKey),
      hay,
    };
  });
}

/**
 * Case-insensitive search: every word of the query must occur in one of the entry's texts.
 * Results are ranked by where the words matched (label first), then by index order.
 */
export function searchSettings(docs: SettingsDoc[], query: string, limit = 30): SettingsDoc[] {
  const words = norm(query).trim().split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  const scored: { d: SettingsDoc; s: number; i: number }[] = [];
  docs.forEach((d, i) => {
    let s = 0;
    for (const w of words) {
      let best = 0;
      for (const h of d.hay) if (h.w > best && h.text.includes(w)) best = h.w;
      if (!best) return;
      s += best;
    }
    if (norm(d.label).startsWith(words[0])) s += 1;
    scored.push({ d, s, i });
  });
  scored.sort((a, b) => b.s - a.s || a.i - b.i);
  return scored.slice(0, limit).map((x) => x.d);
}

/** Whether the entry's card is rendered with these features. */
export function entryAvailable(e: SettingsEntry, f: Features): boolean {
  return !e.feature || e.feature.some((n) => f[n]);
}
