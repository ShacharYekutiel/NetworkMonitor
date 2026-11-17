import { useTranslation } from 'react-i18next';
import type { DeviceClass, DiscoveryResult, ScanStatus } from '../../api/types';
import { formatNumber } from '../../lib/format';
import {
  CheckIcon,
  DesktopIcon,
  XIcon,
  NasIcon,
  PrinterIcon,
  QuestionIcon,
  RouterIcon,
  ServerIcon,
  SwitchIcon,
  WifiIcon,
} from '../icons';
import { CHIP, CHIP_TONE, cx, Ltr, Spinner } from '../ui';

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

export function DeviceClassIcon({ cls, size = 14 }: { cls: DeviceClass; size?: number }) {
  const Icon = CLASS_ICON[cls] ?? QuestionIcon;
  return <Icon size={size} />;
}

export function DeviceClassBadge({ cls }: { cls: DeviceClass }) {
  const { t } = useTranslation();
  return (
    <span
      // Device classes are not statuses: a neutral chip with the class icon (design system).
      className={cx(CHIP, CHIP_TONE.neutral, 'text-ink')}
    >
      <DeviceClassIcon cls={cls} size={12} />
      {t(`deviceClass.${cls}`)}
    </span>
  );
}

/** The vendor slug as the API reports it (data-model.md vendor table), e.g. "opnsense", "hp". */
export function VendorBadge({ vendor }: { vendor: string | null }) {
  if (!vendor) return <span className="text-ink-subtle">—</span>;
  return (
    <span className={cx(CHIP, CHIP_TONE.neutral, 'text-ink')}>
      <Ltr>{vendor}</Ltr>
    </span>
  );
}

const SCAN_TONE: Record<ScanStatus, string> = {
  running: 'bg-accent-soft text-accent-ink',
  completed: 'bg-status-up-soft text-status-up',
  cancelled: 'bg-status-paused-soft text-status-paused',
  failed: 'bg-status-down-soft text-status-down',
};

export function ScanStatusBadge({ status }: { status: ScanStatus }) {
  const { t } = useTranslation();
  return (
    <span className={cx(CHIP, 'font-semibold', SCAN_TONE[status] ?? SCAN_TONE.failed)}>
      {status === 'running' && <Spinner className="h-3 w-3" />}
      {status === 'completed' && <CheckIcon size={12} strokeWidth={2.5} />}
      {status === 'failed' && <XIcon size={12} strokeWidth={2.5} />}
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
        'inline-flex min-h-[20px] items-center rounded-sm px-1.5 font-mono text-[11px] font-medium',
        !wrap && 'whitespace-nowrap',
        tone === 'ok' && 'bg-status-up-soft text-status-up',
        tone === 'info' && 'bg-accent-soft text-accent-ink',
        tone === 'muted' && 'border border-line bg-surface-sunken text-ink-muted',
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
