import type { ReactNode } from 'react';
import { tabClass } from './ui';

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
      className="flex overflow-x-auto border-b border-line [scrollbar-width:none]"
    >
      {tabs.map((k) => (
        <button
          key={k}
          type="button"
          role="tab"
          aria-selected={value === k}
          onClick={() => onChange(k)}
          className={tabClass(value === k)}
        >
          {render(k)}
        </button>
      ))}
    </div>
  );
}
