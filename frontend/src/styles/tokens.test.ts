// The single token set: tokens.json -> tokens.css + tokens.gen.ts (npm run tokens) must be in sync.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { TOKEN_VALUES } from './tokens.gen';
import { DARK_TOKENS, LIGHT_TOKENS, canvasColor } from '../lib/chartTokens';

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
const json = JSON.parse(read('../../../docs/design-system/tokens.json')) as {
  color: { tokens: { name: string; value: { light: string; dark: string } }[] };
};
const css = read('./tokens.css');

describe('design tokens', () => {
  it('tokens.gen.ts has every colour of tokens.json, per theme', () => {
    for (const tk of json.color.tokens) {
      expect((TOKEN_VALUES.light as Record<string, string>)[tk.name], tk.name).toBe(tk.value.light);
      expect((TOKEN_VALUES.dark as Record<string, string>)[tk.name], tk.name).toBe(tk.value.dark);
    }
  });

  it('tokens.css declares every colour in both themes', () => {
    const [light, dark] = css.split(':root.dark');
    for (const tk of json.color.tokens) {
      expect(light).toContain(`--${tk.name}: ${tk.value.light};`);
      expect(dark).toContain(`--${tk.name}: ${tk.value.dark};`);
    }
  });

  it('chart fallbacks follow the token table', () => {
    expect(LIGHT_TOKENS.status.down).toBe(TOKEN_VALUES.light['status-down']);
    expect(DARK_TOKENS.surface).toBe(TOKEN_VALUES.dark.surface);
    expect(DARK_TOKENS.bg).toBe('#000000');
    expect(LIGHT_TOKENS.fontDisplay).toContain('Inter');
  });

  it('minified 8-digit hex colours are turned back into rgba() for the canvas', () => {
    expect(canvasColor('#00000014')).toBe('rgba(0, 0, 0, 0.078)');
    expect(canvasColor('#fff8')).toBe('rgba(255, 255, 255, 0.533)');
    expect(canvasColor('#1d1d1f')).toBe('#1d1d1f');
  });
});
