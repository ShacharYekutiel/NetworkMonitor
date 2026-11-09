// [2b-3] Audit log helpers. Pure.
export const AUDIT_ACTION_PREFIXES = [
  'auth.',
  'device.',
  'group.',
  'sensor.',
  'alert_rule.',
  'notification_channel.',
  'maintenance_window.',
  'event_rule.',
  'topology_link.',
  'discovery_profile.',
  'discovery.',
  'user.',
  'api_token.',
  'settings.',
  'feature.',
  'ingest.',
];
export const AUDIT_ENTITY_TYPES = [
  'device',
  'group',
  'sensor',
  'alert_rule',
  'notification_channel',
  'maintenance_window',
  'event_rule',
  'topology_link',
  'discovery_profile',
  'user',
  'api_token',
];

/** A value of the `changes` diff as text: secrets stay "***", objects as compact JSON. */
export function changeText(v: unknown): string {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'string') return v === '' ? '""' : v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

export type ChangeRow = { field: string; old: string; new: string; secret: boolean };

export function changeRows(
  changes: Record<string, [unknown, unknown]> | null | undefined,
): ChangeRow[] {
  if (!changes) return [];
  return Object.entries(changes)
    .filter(([, v]) => Array.isArray(v))
    .map(([field, [o, n]]) => ({
      field,
      old: changeText(o),
      new: changeText(n),
      secret: o === '***' || n === '***',
    }))
    .sort((a, b) => a.field.localeCompare(b.field));
}

/** Failed/anonymous logins and deletions are highlighted. */
export function auditTone(action: string): 'bad' | 'warn' | 'normal' {
  if (action === 'auth.login_failed') return 'bad';
  if (action.endsWith('.delete') || action.endsWith('.revoke') || action === 'feature.disable')
    return 'warn';
  return 'normal';
}
