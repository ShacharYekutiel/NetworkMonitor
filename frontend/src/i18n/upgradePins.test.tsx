// Pins i18next / react-i18next behaviour an upgrade could change silently:
// Hebrew one/two/other plurals, interpolation escaping, missing keys, `Trans` with components,
// and `Trans` following a language change.
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { act, render } from '@testing-library/react';
import { Trans } from 'react-i18next';
import i18n from './index';

beforeEach(async () => {
  await i18n.changeLanguage('en');
});
afterEach(async () => {
  await i18n.changeLanguage('en');
});

describe('i18next behaviour pins', () => {
  it('resolves Hebrew plurals as one / two / other (0 is other)', () => {
    const he = i18n.getFixedT('he');
    expect(he('dashboard.groupCount', { count: 1 })).toBe('קבוצה אחת');
    expect(he('dashboard.groupCount', { count: 2 })).toBe('2 קבוצות');
    expect(he('dashboard.groupCount', { count: 3 })).toBe('3 קבוצות');
    expect(he('dashboard.groupCount', { count: 0 })).toBe('0 קבוצות');
    expect(he('dashboard.groupCount', { count: 20 })).toBe('20 קבוצות');
  });

  it('resolves English plurals as one / other, and _zero where present', () => {
    expect(i18n.t('dashboard.groupCount', { count: 1 })).toBe('1 group');
    expect(i18n.t('dashboard.groupCount', { count: 2 })).toBe('2 groups');
    expect(i18n.t('aiStatus.openIncidents', { count: 0 })).toBe('No open incidents');
  });

  it('does not HTML-escape interpolated values (escapeValue: false)', () => {
    expect(i18n.t('sensors.created', { name: '<b>a & b</b> 1/2 "q"' })).toBe(
      'Sensor "<b>a & b</b> 1/2 "q"" created',
    );
  });

  it('returns the key for a missing key, in both languages', () => {
    expect(i18n.t('no.such.key')).toBe('no.such.key');
    expect(i18n.getFixedT('he')('no.such.key')).toBe('no.such.key');
  });

  it('marks Hebrew as rtl', () => {
    expect(i18n.dir('he')).toBe('rtl');
    expect(i18n.dir('en')).toBe('ltr');
  });
});

describe('Trans behaviour pins', () => {
  it('renders named components (bdi) around translated text', () => {
    const { container } = render(
      <Trans
        i18nKey="discovery.targetsHint"
        components={{ c: <bdi dir="ltr" className="font-mono" /> }}
      />,
    );
    const bdis = container.querySelectorAll('bdi.font-mono');
    expect(bdis).toHaveLength(3);
    expect(bdis[0]).toHaveTextContent('192.168.1.0/24');
    expect(bdis[0]).toHaveAttribute('dir', 'ltr');
    expect(container.textContent).toContain('One per line: CIDR (192.168.1.0/24)');
  });

  it('renders a value with markup as text inside a component', () => {
    const { container } = render(
      <Trans
        i18nKey="dependency.unreachableMsg"
        values={{ name: 'gw <i>1</i> & 2/3' }}
        components={{ p: <span className="parent" /> }}
      />,
    );
    const parent = container.querySelector('span.parent');
    expect(parent).toHaveTextContent('gw <i>1</i> & 2/3');
    expect(container.querySelector('i')).toBeNull();
    expect(container.textContent).toBe('Unreachable (parent gw <i>1</i> & 2/3 down)');
  });

  it('follows a language change', async () => {
    const { container } = render(
      <Trans
        i18nKey="dependency.unreachableMsg"
        values={{ name: 'gw' }}
        components={{ p: <span /> }}
      />,
    );
    expect(container.textContent).toBe('Unreachable (parent gw down)');
    await act(async () => {
      await i18n.changeLanguage('he');
    });
    expect(container.textContent).toBe('לא נגיש (התקן האב gw מושבת)');
  });
});
