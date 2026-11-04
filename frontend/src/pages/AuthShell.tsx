import type { ReactNode } from 'react';
import { LanguageSwitcher, ThemeToggle } from '../components/Controls';
import { PulseIcon } from '../components/icons';

export function AuthShell({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
}) {
  return (
    <div className="flex min-h-screen flex-col">
      <div className="flex justify-end gap-2 p-3">
        <LanguageSwitcher />
        <ThemeToggle />
      </div>
      <div className="flex flex-1 items-start justify-center px-4 pt-[8vh]">
        <div className="w-full max-w-sm">
          <div className="mb-6 flex items-center justify-center gap-2 text-2xl font-semibold">
            <PulseIcon size={28} className="text-green-600" />
            NetworkMonitor
          </div>
          <div className="rounded-lg bg-white p-6 shadow-sm ring-1 ring-slate-200 dark:bg-slate-900 dark:ring-slate-800">
            <h1 className="text-lg font-semibold">{title}</h1>
            {subtitle && <p className="mt-1 text-sm text-slate-500">{subtitle}</p>}
            <div className="mt-5">{children}</div>
          </div>
        </div>
      </div>
    </div>
  );
}
