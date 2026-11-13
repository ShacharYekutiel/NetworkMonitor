import { useEffect, useRef, type ReactNode } from 'react';
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
  const ref = useRef<HTMLDivElement>(null);
  // A long row (Settings) scrolls sideways: keep the selected tab visible.
  useEffect(() => {
    const el = ref.current?.querySelector<HTMLElement>('[aria-selected="true"]');
    el?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
  }, [value]);
  return (
    <div
      ref={ref}
      role="tablist"
      aria-label={label}
      className="flex overflow-x-auto border-b border-line [scrollbar-width:thin]"
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
