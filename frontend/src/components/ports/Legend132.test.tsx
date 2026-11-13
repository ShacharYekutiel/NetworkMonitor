import { afterEach, describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import i18n from '../../i18n';
import en from '../../i18n/locales/en.json';
import he from '../../i18n/locales/he.json';
import { FaceplateLegend } from './Faceplate';

const KINDS = ['empty', 'up', 'alert', 'emptyAlert', 'disabled'] as const;

describe('[1.3.2] faceplate legend labels and hints', () => {
  afterEach(() => i18n.changeLanguage('en'));

  it('every tile has a label and a hover hint (en)', () => {
    render(<FaceplateLegend />);
    for (const k of KINDS) {
      const el = screen.getByTestId(`legend-${k}`);
      expect(el.textContent?.trim()).not.toBe('');
      expect(el.getAttribute('title')?.length ?? 0).toBeGreaterThan(10);
    }
    const ea = screen.getByTestId('legend-emptyAlert');
    expect(ea.textContent).toContain('No link, alert');
    expect(ea.getAttribute('title')).toContain('interface sensor');
  });

  it('Hebrew labels and hints', async () => {
    await i18n.changeLanguage('he');
    render(<FaceplateLegend />);
    const ea = screen.getByTestId('legend-emptyAlert');
    expect(ea.textContent).toContain('ללא קישור, בהתראה');
    expect(ea.getAttribute('title')).toContain('חיישן ממשק');
  });

  it('en and he define the same hint keys', () => {
    expect(Object.keys(he.ports.legendHint).sort()).toEqual(
      Object.keys(en.ports.legendHint).sort(),
    );
  });
});
