// [1.3.1] Settings search index: guards against drift between the index, the locales and the tabs.
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import type { Features } from '../api/types';
import { FEATURE_NAMES } from '../api/types';
import en from '../i18n/locales/en.json';
import he from '../i18n/locales/he.json';
import { SETTINGS_TABS } from './features';
import {
  buildDocs,
  entryAvailable,
  SETTINGS_CARDS,
  searchSettings,
  settingsEntries,
  settingsIndexKeys,
} from './settingsIndex';

type Tree = { [k: string]: string | Tree };
function lookup(obj: Tree, key: string): string | undefined {
  let cur: string | Tree | undefined = obj;
  for (const part of key.split('.')) {
    if (!cur || typeof cur === 'string') return undefined;
    cur = cur[part];
  }
  return typeof cur === 'string' ? cur : undefined;
}
function sources(dir: string): string {
  let out = '';
  for (const f of readdirSync(dir)) {
    const p = join(dir, f);
    if (statSync(p).isDirectory()) out += sources(p);
    else if (f.endsWith('.tsx') && !f.endsWith('.test.tsx')) out += readFileSync(p, 'utf8');
  }
  return out;
}
const tEn = (k: string) => lookup(en as Tree, k) ?? k;
const tHe = (k: string) => lookup(he as Tree, k) ?? k;

describe('[1.3.1] settings search index', () => {
  it('every key exists in en.json and he.json, without placeholders', () => {
    const keys = settingsIndexKeys();
    expect(keys.length).toBeGreaterThan(100);
    for (const k of keys) {
      expect(lookup(en as Tree, k), `en: ${k}`).toBeTypeOf('string');
      expect(lookup(he as Tree, k), `he: ${k}`).toBeTypeOf('string');
      expect(lookup(en as Tree, k), `placeholder in ${k}`).not.toContain('{{');
    }
  });

  it('every tab of the index is a Settings tab, and every Settings tab is indexed', () => {
    const tabs = new Set(settingsEntries().map((e) => e.tab));
    for (const tab of tabs) expect(SETTINGS_TABS as readonly string[]).toContain(tab);
    for (const tab of SETTINGS_TABS) expect(tabs.has(tab), tab).toBe(true);
  });

  it('every card and field anchor of the index is rendered somewhere', () => {
    const src = sources(resolve(__dirname, '..'));
    for (const c of SETTINGS_CARDS) {
      expect(
        src.includes(`cardAnchor('${c.id}')`) || src.includes(`anchor="${c.id}"`),
        `card anchor ${c.id}`,
      ).toBe(true);
      for (const f of c.fields)
        if (f.anchor) expect(src.includes(`data-anchor="${f.anchor}"`), f.anchor).toBe(true);
    }
  });

  it('card ids and entry ids are unique', () => {
    const ids = SETTINGS_CARDS.map((c) => c.id);
    expect(new Set(ids).size).toBe(ids.length);
    const eids = settingsEntries().map((e) => e.id);
    expect(new Set(eids).size).toBe(eids.length);
  });

  it('"trunk", "link" and "קישור" find the trunk / link-scope settings of Traps & syslog', () => {
    const enDocs = buildDocs(settingsEntries(), tEn);
    const heDocs = buildDocs(settingsEntries(), tHe, tEn);
    const labels = (docs: typeof enDocs, q: string) =>
      searchSettings(docs, q).map((d) => d.entry.labelKey);
    expect(labels(enDocs, 'TRUNK').slice(0, 2).sort()).toEqual([
      'ingestSettings.trunks.include',
      'ingestSettings.trunks.minTagged',
    ]);
    expect(labels(enDocs, 'link')).toEqual(
      expect.arrayContaining(['ingestSettings.linkScope.label', 'ingestSettings.trunks.include']),
    );
    // Hebrew UI: Hebrew words and English technical words both match.
    expect(labels(heDocs, 'קישור')).toEqual(
      expect.arrayContaining(['ingestSettings.linkScope.label', 'ingestSettings.trunks.include']),
    );
    expect(labels(heDocs, 'link alerts')).toContain('ingestSettings.trunks.include');
    expect(labels(heDocs, 'trunk').slice(0, 2)).toContain('ingestSettings.trunks.include');
    const r = searchSettings(heDocs, 'trunk').find(
      (d) => d.entry.labelKey === 'ingestSettings.trunks.include',
    )!;
    expect(r.tab).toBe('מלכודות ו-Syslog');
    expect(r.card).toBe('מלכודות ו-Syslog');
    expect(r.label).toBe('כלול את כל פורטי ה-trunk');
  });

  it('all words must match; no match gives no results', () => {
    const docs = buildDocs(settingsEntries(), tEn);
    expect(searchSettings(docs, '')).toEqual([]);
    expect(searchSettings(docs, 'zzzz-nothing')).toEqual([]);
    const r = searchSettings(docs, 'smtp port');
    expect(r[0].entry.labelKey).toBe('settings.smtpPort');
    expect(searchSettings(docs, 'ldap').some((d) => d.entry.tab === 'ldap')).toBe(true);
  });

  it('[3b] "Bedrock", "AI", "assistant" and "עוזר" find the Assistant settings', () => {
    const enDocs = buildDocs(settingsEntries(), tEn);
    const heDocs = buildDocs(settingsEntries(), tHe, tEn);
    const ids = (docs: typeof enDocs, q: string) =>
      searchSettings(docs, q, 200).map((d) => d.entry.id);
    expect(ids(enDocs, 'bedrock')).toEqual(
      expect.arrayContaining(['assistantConnection', 'assistantKb', 'feature.assistant']),
    );
    expect(ids(enDocs, 'AI')).toEqual(
      expect.arrayContaining(['assistantCapabilities', 'aiCorrelation', 'feature.ai_alert_notes']),
    );
    expect(ids(enDocs, 'assistant').length).toBeGreaterThan(10);
    expect(ids(enDocs, 'light model')[0]).toBe('assistantConnection.2');
    expect(ids(enDocs, 'token cap')).toContain('assistantLimits.1');
    const he = ids(heDocs, 'עוזר');
    expect(he).toEqual(
      expect.arrayContaining(['assistantConnection', 'assistantLimits', 'feature.assistant']),
    );
    expect(ids(heDocs, 'bedrock')).toContain('assistantConnection');
    // per-feature settings require their AI feature; the switch lives in Capabilities
    const corr = settingsEntries().find((e) => e.id === 'aiCorrelation.0')!;
    expect(corr.feature).toEqual(['ai_correlation']);
    const off = Object.fromEntries(FEATURE_NAMES.map((n) => [n, false])) as Features;
    expect(entryAvailable(corr, off)).toBe(false);
    expect(entryAvailable(corr, { ...off, ai_correlation: true })).toBe(true);
    const conn = settingsEntries().find((e) => e.id === 'assistantConnection.0')!;
    expect(entryAvailable(conn, off)).toBe(true); // configure-first
    const sw = settingsEntries().find((e) => e.id === 'feature.ai_correlation')!;
    expect(sw.tab).toBe('assistant');
    expect(sw.cardTitleKey).toBe('assistant.settings.capabilities');
  });

  it("[4] the AWS tab and the assistant's AWS access are found in English and Hebrew", () => {
    const enDocs = buildDocs(settingsEntries(), tEn);
    const heDocs = buildDocs(settingsEntries(), tHe, tEn);
    const ids = (docs: typeof enDocs, q: string) =>
      searchSettings(docs, q, 200).map((d) => d.entry.id);
    for (const id of ['awsConnections', 'awsConnection', 'awsCertificate', 'awsPolicies'])
      expect(settingsEntries().find((e) => e.id === id)?.tab).toBe('aws');
    // the editor / certificate / policies exist only while a connection is open: their
    // results fall back to the list card
    const ta = settingsEntries().find((e) => e.id === 'awsConnection.2')!;
    expect(ta.labelKey).toBe('aws.form.raTrustAnchor');
    expect(ta.selectors).toEqual([
      '[data-anchor="settings-awsConnection"]',
      '[data-anchor="settings-awsConnections"]',
    ]);
    expect(ids(enDocs, 'trust anchor')[0]).toBe('awsConnection.2');
    expect(ids(heDocs, 'trust anchor')).toContain('awsConnection.2');
    expect(ids(heDocs, 'עוגן האמון')).toContain('awsConnection.2');
    expect(ids(enDocs, 'ExternalId')).toContain('assistantAwsAccess.3');
    expect(ids(heDocs, 'external id')).toContain('assistantAwsAccess.3');
    expect(ids(enDocs, 'certreq')).toContain('awsCertificate');
    expect(ids(enDocs, 'setsourceidentity')).toContain('awsPolicies');
    // the 1.4.x words now lead to the AWS cards, not to the assistant connection card
    expect(ids(enDocs, 'roles anywhere')).toEqual(expect.arrayContaining(['awsConnection']));
    expect(ids(enDocs, 'roles anywhere')).not.toContain('assistantConnection');
    expect(ids(enDocs, 'certificate')).toContain('awsCertificate');
    // always available (no feature)
    const off = Object.fromEntries(FEATURE_NAMES.map((n) => [n, false])) as Features;
    expect(entryAvailable(ta, off)).toBe(true);
  });
});
