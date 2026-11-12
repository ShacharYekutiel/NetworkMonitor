// [2b-3] Event rules: form ⇄ API conversion, validation (data-model § Event rules), ordering.
import type {
  EventRule,
  EventRuleAction,
  EventRuleInput,
  EventRuleKind,
  RuleScope,
  SyslogMatch,
  SyslogMessage,
  Trap,
  TrapMatch,
  VarbindOp,
} from '../api/types';
import { isOid, parseList } from './logs';
import { isPassiveAction } from '../api/types';

/** IF-MIB ifIndex column: the key of the Link down rule and of flapping suppression. */
export const IF_INDEX_OID = '1.3.6.1.2.1.2.2.1.1';
export const MAX_PRIORITY = 100000;
export const MAX_VARBINDS = 8;
export const MAX_PROGRAMS = 16;

export type VarbindRow = { oid: string; op: VarbindOp; value: string };
export type MatchForm = {
  trapOids: string;
  varbinds: VarbindRow[];
  ports: 'any' | 'important';
  severityMax: number | null;
  facilities: number[];
  programs: string;
  text: string;
  textRegex: boolean;
};
export type RuleForm = {
  name: string;
  kind: EventRuleKind;
  enabled: boolean;
  priority: string;
  scope: RuleScope;
  groupId: number | null;
  deviceId: number | null;
  match: MatchForm;
  action: EventRuleAction;
  keyVarbindOid: string;
  keyRegex: string;
  hasClearMatch: boolean;
  clearMatch: MatchForm;
  clearAfterMin: string;
};

export function emptyMatch(): MatchForm {
  return {
    trapOids: '',
    varbinds: [],
    ports: 'any',
    severityMax: null,
    facilities: [],
    programs: '',
    text: '',
    textRegex: false,
  };
}

function isTrapMatch(m: TrapMatch | SyslogMatch): m is TrapMatch {
  return 'trap_oids' in m;
}

export function matchToForm(m: TrapMatch | SyslogMatch | null): MatchForm {
  const f = emptyMatch();
  if (!m) return f;
  if (isTrapMatch(m)) {
    f.trapOids = m.trap_oids.join('\n');
    f.varbinds = m.varbinds.map((v) => ({ oid: v.oid, op: v.op, value: v.value ?? '' }));
    f.ports = m.ports;
  } else {
    f.severityMax = m.severity_max;
    f.facilities = [...m.facilities];
    f.programs = m.programs.join(', ');
    f.text = m.text ?? '';
    f.textRegex = m.text_regex === true && !!f.text;
  }
  return f;
}

export function formToMatch(kind: EventRuleKind, f: MatchForm): TrapMatch | SyslogMatch {
  if (kind === 'trap')
    return {
      trap_oids: parseList(f.trapOids).map((o) => o.replace(/^\./, '')),
      varbinds: f.varbinds.map((v) => ({
        oid: v.oid.trim().replace(/^\./, ''),
        op: v.op,
        value: v.op === 'exists' || v.op === 'absent' ? null : v.value,
      })),
      ports: f.ports,
    };
  const text = f.text.trim();
  return {
    severity_max: f.severityMax,
    facilities: [...f.facilities].sort((a, b) => a - b),
    programs: parseList(f.programs),
    text: text || null,
    text_regex: !!text && f.textRegex,
  };
}

export function ruleToForm(r: EventRule | null, kind: EventRuleKind = 'syslog'): RuleForm {
  if (!r)
    return {
      name: '',
      kind,
      enabled: true,
      priority: '100',
      scope: 'global',
      groupId: null,
      deviceId: null,
      match: emptyMatch(),
      action: 'warning',
      keyVarbindOid: '',
      keyRegex: '',
      hasClearMatch: false,
      clearMatch: emptyMatch(),
      clearAfterMin: '60',
    };
  return {
    name: r.name,
    kind: r.kind,
    enabled: r.enabled,
    priority: String(r.priority),
    scope: r.scope,
    groupId: r.group_id,
    deviceId: r.device_id,
    match: matchToForm(r.match),
    action: r.action,
    keyVarbindOid: r.key_varbind_oid ?? '',
    keyRegex: r.key_regex ?? '',
    hasClearMatch: r.clear_match !== null,
    clearMatch: matchToForm(r.clear_match),
    clearAfterMin: r.clear_after_min === null ? '' : String(r.clear_after_min),
  };
}

/** Writable fields for POST/PATCH and for /event-rules/test. */
export function formToInput(f: RuleForm): EventRuleInput {
  const active = !isPassiveAction(f.action);
  return {
    name: f.name.trim(),
    kind: f.kind,
    enabled: f.enabled,
    priority: Number(f.priority),
    scope: f.scope,
    group_id: f.scope === 'group' ? f.groupId : null,
    device_id: f.scope === 'device' ? f.deviceId : null,
    match: formToMatch(f.kind, f.match),
    action: f.action,
    key_varbind_oid:
      active && f.kind === 'trap' && f.keyVarbindOid.trim()
        ? f.keyVarbindOid.trim().replace(/^\./, '')
        : null,
    key_regex: active && f.kind === 'syslog' && f.keyRegex.trim() ? f.keyRegex.trim() : null,
    clear_match: active && f.hasClearMatch ? formToMatch(f.kind, f.clearMatch) : null,
    clear_after_min: active && f.clearAfterMin.trim() ? Number(f.clearAfterMin) : null,
  };
}

/** Number of capturing groups of a regex; null when it does not compile. */
export function regexGroups(src: string): number | null {
  // Python named groups (?P<name>…) are JS (?<name>…); back-references (?P=name) → \k<name>.
  const js = src.replace(/\(\?P</g, '(?<').replace(/\(\?P=(\w+)\)/g, '\\k<$1>');
  try {
    const m = new RegExp(`(?:${js})|`, 'i').exec('');
    return m ? m.length - 1 : 0;
  } catch {
    return null;
  }
}

export type RuleErrors = Partial<Record<string, string>>;

function validateMatch(kind: EventRuleKind, m: MatchForm, prefix: string, e: RuleErrors) {
  if (kind === 'trap') {
    const oids = parseList(m.trapOids);
    if (oids.some((o) => !isOid(o))) e[`${prefix}.trapOids`] = 'eventRules.err.oid';
    if (m.varbinds.length > MAX_VARBINDS) e[`${prefix}.varbinds`] = 'eventRules.err.varbinds';
    m.varbinds.forEach((v, i) => {
      if (!isOid(v.oid)) e[`${prefix}.varbinds.${i}.oid`] = 'eventRules.err.oid';
      if (v.op === 'regex' && (v.value.length > 200 || regexGroups(v.value) === null))
        e[`${prefix}.varbinds.${i}.value`] = 'eventRules.err.regex';
    });
  } else {
    if (parseList(m.programs).length > MAX_PROGRAMS)
      e[`${prefix}.programs`] = 'eventRules.err.programs';
    const text = m.text.trim();
    if (text.length > 200) e[`${prefix}.text`] = 'eventRules.err.textLong';
    else if (text && m.textRegex && regexGroups(text) === null)
      e[`${prefix}.text`] = 'eventRules.err.regex';
  }
}

/** Client-side validation (the server answers 422 for the same cases). Values are i18n keys. */
export function validateRule(f: RuleForm): RuleErrors {
  const e: RuleErrors = {};
  const name = f.name.trim();
  if (!name) e.name = 'validation.required';
  else if (name.length > 128) e.name = 'eventRules.err.nameLong';
  const p = Number(f.priority);
  if (!/^\d+$/.test(f.priority.trim()) || p > MAX_PRIORITY) e.priority = 'eventRules.err.priority';
  if (f.scope === 'group' && f.groupId === null) e.scope = 'eventRules.err.scopeGroup';
  if (f.scope === 'device' && f.deviceId === null) e.scope = 'eventRules.err.scopeDevice';
  validateMatch(f.kind, f.match, 'match', e);
  if (!isPassiveAction(f.action)) {
    if (f.kind === 'trap' && f.keyVarbindOid.trim() && !isOid(f.keyVarbindOid))
      e.key = 'eventRules.err.oid';
    if (f.kind === 'syslog' && f.keyRegex.trim()) {
      const g = f.keyRegex.trim().length > 200 ? null : regexGroups(f.keyRegex.trim());
      if (g === null) e.key = 'eventRules.err.regex';
      else if (g !== 1) e.key = 'eventRules.err.keyGroup';
    }
    if (f.hasClearMatch) validateMatch(f.kind, f.clearMatch, 'clearMatch', e);
    const c = f.clearAfterMin.trim();
    if (c && (!/^\d+$/.test(c) || Number(c) < 1 || Number(c) > 10080))
      e.clearAfterMin = 'eventRules.err.clearAfter';
    if (!f.hasClearMatch && !c) e.clear = 'eventRules.err.needClear';
  }
  return e;
}

/** Sorted like the server: (priority, id). */
export function sortRules<T extends Pick<EventRule, 'priority' | 'id'>>(rules: T[]): T[] {
  return [...rules].sort((a, b) => a.priority - b.priority || a.id - b.id);
}

/**
 * Moves a rule in the ordered list and returns the PATCHes needed ({id, priority}). Only the
 * moved rule changes when a free priority exists between its new neighbours; otherwise the list
 * is renumbered in steps of 10 (only changed rules are returned).
 */
export function reorderPatches(
  rules: Pick<EventRule, 'id' | 'priority'>[],
  from: number,
  to: number,
): { id: number; priority: number }[] {
  const list = sortRules(rules);
  if (from === to || from < 0 || to < 0 || from >= list.length || to >= list.length) return [];
  const [moved] = list.splice(from, 1);
  list.splice(to, 0, moved);
  const prev = to > 0 ? list[to - 1].priority : null;
  const next = to < list.length - 1 ? list[to + 1].priority : null;
  let p: number | null = null;
  if (prev === null && next !== null) p = next - 10 >= 0 ? next - 10 : next > 0 ? next - 1 : null;
  else if (next === null && prev !== null) p = Math.min(prev + 10, MAX_PRIORITY);
  else if (prev !== null && next !== null && next - prev >= 2) p = Math.floor((prev + next) / 2);
  if (p !== null && (prev === null || p > prev) && (next === null || p < next))
    return [{ id: moved.id, priority: p }];
  const out: { id: number; priority: number }[] = [];
  list.forEach((r, i) => {
    const np = (i + 1) * 10;
    if (r.priority !== np) out.push({ id: r.id, priority: np });
  });
  return out;
}

/** "Create rule from this message": a syslog rule prefilled from a stored message. */
export function ruleFromSyslog(m: SyslogMessage): RuleForm {
  const f = ruleToForm(null, 'syslog');
  f.name = m.program ? `${m.program}: ${m.message.slice(0, 60)}` : m.message.slice(0, 80);
  f.match.severityMax = m.severity;
  f.match.programs = m.program;
  f.match.text = m.message.slice(0, 80);
  if (m.device_id !== null) {
    f.scope = 'device';
    f.deviceId = m.device_id;
  }
  return f;
}

/** "Create rule from this trap": trap OID (and device) prefilled. */
export function ruleFromTrap(t: Trap): RuleForm {
  const f = ruleToForm(null, 'trap');
  f.name = (t.trap_name ?? t.trap_oid).slice(0, 128);
  f.match.trapOids = t.trap_oid;
  if (t.varbinds.some((v) => v.oid === IF_INDEX_OID || v.oid.startsWith(`${IF_INDEX_OID}.`)))
    f.keyVarbindOid = IF_INDEX_OID;
  if (t.device_id !== null) {
    f.scope = 'device';
    f.deviceId = t.device_id;
  }
  return f;
}
