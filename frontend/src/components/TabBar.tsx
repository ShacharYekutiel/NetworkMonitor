import type { ReactNode } from 'react';
import { cx } from './ui';

/** A row of tabs (role=tablist) under a page header. */
export function TabBar<T extends string>({
  tabs,
  value,
  onChange,
  label,
  render,
}: {
  tabs: T[];
  value: T;
  onChange: (t: T) => void;
  label: string;
  render: (t: T) => ReactNode;
}) {
  return (
    <div
      role="tablist"
      aria-label={label}
      className="flex flex-wrap gap-1 border-b border-slate-200 dark:border-slate-800"
    >
      {tabs.map((k) => (
        <button
          key={k}
          type="button"
          role="tab"
          aria-selected={value === k}
          onClick={() => onChange(k)}
          className={cx(
            '-mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium',
            value === k
              ? 'border-sky-600 text-sky-700 dark:border-sky-400 dark:text-sky-300'
              : 'border-transparent text-slate-600 hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-200',
          )}
        >
          {render(k)}
        </button>
      ))}
    </div>
  );
}
