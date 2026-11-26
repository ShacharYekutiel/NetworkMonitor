import type { JSX } from 'react';
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
  tone: 'ok' | 'muted' | 'info' | 'bad';
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
        tone === 'bad' && 'bg-status-down-soft text-status-down',
      )}
    >
      {children}
    </span>
  );
}

/** ICMP / SNMP / open ports chips of a result. */
export function ReachabilityChips({
  r,
  sshChecked,
}: {
  r: DiscoveryResult;
  /** [profiles] the scan ran the SSH login check (else no SSH badge) */
  sshChecked?: boolean;
}) {
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
      {/* [profiles] the connection profile whose credentials answered */}
      {r.snmp_ok && r.snmp_profile_name && (
        <span
          className="inline-flex min-h-[20px] items-center rounded-sm bg-accent-soft px-1.5 text-[11px] font-medium text-accent-ink"
          title={t('discovery.answeredProfile', { name: r.snmp_profile_name })}
          data-testid="result-snmp-profile"
        >
          <bdi>{r.snmp_profile_name}</bdi>
        </span>
      )}
      {sshChecked && <SshBadge r={r} />}
      {r.open_ports.length > 0 && (
        <Chip tone="muted" wrap>
          <span title={t('discovery.openPorts')}>TCP {r.open_ports.join(' ')}</span>
        </Chip>
      )}
    </span>
  );
}

/** [profiles] Result of the opt-in SSH login check: not tried / failed / logged in (with what). */
export function SshBadge({ r }: { r: DiscoveryResult }) {
  const { t } = useTranslation();
  const what =
    r.ssh_credential === 'profile'
      ? (r.ssh_profile_name ?? t('discovery.deletedProfile', { id: r.ssh_profile_id ?? 0 }))
      : t('discovery.sshTypedShort');
  if (r.ssh_ok)
    return (
      <span data-testid="result-ssh" title={t('discovery.sshOkTitle', { what })}>
        <Chip tone="ok">
          SSH ✓
          <span className="ms-1 font-sans">
            <bdi>{what}</bdi>
          </span>
        </Chip>
      </span>
    );
  if (r.ssh_attempted)
    return (
      <span data-testid="result-ssh" title={t('discovery.sshFailedTitle')}>
        <Chip tone="bad">SSH ✗ {t('discovery.sshFailed')}</Chip>
      </span>
    );
  return (
    <span data-testid="result-ssh" title={t('discovery.sshNotTriedTitle')}>
      <Chip tone="muted">SSH {t('discovery.sshNotTried')}</Chip>
    </span>
  );
}
