import { useCallback, useLayoutEffect, useRef, useState } from 'react';

/**
 * Height that lets an element reach the bottom of the viewport (minus the page's bottom
 * padding), for canvases that should fill the space under the header (the topology map).
 * Re-measured on window resize and whenever something above it in <main> changes size
 * (e.g. the maintenance banner appears or the filter row wraps). Never below `min`.
 */
export function useFillHeight<T extends HTMLElement>(min: number, deps: unknown[] = []) {
  const ref = useRef<T>(null);
  const [height, setHeight] = useState<number | undefined>(undefined);

  const measure = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const main = el.closest('main');
    // The page's padding box inside <main> (its bottom padding stays visible under the canvas).
    let wrapper: HTMLElement | null = el;
    while (wrapper && wrapper.parentElement && wrapper.parentElement !== main) {
      wrapper = wrapper.parentElement;
    }
    const pad = wrapper && main ? parseFloat(getComputedStyle(wrapper).paddingBottom) || 0 : 0;
    const top = el.getBoundingClientRect().top + (main?.scrollTop ?? 0);
    const mainTop = main ? main.getBoundingClientRect().top : 0;
    const bottom = main ? mainTop + main.clientHeight : window.innerHeight;
    const h = Math.max(min, Math.floor(bottom - top - pad));
    setHeight((prev) => (prev === h ? prev : h));
  }, [min]);

  useLayoutEffect(() => {
    measure();
    const el = ref.current;
    const main = el?.closest('main');
    window.addEventListener('resize', measure);
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => measure()) : null;
    if (ro && main) {
      ro.observe(main);
      // Everything above the element inside <main>: banners, header, filter rows.
      let n: Element | null = el ?? null;
      while (n && n !== main) {
        let s = n.previousElementSibling;
        while (s) {
          ro.observe(s);
          s = s.previousElementSibling;
        }
        n = n.parentElement;
      }
    }
    return () => {
      window.removeEventListener('resize', measure);
      ro?.disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [measure, ...deps]);

  return { ref, height };
}
