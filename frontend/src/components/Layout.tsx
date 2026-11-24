import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Outlet, useLocation, useNavigationType } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useSensorTypes } from '../hooks/queries';
import { readMenuTab, readPinned, writeMenuTab, writePinned, type MenuTab } from '../lib/menu';
import { GlobalMaintenanceBanner } from './Maintenance';
import { MenuPanel } from './shell/MenuPanel';
import { ShellContext } from './shell/shellContext';
import { TopBar } from './shell/TopBar';
import { TreeMenuProvider } from './treemenu/TreeContextMenu';
import { cx } from './ui';

export { Mark } from './shell/TopBar';

/** Pages that show the device tree context (the menu opens on the Inventory there by default). */
export function isTreePage(pathname: string): boolean {
  return (
    pathname === '/' ||
    pathname.startsWith('/groups/') ||
    pathname.startsWith('/devices/') ||
    pathname.startsWith('/sensors/')
  );
}

const MENU_ID = 'nm-menu';

function reducedMotion(): boolean {
  return !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
}

/**
 * Premium shell (docs/design-system/premium/BUILD.md § 3.2): a translucent top bar over the page,
 * the ☰ menu as a sheet (or docked when pinned), and a push transition between pages.
 */
export function Layout() {
  const { i18n } = useTranslation();
  useSensorTypes(); // [1.3.1] named value maps for enum value texts everywhere
  const location = useLocation();
  const navType = useNavigationType();
  const [pinned, setPinnedState] = useState(readPinned);
  const [open, setOpen] = useState(false);
  const [tab, setTabState] = useState<MenuTab>(readMenuTab);

  const setPinned = useCallback((p: boolean) => {
    setPinnedState(p);
    writePinned(p);
    if (!p) setOpen(false);
  }, []);
  const setTab = useCallback((t: MenuTab) => {
    setTabState(t);
    writeMenuTab(t);
  }, []);
  const close = useCallback(() => setOpen(false), []);

  const shell = useMemo(
    () => ({
      openMenu: (t?: MenuTab) => {
        if (t) setTab(t);
        if (!readPinned()) setOpen(true);
      },
    }),
    [setTab],
  );

  // A page change closes the sheet (a pinned menu stays).
  useEffect(() => setOpen(false), [location.pathname]);

  // The ☰ button opens/closes the sheet; pressed while the menu is locked it unlocks (§ 6b).
  const onMenu = () => {
    if (pinned) return setPinned(false);
    setOpen((o) => !o);
  };

  // Push transition: slide 30px + fade (forward from the inline end, back from the start).
  const pageRef = useRef<HTMLDivElement>(null);
  const mainRef = useRef<HTMLElement>(null);
  const first = useRef(true);
  useLayoutEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    mainRef.current?.scrollTo?.({ top: 0 });
    const el = pageRef.current;
    if (!el || reducedMotion() || typeof el.animate !== 'function') return;
    const rtl = i18n.dir() === 'rtl';
    const back = navType === 'POP';
    const dx = (back ? -30 : 30) * (rtl ? -1 : 1);
    el.animate(
      [
        { opacity: 0, transform: `translateX(${dx}px)` },
        { opacity: 1, transform: 'none' },
      ],
      { duration: 420, easing: 'cubic-bezier(.32,.72,0,1)' },
    );
    // Only the path counts as a new page (tabs and filters live in the query string).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname]);

  return (
    <ShellContext.Provider value={shell}>
      {/* the group / device / sensor context menu on every screen (tree, rows, cards, nodes) */}
      <TreeMenuProvider>
        <div className="relative flex h-screen overflow-hidden print:block print:h-auto print:overflow-visible">
          {pinned && (
            <MenuPanel
              id={MENU_ID}
              pinned
              open
              tab={tab}
              onTab={setTab}
              onPin={setPinned}
              onClose={close}
            />
          )}
          <main
            ref={mainRef}
            className="relative min-w-0 flex-1 overflow-y-auto pt-header [container-type:inline-size] print:overflow-visible print:pt-0"
          >
            <div className="print:hidden">
              <GlobalMaintenanceBanner />
            </div>
            <div
              ref={pageRef}
              className={cx(
                'pb-16 pt-2 print:max-w-none print:p-0',
                // pinned: the column fills the space next to the menu with 36px gutters (§ 6b)
                pinned ? 'px-9' : 'mx-auto max-w-page px-6',
              )}
            >
              <Outlet />
            </div>
          </main>
          <TopBar menuId={MENU_ID} menuOpen={open || pinned} onMenu={onMenu} />
          {!pinned && (
            <>
              <div
                aria-hidden="true"
                onClick={close}
                className={cx(
                  'fixed inset-0 z-50 bg-overlay backdrop-blur-[4px] transition-opacity duration-300 print:hidden',
                  open ? 'opacity-100' : 'pointer-events-none opacity-0',
                )}
              />
              <MenuPanel
                id={MENU_ID}
                pinned={false}
                open={open}
                tab={tab}
                onTab={setTab}
                onPin={setPinned}
                onClose={close}
              />
            </>
          )}
        </div>
      </TreeMenuProvider>
    </ShellContext.Provider>
  );
}
