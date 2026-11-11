import type { Config } from 'tailwindcss';

/** Design tokens (docs/design-system/tokens.json) as CSS variables from src/styles/tokens.css. */
const v = (name: string) => `var(--${name})`;

const STATUSES = ['up', 'warning', 'down', 'unreachable', 'unknown', 'paused'] as const;
const statusColors: Record<string, string> = {};
for (const s of STATUSES) {
  statusColors[s] = v(`status-${s}`);
  statusColors[`${s}-soft`] = v(`status-${s}-soft`);
}

const layout = {
  header: v('header-height'),
  rail: v('rail-width'),
  tree: v('tree-width'),
  row: v('row-height'),
};

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  // The tokens switch on the `dark` class of <html> (hooks/theme.tsx); components need no
  // `dark:` colour variants.
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        bg: v('bg'),
        surface: {
          DEFAULT: v('surface'),
          raised: v('surface-raised'),
          sunken: v('surface-sunken'),
          hover: v('surface-hover'),
        },
        line: { DEFAULT: v('border'), strong: v('border-strong') },
        ink: { DEFAULT: v('ink'), muted: v('ink-muted'), subtle: v('ink-subtle') },
        chrome: {
          DEFAULT: v('chrome'),
          ink: v('chrome-ink'),
          muted: v('chrome-muted'),
          field: v('chrome-field'),
          border: v('chrome-border'),
        },
        accent: {
          DEFAULT: v('accent'),
          hover: v('accent-hover'),
          ink: v('accent-ink'),
          soft: v('accent-soft'),
          on: v('on-accent'),
        },
        focus: v('focus'),
        nav: { active: v('nav-active'), 'active-ink': v('nav-active-ink') },
        status: statusColors,
        ack: { DEFAULT: v('ack'), soft: v('ack-soft') },
        danger: v('danger'),
        'on-danger': v('on-danger'),
        'on-status': v('on-status'),
        overlay: v('overlay'),
        'chart-1': v('chart-1'),
        'chart-2': v('chart-2'),
        'chart-3': v('chart-3'),
        'chart-4': v('chart-4'),
        'chart-grid': v('chart-grid'),
        'port-empty': v('port-empty'),
        'port-disabled': v('port-disabled'),
      },
      fontFamily: {
        sans: v('font-sans'),
        display: v('font-display'),
        mono: v('font-mono'),
      },
      borderRadius: {
        xs: v('radius-xs'),
        sm: v('radius-sm'),
        md: v('radius-md'),
        lg: v('radius-lg'),
        full: v('radius-full'),
      },
      boxShadow: { pop: v('shadow-pop') },
      spacing: layout,
      width: layout,
      height: layout,
      inset: layout,
      maxWidth: { content: v('content-max') },
      outlineColor: { focus: v('focus') },
      ringColor: { focus: v('focus') },
    },
  },
  plugins: [],
} satisfies Config;
