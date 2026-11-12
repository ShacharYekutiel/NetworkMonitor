import type { TFunction } from 'i18next';
import { SKIP_REASONS } from '../api/types';
import type {
  Confidence,
  SkipReason,
  TemplateFilter,
  ApplyTemplateResult,
  ApplyInterface,
  ApplyInterfacesMode,
  ApplyTemplateRequest,
  DeviceTemplate,
  DiscoveryResult,
  ImportItem,
  ImportResponse,
  L10n,
  SkippedSensor,
  TemplateEntry,
  TemplateMatchRule,
} from '../api/types';

const HEBREW = /[\u0590-\u05FF]/;
// A Latin/number token, or several separated by single spaces ("Check Point", "enterprise 2620").
const LTR_RUN = /[/\\^]?[A-Za-z0-9][^\s\u0590-\u05FF()]*(?: [A-Za-z0-9][^\s\u0590-\u05FF()]*)*/g;

/**
 * In Hebrew text, wraps Latin/number tokens (paths like "/volume1", "C:\", regexes like "^eth0$")
 * in Unicode LTR isolates so their neutral characters stay attached to them.
 */
export function isolateLtrRuns(s: string): string {
  if (!HEBREW.test(s)) return s;
  return s.replace(LTR_RUN, (m) => `\u2066${m}\u2069`);
}

export function l10n(v: L10n | null | undefined, lang: string): string {
  if (!v) return '';
  return (lang === 'he' ? v.he : v.en) || v.en || v.he || '';
}

export function templateName(
  templates: DeviceTemplate[] | undefined,
  id: string,
  lang: string,
): string {
  const tpl = templates?.find((x) => x.id === id);
  return tpl ? l10n(tpl.name, lang) : id;
}

/** One line of text per template entry ("what it creates"). */
export function describeEntry(e: TemplateEntry, lang: string, t: TFunction): string {
  return isolateLtrRuns(describeEntryRaw(e, lang, t));
}

function describeEntryRaw(e: TemplateEntry, lang: string, t: TFunction): string {
  switch (e.kind) {
    case 'sensor': {
      const name = l10n(e.name, lang);
      const type = t(`sensorType.${e.type}`);
      const base = name && name !== type ? `${name} (${type})` : type;
      return e.probe ? `${base} — ${t('templates.ifAvailable')}` : base;
    }
    case 'snmp_interfaces': {
      const key = e.oper_up_only ? 'templates.entryInterfacesUp' : 'templates.entryInterfaces';
      let s = t(key, { max: e.max });
      if (e.include_name_regex) s += ` — ${t('templates.matching', { re: e.include_name_regex })}`;
      return s;
    }
    case 'snmp_table': {
      let s = t('templates.entryTable', {
        name: l10n(e.name, lang).replace('{label}', '…'),
        max: e.max,
      });
      // [2b-1] enum status columns (fans/PSUs) are shown as such.
      if (e.config?.value_type === 'enum') s += ` — ${t('templates.entryEnum')}`;
      return s;
    }
  }
}

/** A short text for a template's match rules. */
export function describeMatch(rules: TemplateMatchRule[], t: TFunction): string {
  if (!rules.length) return t('templates.matchNone');
  const parts = rules.map((r) => {
    const p: string[] = [];
    if (r.requires_snmp) p.push(t('templates.matchSnmp'));
    if (r.device_classes?.length)
      p.push(r.device_classes.map((c) => t(`deviceClass.${c}`)).join(' / '));
    if (r.vendors?.length) p.push(r.vendors.map(vendorLabel).join(' / '));
    if (r.any_open_ports?.length)
      p.push(t('templates.matchPorts', { ports: r.any_open_ports.join(', ') }));
    if (r.enterprises?.length)
      p.push(t('templates.matchEnterprises', { list: r.enterprises.join(', ') }));
    if (r.sys_object_id_prefixes?.length)
      p.push(t('templates.matchOidPrefixes', { list: r.sys_object_id_prefixes.join(', ') }));
    if (r.sys_descr_regex) p.push(t('templates.matchDescr', { re: r.sys_descr_regex }));
    return p.length ? p.join(' + ') : t('templates.matchAny');
  });
  return isolateLtrRuns(parts.join(t('templates.or')));
}

const VENDOR_LABELS: Record<string, string> = {
  hp: 'HP',
  h3c: 'H3C',
  aruba: 'Aruba',
  supermicro: 'Supermicro',
  'd-link': 'D-Link',
  apc: 'APC',
  'tp-link': 'TP-Link',
  opnsense: 'OPNsense',
  pfsense: 'pfSense',
  freebsd: 'FreeBSD',
  qnap: 'QNAP',
  paloalto: 'Palo Alto',
  mikrotik: 'MikroTik',
  zyxel: 'Zyxel',
  netgear: 'NETGEAR',
  ubiquiti: 'Ubiquiti',
  microsoft: 'Microsoft',
  // [2b-2] template library vendor folders
  hpe: 'HPE',
  checkpoint: 'Check Point',
  fortinet: 'Fortinet',
  cisco: 'Cisco',
  dell: 'Dell',
};

/** Human vendor name from a vendor slug (data-model.md vendor table). */
export function vendorLabel(slug: string | null | undefined): string {
  if (!slug) return '';
  return VENDOR_LABELS[slug] ?? slug.charAt(0).toUpperCase() + slug.slice(1);
}

/** i18n key of a skip reason; unknown reasons fall back to the generic text. */
export function skipReasonKey(reason: string): string {
  return SKIP_REASONS.includes(reason as SkipReason)
    ? `templates.skip.${reason}`
    : 'templates.skip.other';
}

/** Skipped entries grouped by reason, in a stable order (most actionable first). */
export function groupSkipped(
  skipped: SkippedSensor[],
): { reason: string; items: SkippedSensor[] }[] {
  const order = [
    'snmp_error',
    'probe_failed',
    'no_rows',
    'not_applicable',
    'max_reached',
    'exists',
  ];
  const map = new Map<string, SkippedSensor[]>();
  for (const s of skipped) map.set(s.reason, [...(map.get(s.reason) ?? []), s]);
  const rank = (r: string) => (order.includes(r) ? order.indexOf(r) : order.length);
  return [...map.entries()]
    .sort((a, b) => rank(a[0]) - rank(b[0]))
    .map(([reason, items]) => ({ reason, items }));
}

export function applySummary(r: ApplyTemplateResult): {
  created: number;
  skipped: number;
  existing: number;
} {
  return {
    created: r.created.length,
    skipped: r.skipped.filter((s) => s.reason !== 'exists').length,
    existing: r.skipped.filter((s) => s.reason === 'exists').length,
  };
}

export type ImportRow =
  | {
      kind: 'created';
      result_id: number;
      ip: string;
      device_id: number;
      device_name: string;
      template_id: string;
      sensors_created: number;
      sensors_skipped: SkippedSensor[];
    }
  | {
      kind: 'error';
      result_id: number;
      ip: string;
      code: string;
      message: string;
      device_id: number | null;
      /** Not imported for a transient reason (internal_error, incl. the 90 s budget): retry it. */
      retry: boolean;
      /** The backend's import time budget ran out before this host was processed. */
      timedOut: boolean;
    }
  | { kind: 'missing'; result_id: number; ip: string };

/** One row per requested item, in request order (the API keeps `items` order too). */
export function importRows(
  items: ImportItem[],
  res: ImportResponse,
  results: DiscoveryResult[],
): ImportRow[] {
  const ipOf = (id: number) => results.find((r) => r.id === id)?.ip ?? `#${id}`;
  return items.map((it): ImportRow => {
    const c = res.created.find((x) => x.result_id === it.result_id);
    if (c) return { kind: 'created', ...c, ip: ipOf(it.result_id) };
    const e = res.errors.find((x) => x.result_id === it.result_id);
    if (e)
      return {
        kind: 'error',
        result_id: e.result_id,
        ip: ipOf(e.result_id),
        code: e.code,
        message: e.message,
        device_id: e.device_id,
        retry: e.code === 'internal_error',
        timedOut: e.code === 'internal_error' && /time budget/i.test(e.message),
      };
    return { kind: 'missing', result_id: it.result_id, ip: ipOf(it.result_id) };
  });
}

/** Result ids worth retrying after an import response (transient errors and unanswered items). */
export function retryIds(rows: ImportRow[]): number[] {
  return rows
    .filter((r) => r.kind === 'missing' || (r.kind === 'error' && r.retry))
    .map((r) => r.result_id);
}

/** [2b-2] The entry's confidence (null for entries without a vendor OID). */
export function entryConfidence(e: TemplateEntry): Confidence | null {
  return e.confidence ?? null;
}

/** [2b-2] "1.3.6.1.4.1.2620.1.5.2.0 in 1" texts of a sensor entry's `when` conditions. */
export function describeWhen(filters: TemplateFilter[] | undefined, t: TFunction): string[] {
  return (filters ?? []).map((f) => {
    const parts: string[] = [];
    if (f.in?.length) parts.push(t('templates.whenIn', { list: f.in.join(', ') }));
    if (f.not_in?.length) parts.push(t('templates.whenNotIn', { list: f.not_in.join(', ') }));
    return parts.length ? `${f.oid} ${parts.join(' · ')}` : `${f.oid} ${t('templates.whenAny')}`;
  });
}

/** [2b-2] Templates grouped by vendor slug (sorted by label; generic / no vendor last). */
export function groupByVendor(
  templates: DeviceTemplate[],
): { vendor: string | null; items: DeviceTemplate[] }[] {
  const map = new Map<string | null, DeviceTemplate[]>();
  for (const tpl of templates) {
    // Built-in generic templates (vendor folder `generic`) join the ones without a vendor.
    const v = tpl.vendor && tpl.vendor !== 'generic' ? tpl.vendor : null;
    map.set(v, [...(map.get(v) ?? []), tpl]);
  }
  return [...map.entries()]
    .sort((a, b) => {
      if (a[0] === null) return 1;
      if (b[0] === null) return -1;
      return vendorLabel(a[0]).localeCompare(vendorLabel(b[0]));
    })
    .map(([vendor, items]) => ({ vendor, items }));
}

// ---------------------------------------------------------------------------
// [1.3] Apply template: which ports an `snmp_interfaces` entry covers

export type PortsChoice = ApplyInterfacesMode | 'choose';

/** True when the template creates interface sensors (the "Ports" section is shown). */
export function hasInterfacesEntry(tpl: Pick<DeviceTemplate, 'entries'> | undefined): boolean {
  return !!tpl?.entries.some((e) => e.kind === 'snmp_interfaces');
}

/** Initial "Choose ports" selection: the important ports plus those already monitored. */
export function initialPortPick(items: ApplyInterface[]): number[] {
  return items.filter((i) => i.important || i.has_sensor).map((i) => i.if_index);
}

/** if_index values the request would cover (null = every port). */
export function coveredPorts(
  items: ApplyInterface[],
  choice: PortsChoice,
  picked: ReadonlySet<number>,
): Set<number> | null {
  if (choice === 'all') return null;
  if (choice === 'none') return new Set();
  if (choice === 'important')
    return new Set(items.filter((i) => i.important).map((i) => i.if_index));
  return new Set(items.filter((i) => picked.has(i.if_index)).map((i) => i.if_index));
}

/** Interface sensors that "remove unselected" would delete. */
export function sensorsToRemove(
  items: ApplyInterface[],
  choice: PortsChoice,
  picked: ReadonlySet<number>,
): ApplyInterface[] {
  const keep = coveredPorts(items, choice, picked);
  if (keep === null) return [];
  return items.filter((i) => i.has_sensor && !keep.has(i.if_index));
}

/** Case-insensitive match on name and alias. */
export function filterInterfaces(items: ApplyInterface[], q: string): ApplyInterface[] {
  const s = q.trim().toLowerCase();
  if (!s) return items;
  return items.filter((i) => i.name.toLowerCase().includes(s) || i.alias.toLowerCase().includes(s));
}

/** Request options of POST apply-template for the chosen ports. */
export function applyOptions(
  choice: PortsChoice,
  picked: ReadonlySet<number>,
  removeUnselected: boolean,
): Omit<ApplyTemplateRequest, 'template_id'> {
  return {
    interfaces: choice === 'choose' ? [...picked].sort((a, b) => a - b) : choice,
    remove_unselected_interfaces: choice !== 'all' && removeUnselected,
  };
}
