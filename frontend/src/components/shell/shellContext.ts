import { createContext, useContext } from 'react';
import type { MenuTab } from '../../lib/menu';

/** What pages may ask of the premium shell (e.g. the home's "All screens" row). */
export type Shell = { openMenu: (tab?: MenuTab) => void };
export const ShellContext = createContext<Shell>({ openMenu: () => undefined });
export const useShell = (): Shell => useContext(ShellContext);
