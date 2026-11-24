import { describe, expect, it } from 'vitest';
import i18n from '../i18n';
import type { AlertRule, NotificationChannel } from '../api/types';
import { defaultRuleInput, noActiveRule, offerDefaultRuleWithChannel } from './defaultRule';

const rule = (enabled: boolean, channel_ids = [7]) =>
  ({ id: 1, enabled, channel_ids }) as AlertRule;
const channel = { id: 7, enabled: true } as NotificationChannel;
const channelOff = { id: 8, enabled: false } as NotificationChannel;

describe('default alert rule', () => {
  it('every sensor, down and recovery, every severity, no delay, enabled, named in the UI language', () => {
    expect(defaultRuleInput(i18n.getFixedT('en'), [3, 4])).toEqual({
      name: 'Default: every sensor that goes down or recovers',
      sensor_id: null,
      on_statuses: ['down', 'up'],
      severities: [],
      delay_s: 0,
      enabled: true,
      channel_ids: [3, 4],
    });
    expect(defaultRuleInput(i18n.getFixedT('he'), [3]).name).toBe(
      'ברירת מחדל: כל חיישן שעובר לתקלה או חוזר לתקין',
    );
  });

  it('is offered with a new channel only when no rule exists at all', () => {
    expect(offerDefaultRuleWithChannel([])).toBe(true);
    expect(offerDefaultRuleWithChannel([rule(false)])).toBe(false);
    expect(offerDefaultRuleWithChannel(undefined)).toBe(false); // still loading
  });

  it('warns when channels exist and no rule is enabled', () => {
    expect(noActiveRule([channel], [])).toBe(true);
    expect(noActiveRule([channel], [rule(false)])).toBe(true);
    expect(noActiveRule([channel], [rule(false), rule(true)])).toBe(false);
    expect(noActiveRule([], [])).toBe(false);
    expect(noActiveRule([channel], undefined)).toBe(false);
    // an enabled rule whose channels are all turned off sends nothing either
    expect(noActiveRule([channel, channelOff], [rule(true, [8])])).toBe(true);
    expect(noActiveRule([channel, channelOff], [rule(true, [8, 7])])).toBe(false);
  });

  it('has every text in both languages', () => {
    for (const k of [
      'name',
      'create',
      'createHint',
      'created',
      'createFailed',
      'bannerTitle',
      'bannerHint',
      'bannerNoChannel',
      'bannerChannelsOff',
      'bannerButton',
    ])
      for (const lng of ['en', 'he'])
        expect(i18n.exists(`rules.defaultRule.${k}`, { lng }), `${lng}:${k}`).toBe(true);
  });
});
