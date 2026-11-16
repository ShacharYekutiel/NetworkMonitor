// [1.4.1] pentest finding 10: values that reach an ECharts tooltip (innerHTML) are escaped.
import { describe, expect, it } from 'vitest';
import {
  escapeHtml,
  histogramTooltipHtml,
  historyTooltipHtml,
  safeColor,
  usageTooltipHtml,
} from './html';
import { formatValue } from './format';
import { formatCost } from './assistant';
import { renderAnswer } from './markdown';

const XSS = '<img src=x onerror=alert(1)>';

/** What the browser would build from the tooltip string. */
function dom(html: string): HTMLElement {
  const el = document.createElement('div');
  el.innerHTML = html;
  return el;
}

describe('escapeHtml', () => {
  it('escapes markup and attribute delimiters', () => {
    expect(escapeHtml(`</script><svg onload=alert(2)> & "q" 'a' \`b\``)).toBe(
      '&lt;/script&gt;&lt;svg onload=alert(2)&gt; &amp; &quot;q&quot; &#39;a&#39; &#96;b&#96;',
    );
    expect(escapeHtml(null)).toBe('');
    expect(escapeHtml(12)).toBe('12');
  });
});

describe('chart tooltips', () => {
  it('history chart: a custom sensor unit cannot inject markup', () => {
    const html = historyTooltipHtml({
      when: '13/11/2026, 10:00:00',
      avg: formatValue(5, XSS),
      min: formatValue(1, XSS),
      max: formatValue(9, XSS),
      labels: { avg: 'Average', min: 'Min', max: 'Max' },
    });
    const el = dom(html);
    expect(el.querySelector('img')).toBeNull();
    expect(el.querySelectorAll('b')).toHaveLength(1);
    expect(el.textContent).toContain(`5 ${XSS}`);
    expect(
      dom(
        historyTooltipHtml({
          when: XSS,
          avg: null,
          min: '',
          max: '',
          labels: { avg: '', min: '', max: '' },
        }),
      ).querySelector('img'),
    ).toBeNull();
  });

  it('log histogram: series names and colours are inert', () => {
    const html = histogramTooltipHtml('10:00', [
      { name: XSS, color: 'red"><script>alert(1)</script>', value: 3 },
      { name: 'error', color: '#dc2626', value: 2 },
    ]);
    const el = dom(html);
    expect(el.querySelector('img,script')).toBeNull();
    expect(el.querySelectorAll('span')).toHaveLength(2);
    expect(el.textContent).toContain(XSS);
    expect(safeColor('#dc2626')).toBe('#dc2626');
    expect(safeColor('rgb(1, 2, 3)')).toBe('rgb(1, 2, 3)');
    expect(safeColor('red;background:url(x)')).toBe('currentColor');
  });

  it('assistant usage: the currency setting is escaped', () => {
    const html = usageTooltipHtml({
      day: '2026-11-13',
      tokens: '1.2k',
      questions: 4,
      cost: formatCost(0.5, XSS),
      labels: { tokens: 'Tokens', questions: 'Questions' },
    });
    const el = dom(html);
    expect(el.querySelector('img')).toBeNull();
    expect(el.textContent).toContain(`0.500 ${XSS}`);
  });
});

describe('assistant answers (the one dangerouslySetInnerHTML sink)', () => {
  it('model output with device names carrying markup stays text', () => {
    const el = dom(
      renderAnswer(
        `Device ${XSS} is down. <script>alert(1)</script>\n\n` +
          '[click](javascript:alert(1)) ![i](http://x/y.png) <http://evil.example>\n\n' +
          '| a |\n|---|\n| <b onmouseover=alert(1)>x</b> |\n\n`<svg onload=alert(2)>`',
      ),
    );
    expect(el.querySelector('img,script,svg,b,a')).toBeNull();
    expect(el.querySelectorAll('[onerror],[onload],[onmouseover],[onclick]')).toHaveLength(0);
    expect(el.textContent).toContain(XSS);
  });
});
