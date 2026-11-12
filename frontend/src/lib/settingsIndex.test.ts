// [1.3.1] Settings search index: guards against drift between the index, the locales and the tabs.
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import en from '../i18n/locales/en.json';
import he from '../i18n/locales/he.json';
import { SETTINGS_TABS } from './features';
import {
  buildDocs,
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
});
