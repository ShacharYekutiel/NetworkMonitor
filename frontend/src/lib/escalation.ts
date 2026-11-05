// [2b-1] Alert-rule escalation/reminder form mapping (inputs in minutes, API in seconds).
import type { AlertRule } from '../api/types';

export type EscalationForm = {
  escalate: boolean;
  /** minutes */
  escalation_after_min: string;
  escalation_channel_ids: number[];
  remind: boolean;
  /** minutes */
  repeat_every_min: string;
};

export type EscalationErrors = Partial<
  Record<'escalation_after' | 'escalation_channels' | 'repeat_every' | 'statuses', string>
>;

export const ESCALATION_MIN_S = 60;
export const REPEAT_MIN_S = 300;
export const ESCALATION_MAX_S = 604800;

function minutesText(s: number | null | undefined, fallback: string): string {
  if (s === null || s === undefined) return fallback;
  const m = s / 60;
  return Number.isInteger(m) ? String(m) : String(Math.round(m * 100) / 100);
}

export function escalationForm(
  rule?: Pick<AlertRule, 'escalation_after_s' | 'escalation_channel_ids' | 'repeat_every_s'>,
): EscalationForm {
  return {
    escalate: rule?.escalation_after_s != null,
    escalation_after_min: minutesText(rule?.escalation_after_s, '15'),
    escalation_channel_ids: rule?.escalation_channel_ids ?? [],
    remind: rule?.repeat_every_s != null,
    repeat_every_min: minutesText(rule?.repeat_every_s, '60'),
  };
}

function minutesToSeconds(v: string): number | null {
  const s = v.trim().replace(',', '.');
  if (!/^\d+(\.\d+)?$/.test(s)) return null;
  return Math.round(Number(s) * 60);
}

/**
 * Builds the escalation part of a rule body (data-model.md § escalation validation):
 * `escalation_after_s` 60–604800 and ≥ `delay_s`, together with ≥ 1 escalation channel;
 * `repeat_every_s` 300–604800; both need `down` or `warning` in `on_statuses`.
 * Disabled parts are sent as null / [] (which also clears them on PATCH). Errors are i18n keys.
 */
export function escalationToInput(
  f: EscalationForm,
  ctx: { delay_s: number; on_statuses: AlertRule['on_statuses'] },
): {
  body: Pick<AlertRule, 'escalation_after_s' | 'escalation_channel_ids' | 'repeat_every_s'>;
  errors: EscalationErrors;
} {
  const errors: EscalationErrors = {};
  const body: Pick<AlertRule, 'escalation_after_s' | 'escalation_channel_ids' | 'repeat_every_s'> =
    { escalation_after_s: null, escalation_channel_ids: [], repeat_every_s: null };
  const problemStatus = ctx.on_statuses.includes('down') || ctx.on_statuses.includes('warning');
  if (f.escalate) {
    const s = minutesToSeconds(f.escalation_after_min);
    if (s === null || s < ESCALATION_MIN_S || s > ESCALATION_MAX_S)
      errors.escalation_after = 'escalation.err.after';
    else if (s < ctx.delay_s) errors.escalation_after = 'escalation.err.afterDelay';
    body.escalation_after_s = s;
    body.escalation_channel_ids = [...new Set(f.escalation_channel_ids)];
    if (!body.escalation_channel_ids.length) errors.escalation_channels = 'escalation.err.channels';
  }
  if (f.remind) {
    const s = minutesToSeconds(f.repeat_every_min);
    if (s === null || s < REPEAT_MIN_S || s > ESCALATION_MAX_S)
      errors.repeat_every = 'escalation.err.repeat';
    body.repeat_every_s = s;
  }
  if ((f.escalate || f.remind) && !problemStatus) errors.statuses = 'escalation.err.statuses';
  return { body, errors };
}
