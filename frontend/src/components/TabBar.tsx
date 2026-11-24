import { useEffect, useRef, type ReactNode } from 'react';
import { cx, SEG_TRACK, tabClass, wrapTabClass } from './ui';

/**
 * A row of tabs (role=tablist) under a page header, drawn as the premium segmented control.
 * `wrap`: a long set (Settings) wraps onto further rows as chips, the selected one in ink.
 */
export function TabBar<T extends string>({
  tabs,
  value,
  onChange,
  label,
  render,
  wrap,
}: {
  tabs: T[];
  value: T;
  onChange: (t: T) => void;
  label: string;
  render: (t: T) => ReactNode;
  wrap?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  // A row that scrolls sideways keeps the selected tab visible.
  useEffect(() => {
    if (wrap) return;
    const el = ref.current?.querySelector<HTMLElement>('[aria-selected="true"]');
    el?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
  }, [value, wrap]);
  return (
    <div
      ref={ref}
      role="tablist"
      aria-label={label}
      className={wrap ? 'flex flex-wrap gap-1.5' : cx(SEG_TRACK, 'flex')}
    >
      {tabs.map((k) => (
        <button
          key={k}
          type="button"
          role="tab"
          aria-selected={value === k}
          onClick={() => onChange(k)}
          className={wrap ? wrapTabClass(value === k) : tabClass(value === k)}
        >
          {render(k)}
        </button>
      ))}
    </div>
  );
}
