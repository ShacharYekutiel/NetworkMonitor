import type { TFunction } from 'i18next';
import type { AlertRuleInput } from '../api/endpoints';
import type { AlertRule, NotificationChannel } from '../api/types';

/**
 * The default alert rule the notifications page offers (never created without the user choosing it):
 * every sensor, "down" and its recovery ("up"), every severity, no delay, enabled. It is an ordinary rule afterwards. The name
 * is stored as data, so it is written in the UI language at creation time.
 */
export function defaultRuleInput(t: TFunction, channelIds: number[]): AlertRuleInput {
  return {
    name: t('rules.defaultRule.name'),
    sensor_id: null,
    on_statuses: ['down', 'up'],
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

/**
 * Channels exist but nothing would ever be sent: no rule is enabled, or every enabled rule sends only to
 * channels that are turned off.
 */
export function noActiveRule(
  channels: NotificationChannel[] | undefined,
  rules: AlertRule[] | undefined,
): boolean {
  if (!channels?.length || rules === undefined) return false;
  const on = new Set(channels.filter((c) => c.enabled).map((c) => c.id));
  return !rules.some((r) => r.enabled && r.channel_ids.some((id) => on.has(id)));
}
