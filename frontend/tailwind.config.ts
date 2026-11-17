import type { Config } from 'tailwindcss';

/**
 * Design tokens (docs/design-system/tokens.json -> src/styles/tokens.css, `npm run tokens`) as
 * Tailwind names. Every colour is a CSS variable, so components need no `dark:` variants.
 */
const v = (name: string) => `var(--${name})`;

const STATUSES = ['up', 'warning', 'down', 'unreachable', 'unknown', 'paused'] as const;
const statusColors: Record<string, string> = {};
for (const s of STATUSES) {
  statusColors[s] = v(`status-${s}`);
  statusColors[`${s}-soft`] = v(`status-${s}-soft`);
}

const layout = {
  header: v('header-height'),
  menu: v('menu-width'),
  row: v('row-height'),
};

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  // The tokens switch on the `dark` class of <html> (hooks/theme.tsx).
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        bg: { DEFAULT: v('bg'), top: v('bg-top') },
        surface: {
          DEFAULT: v('surface'),
          raised: v('surface-raised'),
          sunken: v('surface-sunken'),
          hover: v('surface-hover'),
        },
        line: { DEFAULT: v('border'), strong: v('border-strong') },
        field: { DEFAULT: v('field'), border: v('field-border'), disabled: v('field-disabled') },
        ink: { DEFAULT: v('ink'), muted: v('ink-muted'), subtle: v('ink-subtle') },
        chrome: {
          DEFAULT: v('chrome'),
          ink: v('chrome-ink'),
          muted: v('chrome-muted'),
          field: v('chrome-field'),
          border: v('chrome-border'),
        },
        chip: v('chip'),
        accent: {
          DEFAULT: v('accent'),
          hover: v('accent-hover'),
          ink: v('accent-ink'),
          soft: v('accent-soft'),
          on: v('on-accent'),
        },
        focus: v('focus'),
        nav: { active: v('nav-active'), 'active-ink': v('nav-active-ink') },
        island: { DEFAULT: v('island'), ink: v('island-ink') },
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
        hebrew: v('font-hebrew'),
        display: v('font-display'),
        mono: v('font-mono'),
      },
      borderRadius: {
        xs: v('radius-xs'),
        sm: v('radius-sm'),
        md: v('radius-md'),
        lg: v('radius-lg'),
        xl: v('radius-xl'),
        full: v('radius-full'),
      },
      boxShadow: { card: v('shadow-card'), pop: v('shadow-pop'), island: v('shadow-island') },
      transitionTimingFunction: { premium: v('ease') },
      spacing: layout,
      width: layout,
      height: layout,
      inset: layout,
      maxWidth: { page: v('page-max'), content: v('content-max') },
      outlineColor: { focus: v('focus') },
      ringColor: { focus: v('focus') },
    },
  },
  plugins: [],
} satisfies Config;
