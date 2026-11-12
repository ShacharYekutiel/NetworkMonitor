// [3b] A small accessible popup menu (role="menu") opened at a point: arrow keys / Home / End /
// type-ahead, Enter or Space to activate, Esc / Tab / outside click / scroll / resize to close.
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { nextMenuIndex, placeMenu, type Point } from '../../lib/treeMenu';
import { cx } from '../ui';

export type ContextMenuEntry =
  | {
      type: 'item';
      key: string;
      label: string;
      icon?: ReactNode;
      danger?: boolean;
      disabled?: boolean;
      title?: string;
    }
  | { type: 'separator'; key: string };

/** Why the menu closed: keyboard / activation return focus to the opener, a click elsewhere does not. */
export type CloseReason = 'escape' | 'tab' | 'select' | 'outside' | 'viewport';

function isRtl(): boolean {
  return (
    typeof document !== 'undefined' &&
    (document.documentElement.dir || getComputedStyle(document.documentElement).direction) === 'rtl'
  );
}

export function ContextMenu({
  anchor,
  label,
  entries,
  onSelect,
  onClose,
  testId = 'context-menu',
}: {
  anchor: Point;
  label: string;
  entries: ContextMenuEntry[];
  onSelect: (key: string) => void;
  onClose: (reason: CloseReason) => void;
  testId?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const items = entries.flatMap((e) => (e.type === 'item' ? [e] : []));
  const [active, setActive] = useState(() => nextMenuIndex(items, -1, 'Home'));
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // Measure, then place inside the viewport (mirrored in RTL).
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    setPos(
      placeMenu(
        anchor,
        { width: r.width, height: r.height },
        { width: window.innerWidth, height: window.innerHeight },
        isRtl(),
      ),
    );
  }, [anchor, entries.length]);

  useEffect(() => {
    if (active >= 0) itemRefs.current[active]?.focus();
    else ref.current?.focus();
  }, [active, pos]);

  useEffect(() => {
    const onPointer = (e: Event) => {
      if (!ref.current?.contains(e.target as Node)) onCloseRef.current('outside');
    };
    const onViewport = (e: Event) => {
      if (e.type === 'scroll' && ref.current?.contains(e.target as Node)) return;
      onCloseRef.current('viewport');
    };
    document.addEventListener('mousedown', onPointer, true);
    document.addEventListener('touchstart', onPointer, true);
    window.addEventListener('resize', onViewport);
    window.addEventListener('blur', onViewport);
    document.addEventListener('scroll', onViewport, true);
    return () => {
      document.removeEventListener('mousedown', onPointer, true);
      document.removeEventListener('touchstart', onPointer, true);
      window.removeEventListener('resize', onViewport);
      window.removeEventListener('blur', onViewport);
      document.removeEventListener('scroll', onViewport, true);
    };
  }, []);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      onClose('escape');
      return;
    }
    if (e.key === 'Tab') {
      e.preventDefault();
      onClose('tab');
      return;
    }
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      const it = items[active];
      if (it && !it.disabled) onSelect(it.key);
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const next = nextMenuIndex(items, active, e.key);
    if (next !== active || ['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) {
      e.preventDefault();
      setActive(next);
    }
  };

  let index = -1;
  return createPortal(
    <div
      ref={ref}
      role="menu"
      aria-label={label}
      aria-orientation="vertical"
      data-testid={testId}
      tabIndex={-1}
      onKeyDown={onKeyDown}
      onContextMenu={(e) => e.preventDefault()}
      style={{
        position: 'fixed',
        left: pos?.left ?? anchor.x,
        top: pos?.top ?? anchor.y,
        visibility: pos ? 'visible' : 'hidden',
      }}
      className="z-[70] max-h-[calc(100vh-8px)] min-w-[12rem] max-w-[min(20rem,calc(100vw-8px))] overflow-y-auto rounded-md border border-slate-200 bg-white py-1 text-sm shadow-lg focus:outline-none dark:border-slate-700 dark:bg-slate-900"
    >
      {entries.map((e) => {
        if (e.type === 'separator')
          return (
            <div
              key={e.key}
              role="separator"
              className="my-1 border-t border-slate-200 dark:border-slate-700"
            />
          );
        const i = ++index;
        return (
          <button
            key={e.key}
            ref={(el) => {
              itemRefs.current[i] = el;
            }}
            type="button"
            role="menuitem"
            tabIndex={i === active ? 0 : -1}
            aria-disabled={e.disabled || undefined}
            title={e.title}
            data-danger={e.danger || undefined}
            onMouseMove={() => !e.disabled && i !== active && setActive(i)}
            onClick={() => !e.disabled && onSelect(e.key)}
            className={cx(
              'flex w-full items-center gap-2 px-3 py-1.5 text-start focus:outline-none',
              e.disabled
                ? 'cursor-default text-slate-400 dark:text-slate-500'
                : e.danger
                  ? 'text-red-700 focus:bg-red-50 dark:text-red-400 dark:focus:bg-red-950/50'
                  : 'text-slate-700 focus:bg-slate-100 dark:text-slate-200 dark:focus:bg-slate-800',
            )}
          >
            <span className="flex w-4 shrink-0 justify-center" aria-hidden="true">
              {e.icon}
            </span>
            <span className="min-w-0 flex-1 truncate">{e.label}</span>
          </button>
        );
      })}
    </div>,
    document.body,
  );
}
