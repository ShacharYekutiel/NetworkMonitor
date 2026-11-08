import type { TFunction } from 'i18next';
import type {
  ApplyTemplateResult,
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
const LTR_RUN = /[/\\^]?[A-Za-z0-9][^\s\u0590-\u05FF()]*/g;

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
};

/** Human vendor name from a vendor slug (data-model.md vendor table). */
export function vendorLabel(slug: string | null | undefined): string {
  if (!slug) return '';
  return VENDOR_LABELS[slug] ?? slug.charAt(0).toUpperCase() + slug.slice(1);
}

/** i18n key of a skip reason; unknown reasons fall back to the generic text. */
export function skipReasonKey(reason: string): string {
  return ['exists', 'probe_failed', 'snmp_error', 'max_reached', 'no_rows'].includes(reason)
    ? `templates.skip.${reason}`
    : 'templates.skip.other';
}

/** Skipped entries grouped by reason, in a stable order (most actionable first). */
export function groupSkipped(
  skipped: SkippedSensor[],
): { reason: string; items: SkippedSensor[] }[] {
  const order = ['snmp_error', 'probe_failed', 'no_rows', 'max_reached', 'exists'];
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
