// [1.3.1] Search over every Settings tab (src/lib/settingsIndex.ts).
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { useTranslation } from 'react-i18next';
import type { Features } from '../api/types';
import { featureTab, type SettingsTab } from '../lib/features';
import {
  buildDocs,
  entryAvailable,
  featureSel,
  searchSettings,
  settingsEntries,
  type SettingsDoc,
} from '../lib/settingsIndex';
import { SearchIcon, XIcon } from './icons';
import { cx, Input } from './ui';

const HIGHLIGHT = ['outline', 'outline-2', 'outline-offset-2', 'outline-amber-400'];
const HIGHLIGHT_MS = 2500;

type Target = { selectors: string[]; n: number };

/** Scrolls to the first element found (polling while the tab renders) and highlights it. */
function useScrollTarget(target: Target | null) {
  useEffect(() => {
    if (!target) return;
    let tries = 0;
    let poll: ReturnType<typeof setTimeout> | undefined;
    let clear: ReturnType<typeof setTimeout> | undefined;
    let el: HTMLElement | null = null;
    const off = () => {
      if (!el) return;
      el.classList.remove(...HIGHLIGHT);
      delete el.dataset.highlight;
    };
    const tick = () => {
      for (const s of target.selectors) {
        el = document.querySelector<HTMLElement>(s);
        if (el) break;
      }
      if (!el) {
        if (++tries < 60) poll = setTimeout(tick, 50);
        return;
      }
      el.scrollIntoView?.({ behavior: 'smooth', block: 'center' });
      el.classList.add(...HIGHLIGHT);
      el.dataset.highlight = 'true';
      clear = setTimeout(off, HIGHLIGHT_MS);
    };
    tick();
    return () => {
      clearTimeout(poll);
      clearTimeout(clear);
      off();
    };
  }, [target]);
}

export function SettingsSearch({
  features,
  onTab,
}: {
  features: Features;
  onTab: (tab: SettingsTab) => void;
}) {
  const { t, i18n } = useTranslation();
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [target, setTarget] = useState<Target | null>(null);
  const box = useRef<HTMLDivElement>(null);
  useScrollTarget(target);

  const lang = i18n.language;
  const docs = useMemo(() => {
    const cur = i18n.getFixedT(lang);
    const en = lang === 'en' ? undefined : i18n.getFixedT('en');
    return buildDocs(settingsEntries(), (k) => cur(k), en && ((k) => en(k)));
  }, [i18n, lang]);
  const results = useMemo(() => searchSettings(docs, query), [docs, query]);
  useEffect(() => setActive(0), [query]);

  const go = (d: SettingsDoc) => {
    const e = d.entry;
    if (entryAvailable(e, features)) {
      onTab(e.tab);
      setTarget((p) => ({ selectors: e.selectors, n: (p?.n ?? 0) + 1 }));
    } else {
      const f = e.feature![0];
      onTab(featureTab(f));
      setTarget((p) => ({ selectors: [featureSel(f)], n: (p?.n ?? 0) + 1 }));
    }
    setOpen(false);
  };

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      setQuery('');
      setOpen(false);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const d = results[active] ?? results[0];
      if (d) go(d);
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      setOpen(true);
      setActive((a) => Math.min(a + 1, results.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    }
  };

  const requires = (d: SettingsDoc) => {
    const f = d.entry.feature ?? [];
    return t('settingsSearch.requires', {
      feature: f.map((n) => t(`features.name.${n}`)).join(' / '),
      tab: t(`settings.tab.${featureTab(f[0])}`),
    });
  };

  const show = open && query.trim().length > 0;
  return (
    <div
      ref={box}
      className="relative w-full max-w-xl"
      onBlur={(e) => {
        if (!box.current?.contains(e.relatedTarget as Node | null)) setOpen(false);
      }}
    >
      <SearchIcon
        size={14}
        className="pointer-events-none absolute start-2.5 top-1/2 -translate-y-1/2 text-slate-400"
      />
      <Input
        type="search"
        role="combobox"
        aria-expanded={show}
        aria-controls="settings-search-results"
        aria-label={t('settingsSearch.label')}
        placeholder={t('settingsSearch.placeholder')}
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKey}
        className="ps-8 pe-8"
        data-testid="settings-search"
      />
      {query && (
        <button
          type="button"
          aria-label={t('settingsSearch.clear')}
          onClick={() => {
            setQuery('');
            setOpen(false);
          }}
          className="absolute end-2 top-1/2 -translate-y-1/2 rounded p-0.5 text-slate-400 hover:text-slate-700 dark:hover:text-slate-200"
        >
          <XIcon size={14} />
        </button>
      )}
      {show && (
        <div
          id="settings-search-results"
          role="listbox"
          aria-label={t('settingsSearch.label')}
          className="absolute inset-x-0 top-full z-30 mt-1 max-h-96 overflow-auto rounded-md bg-white py-1 shadow-lg ring-1 ring-slate-200 dark:bg-slate-900 dark:ring-slate-700"
          data-testid="settings-search-results"
        >
          {results.length === 0 && (
            <p className="px-3 py-2 text-sm text-slate-500">{t('settingsSearch.none')}</p>
          )}
          {results.map((d, i) => {
            const ok = entryAvailable(d.entry, features);
            const isCard = d.entry.labelKey === d.entry.cardTitleKey;
            return (
              <button
                key={d.entry.id}
                type="button"
                role="option"
                aria-selected={i === active}
                tabIndex={-1}
                onMouseDown={(e) => e.preventDefault()}
                onMouseEnter={() => setActive(i)}
                onClick={() => go(d)}
                className={cx(
                  'block w-full px-3 py-1.5 text-start',
                  i === active && 'bg-sky-50 dark:bg-slate-800',
                )}
                data-testid="settings-search-result"
                data-entry={d.entry.id}
              >
                <span
                  className={cx(
                    'block text-sm font-medium',
                    !ok && 'text-slate-500 dark:text-slate-400',
                  )}
                >
                  {d.label}
                </span>
                <span className="block text-xs text-slate-500">
                  {d.tab}
                  {!isCard && (
                    <>
                      <span className="mx-1" aria-hidden="true">
                        ›
                      </span>
                      {d.card}
                    </>
                  )}
                </span>
                {!ok && (
                  <span className="mt-0.5 block text-xs text-amber-700 dark:text-amber-400">
                    {requires(d)}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
