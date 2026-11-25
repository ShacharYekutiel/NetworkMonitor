// [syslog-protection] Syslog filter rules: the editor form, its validation and the input body.
import type { SyslogFilter, SyslogFilterInput, TopTalker } from '../api/types';
import { parseSources } from './ingestSettings';

export type FilterForm = {
  name: string;
  enabled: boolean;
  sources: string; // IPs / CIDRs, one per line
  deviceId: number | null;
  program: string;
  programMatch: 'exact' | 'prefix';
  msgid: string;
  severityMin: number | null;
  contains: string;
  action: 'drop' | 'keep';
  keepN: string;
};

export function emptyFilterForm(): FilterForm {
  return {
    name: '',
    enabled: true,
    sources: '',
    deviceId: null,
    program: '',
    programMatch: 'exact',
    msgid: '',
    severityMin: null,
    contains: '',
    action: 'drop',
    keepN: '100',
  };
}

export function formFromFilter(f: SyslogFilter): FilterForm {
  return {
    name: f.name,
    enabled: f.enabled,
    sources: f.sources.join('\n'),
    deviceId: f.device_ids[0] ?? null,
    program: f.program,
    programMatch: f.program_match,
    msgid: f.msgid,
    severityMin: f.severity_min,
    contains: f.message_contains,
    action: f.action,
    keepN: String(f.keep_n),
  };
}

/** "Create a filter rule from this": the busiest (source, program, severity) combination. */
export function formFromTalker(t: TopTalker, name: string): FilterForm {
  return {
    ...emptyFilterForm(),
    name,
    sources: t.device_id === null ? t.source_ip : '',
    deviceId: t.device_id,
    program: t.program,
    severityMin: t.severity,
  };
}

export type FilterFormErrors = {
  name?: boolean;
  sources?: string[];
  keepN?: boolean;
  noCondition?: boolean;
};

export function filterFormErrors(f: FilterForm): FilterFormErrors {
  const e: FilterFormErrors = {};
  if (!f.name.trim() || f.name.trim().length > 100) e.name = true;
  const src = parseSources(f.sources);
  if (src.invalid.length) e.sources = src.invalid;
  const n = Number(f.keepN);
  if (f.action === 'keep' && (!Number.isInteger(n) || n < 1 || n > 100000)) e.keepN = true;
  const any =
    src.list.length > 0 ||
    f.deviceId !== null ||
    f.program.trim() !== '' ||
    f.msgid.trim() !== '' ||
    f.contains.trim() !== '' ||
    f.severityMin !== null;
  if (!any) e.noCondition = true;
  return e;
}

export function hasErrors(e: FilterFormErrors): boolean {
  return !!(e.name || e.sources?.length || e.keepN || e.noCondition);
}

export function filterInput(f: FilterForm): SyslogFilterInput {
  return {
    name: f.name.trim(),
    enabled: f.enabled,
    sources: parseSources(f.sources).list,
    device_ids: f.deviceId === null ? [] : [f.deviceId],
    program: f.program.trim(),
    program_match: f.programMatch,
    msgid: f.msgid.trim(),
    severity_min: f.severityMin,
    message_contains: f.contains.trim(),
    action: f.action,
    keep_n: f.action === 'keep' ? Number(f.keepN) : undefined,
  };
}
