// [3b] Rule drafts (architecture § 14.8.6): the draft only PREFILLS the normal form; every value is
// type-checked here and the admin saves through the normal API. Pure.
import type { AlertRule, EventRule, EventRuleInput } from '../api/types';
import type { AlertRuleInput } from '../api/endpoints';
import { ruleToForm, type RuleForm } from './eventRules';

const isInt = (v: unknown): v is number => typeof v === 'number' && Number.isInteger(v);
const isIntOrNull = (v: unknown): v is number | null => v === null || isInt(v);
const isIntList = (v: unknown): v is number[] => Array.isArray(v) && v.every(isInt);
const RULE_STATUSES = ['down', 'warning', 'up'] as const;

/** Alert-rule draft → the writable fields the alert-rule form understands (unknown keys dropped). */
export function alertDraftToForm(d: Record<string, unknown>): AlertRuleInput {
  const out: AlertRuleInput = {};
  if (typeof d.name === 'string') out.name = d.name.slice(0, 128);
  if ('sensor_id' in d && isIntOrNull(d.sensor_id)) out.sensor_id = d.sensor_id;
  if (Array.isArray(d.on_statuses))
    out.on_statuses = d.on_statuses.filter((s): s is AlertRule['on_statuses'][number] =>
      (RULE_STATUSES as readonly unknown[]).includes(s),
    );
  if (isInt(d.delay_s)) out.delay_s = d.delay_s;
  if (typeof d.enabled === 'boolean') out.enabled = d.enabled;
  if (isIntList(d.channel_ids)) out.channel_ids = d.channel_ids;
  if ('escalation_after_s' in d && isIntOrNull(d.escalation_after_s))
    out.escalation_after_s = d.escalation_after_s;
  if (isIntList(d.escalation_channel_ids)) out.escalation_channel_ids = d.escalation_channel_ids;
  if ('repeat_every_s' in d && isIntOrNull(d.repeat_every_s)) out.repeat_every_s = d.repeat_every_s;
  if (typeof d.ai_note === 'boolean') out.ai_note = d.ai_note;
  return out;
}

const EVENT_KEYS = [
  'name',
  'kind',
  'enabled',
  'priority',
  'scope',
  'group_id',
  'device_id',
  'match',
  'action',
  'key_varbind_oid',
  'key_regex',
  'clear_match',
  'clear_after_min',
] as const;

/** Event-rule draft → its writable fields (shape checks are done by the form's validator). */
export function eventDraftToForm(d: Record<string, unknown>): EventRuleInput {
  const out: Record<string, unknown> = {};
  for (const k of EVENT_KEYS) if (k in d) out[k] = d[k];
  return out as EventRuleInput;
}

/** The event-rule editor's form state for a draft (falls back to an empty form on odd shapes). */
export function eventDraftRuleForm(d: Record<string, unknown>): RuleForm {
  const i = eventDraftToForm(d);
  const kind = i.kind === 'trap' ? 'trap' : 'syslog';
  const empty = ruleToForm(null, kind);
  try {
    const pseudo = {
      id: 0,
      name: typeof i.name === 'string' ? i.name : '',
      kind,
      enabled: typeof i.enabled === 'boolean' ? i.enabled : true,
      builtin: null,
      priority: isInt(i.priority) ? i.priority : 100,
      scope: i.scope ?? 'global',
      group_id: i.group_id ?? null,
      device_id: i.device_id ?? null,
      scope_name: null,
      match: i.match ?? null,
      action: i.action ?? 'warning',
      key_varbind_oid: i.key_varbind_oid ?? null,
      key_regex: i.key_regex ?? null,
      clear_match: i.clear_match ?? null,
      clear_after_min: i.clear_after_min ?? null,
      active_conditions: 0,
      last_matched_at: null,
      created_at: '',
      updated_at: '',
    } as unknown as EventRule;
    const f = ruleToForm(pseudo);
    return { ...f, match: f.match ?? empty.match, clearMatch: f.clearMatch ?? empty.clearMatch };
  } catch {
    return { ...empty, name: typeof i.name === 'string' ? i.name : '' };
  }
}
