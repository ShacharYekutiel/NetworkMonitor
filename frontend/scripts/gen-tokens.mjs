#!/usr/bin/env node
// Design tokens: docs/design-system/tokens.json -> CSS variables + a TS table for canvas/SVG code.
//
//   node scripts/gen-tokens.mjs          write the three outputs
//   node scripts/gen-tokens.mjs --check  exit 1 when an output is not up to date
//
// Outputs:
//   frontend/src/styles/tokens.css   light on :root, dark on :root.dark (hooks/theme.tsx sets the class)
//   docs/design-system/tokens.css    the same file, for the mockups
//   frontend/src/styles/tokens.gen.ts literal values per theme (ECharts and inline SVG cannot read CSS vars)
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..', '..');
const SRC = join(ROOT, 'docs', 'design-system', 'tokens.json');
const OUT_CSS = [
  join(ROOT, 'frontend', 'src', 'styles', 'tokens.css'),
  join(ROOT, 'docs', 'design-system', 'tokens.css'),
];
const OUT_TS = join(ROOT, 'frontend', 'src', 'styles', 'tokens.gen.ts');

const json = JSON.parse(readFileSync(SRC, 'utf8'));

function themed(groups) {
  const light = [];
  const dark = [];
  for (const g of groups)
    for (const tk of g) {
      if (typeof tk.value === 'string') {
        light.push([tk.name, tk.value]);
      } else {
        light.push([tk.name, tk.value.light]);
        dark.push([tk.name, tk.value.dark]);
      }
    }
  return { light, dark };
}

const { light, dark } = themed([json.color.tokens, json.shadow.tokens]);
const plain = [
  ...json.spacing.tokens.map((t) => [t.name, t.value]),
  ...json.radius.tokens.map((t) => [t.name, t.value]),
  ...json.layout.tokens.map((t) => [t.name, t.value]),
  ...(json.motion?.tokens ?? []).map((t) => [t.name, t.value]),
  ...Object.entries(json.type.families).map(([k, v]) => [`font-${k}`, v]),
];

const decl = (pairs) => pairs.map(([k, v]) => `  --${k}: ${v};`).join('\n');
const css = `/* NetworkMonitor design tokens — generated from docs/design-system/tokens.json by
 * frontend/scripts/gen-tokens.mjs. Do not edit by hand. */
/* Light values on :root; dark values when <html> has class "dark" (hooks/theme.tsx). */
:root {
  color-scheme: light;
${decl(light)}
${decl(plain)}
}
:root.dark,
[data-theme='dark'] {
  color-scheme: dark;
${decl(dark)}
}
`;

const quote = (v) => `'${v.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'`;
const obj = (pairs) => `{\n${pairs.map(([k, v]) => `    '${k}': ${quote(v)},`).join('\n')}\n  }`;
const ts = `// Generated from docs/design-system/tokens.json by frontend/scripts/gen-tokens.mjs. Do not edit.
// Literal token values per theme, for code that cannot read CSS variables (ECharts, inline SVG).
export const TOKEN_VALUES = {
  light: ${obj(light)},
  dark: ${obj(dark)},
} as const;

export const TOKEN_CONSTANTS = ${obj(plain)} as const;

export type TokenName = keyof typeof TOKEN_VALUES.light;
`;

const check = process.argv.includes('--check');
let stale = 0;
for (const [file, body] of [...OUT_CSS.map((f) => [f, css]), [OUT_TS, ts]]) {
  let cur = null;
  try {
    cur = readFileSync(file, 'utf8');
  } catch {
    /* missing */
  }
  if (cur === body) continue;
  if (check) {
    console.error(`stale: ${file}`);
    stale++;
  } else {
    writeFileSync(file, body);
    console.log(`wrote ${file}`);
  }
}
if (stale) process.exit(1);
