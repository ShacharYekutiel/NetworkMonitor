import { useTranslation } from 'react-i18next';
import type { DeviceClass, DiscoveryResult, ScanStatus } from '../../api/types';
import { formatNumber } from '../../lib/format';
import {
  DesktopIcon,
  NasIcon,
  PrinterIcon,
  QuestionIcon,
  RouterIcon,
  ServerIcon,
  SwitchIcon,
  WifiIcon,
} from '../icons';
import { cx, Ltr } from '../ui';

const CLASS_ICON: Record<DeviceClass, (p: { size?: number; className?: string }) => JSX.Element> = {
  router: RouterIcon,
  switch: SwitchIcon,
  access_point: WifiIcon,
  printer: PrinterIcon,
  nas: NasIcon,
  server: ServerIcon,
  workstation: DesktopIcon,
  unknown: QuestionIcon,
};

const CLASS_TONE: Record<DeviceClass, string> = {
  router:
    'bg-violet-50 text-violet-800 ring-violet-200 dark:bg-violet-950 dark:text-violet-200 dark:ring-violet-800',
  switch: 'bg-sky-50 text-sky-800 ring-sky-200 dark:bg-sky-950 dark:text-sky-200 dark:ring-sky-800',
  access_point:
    'bg-teal-50 text-teal-800 ring-teal-200 dark:bg-teal-950 dark:text-teal-200 dark:ring-teal-800',
  printer:
    'bg-orange-50 text-orange-800 ring-orange-200 dark:bg-orange-950 dark:text-orange-200 dark:ring-orange-800',
  nas: 'bg-indigo-50 text-indigo-800 ring-indigo-200 dark:bg-indigo-950 dark:text-indigo-200 dark:ring-indigo-800',
  server:
    'bg-cyan-50 text-cyan-800 ring-cyan-200 dark:bg-cyan-950 dark:text-cyan-200 dark:ring-cyan-800',
  workstation:
    'bg-fuchsia-50 text-fuchsia-800 ring-fuchsia-200 dark:bg-fuchsia-950 dark:text-fuchsia-200 dark:ring-fuchsia-800',
  unknown:
    'bg-slate-50 text-slate-600 ring-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700',
};

export function DeviceClassIcon({ cls, size = 14 }: { cls: DeviceClass; size?: number }) {
  const Icon = CLASS_ICON[cls] ?? QuestionIcon;
  return <Icon size={size} />;
}

export function DeviceClassBadge({ cls }: { cls: DeviceClass }) {
  const { t } = useTranslation();
  return (
    <span
      className={cx(
        'inline-flex items-center gap-1 whitespace-nowrap rounded px-1.5 py-0.5 text-xs font-medium ring-1 ring-inset',
        CLASS_TONE[cls] ?? CLASS_TONE.unknown,
      )}
    >
      <DeviceClassIcon cls={cls} size={12} />
      {t(`deviceClass.${cls}`)}
    </span>
  );
}

/** The vendor slug as the API reports it (data-model.md vendor table), e.g. "opnsense", "hp". */
export function VendorBadge({ vendor }: { vendor: string | null }) {
  if (!vendor) return <span className="text-slate-400">—</span>;
  return (
    <span className="inline-flex whitespace-nowrap rounded bg-slate-100 px-1.5 py-0.5 text-xs font-medium text-slate-700 dark:bg-slate-800 dark:text-slate-200">
      <Ltr>{vendor}</Ltr>
    </span>
  );
}

const SCAN_TONE: Record<ScanStatus, string> = {
  running: 'bg-sky-600 text-white',
  completed: 'bg-green-600 text-white',
  cancelled: 'bg-gray-400 text-white',
  failed: 'bg-red-600 text-white',
};

export function ScanStatusBadge({ status }: { status: ScanStatus }) {
  const { t } = useTranslation();
  return (
    <span
      className={cx(
        'inline-flex items-center gap-1 whitespace-nowrap rounded px-1.5 py-0.5 text-xs font-medium',
        SCAN_TONE[status] ?? SCAN_TONE.failed,
      )}
    >
      {status === 'running' && (
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-white" aria-hidden="true" />
      )}
      {t(`discovery.status.${status}`)}
    </span>
  );
}

function Chip({
  children,
  tone,
  wrap,
}: {
  children: React.ReactNode;
  tone: 'ok' | 'muted' | 'info';
  wrap?: boolean;
}) {
  return (
    <span
      className={cx(
        'inline-flex items-center rounded px-1 py-px text-[11px] font-medium ring-1 ring-inset',
        !wrap && 'whitespace-nowrap',
        tone === 'ok' &&
          'bg-green-50 text-green-800 ring-green-200 dark:bg-green-950 dark:text-green-300 dark:ring-green-900',
        tone === 'info' &&
          'bg-sky-50 text-sky-800 ring-sky-200 dark:bg-sky-950 dark:text-sky-300 dark:ring-sky-900',
        tone === 'muted' &&
          'bg-slate-50 text-slate-600 ring-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700',
      )}
    >
      {children}
    </span>
  );
}

/** ICMP / SNMP / open ports chips of a result. */
export function ReachabilityChips({ r }: { r: DiscoveryResult }) {
  const { t } = useTranslation();
  return (
    <span dir="ltr" className="inline-flex flex-wrap gap-1">
      {r.ping_ok && (
        <Chip tone="ok">
          ICMP
          {r.ping_rtt_ms !== null && (
            <span className="ms-1 font-normal opacity-80">{formatNumber(r.ping_rtt_ms, 1)} ms</span>
          )}
        </Chip>
      )}
      {r.snmp_ok && (
        <Chip tone="info">
          <span title={r.snmp_credential === 'extra' ? t('discovery.extraCredential') : undefined}>
            SNMP {r.snmp_version ?? ''}
            {r.snmp_credential === 'extra' && '*'}
          </span>
        </Chip>
      )}
      {r.open_ports.length > 0 && (
        <Chip tone="muted" wrap>
          <span title={t('discovery.openPorts')}>TCP {r.open_ports.join(' ')}</span>
        </Chip>
      )}
    </span>
  );
}
