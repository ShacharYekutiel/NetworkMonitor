import type { TFunction } from 'i18next';
import type { AlertRuleInput } from '../api/endpoints';
import type { AlertRule, NotificationChannel } from '../api/types';

/**
 * The default alert rule the notifications page offers (never created without the user choosing it):
 * every sensor, "down" only, every severity, no delay, enabled. It is an ordinary rule afterwards. The name
 * is stored as data, so it is written in the UI language at creation time.
 */
export function defaultRuleInput(t: TFunction, channelIds: number[]): AlertRuleInput {
  return {
    name: t('rules.defaultRule.name'),
    sensor_id: null,
    on_statuses: ['down'],
    severities: [],
    delay_s: 0,
    enabled: true,
    channel_ids: channelIds,
  };
}

/** No rule at all: the "create channel" dialog offers the default rule. */
export function offerDefaultRuleWithChannel(rules: AlertRule[] | undefined): boolean {
  return rules !== undefined && rules.length === 0;
}

/** Channels exist but no rule is enabled: nothing would ever be sent. */
export function noActiveRule(
  channels: NotificationChannel[] | undefined,
  rules: AlertRule[] | undefined,
): boolean {
  return !!channels?.length && rules !== undefined && !rules.some((r) => r.enabled);
}
