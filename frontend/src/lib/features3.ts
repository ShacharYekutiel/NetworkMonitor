// [3] Status lines of the phase-3 rows on the Features page (architecture.md § 13.10). Pure.
import type {
  BackupRunnerStatus,
  FlowStatus,
  KerberosSettings,
  NewDeviceStatus,
  TelegramPoller,
} from '../api/types3';
import type { StatusLine } from './features';

export function flowStatusLines(s: FlowStatus | null | undefined): StatusLine[] {
  if (!s) return [];
  const out: StatusLine[] = [];
  const listening = s.receivers.filter((r) => r.state === 'listening').map((r) => r.port);
  if (listening.length)
    out.push({
      key: 'features.status.listening',
      params: { proto: 'UDP', port: listening.join(', ') },
      tone: 'ok',
    });
  for (const r of s.receivers.filter((x) => x.state === 'error'))
    out.push({
      key: 'features.status.error',
      params: { proto: 'UDP', port: r.port, error: r.error ?? '' },
      tone: 'error',
    });
  if (!listening.length && !out.length)
    out.push({ key: 'features.status.socketOff', params: { proto: 'UDP' }, tone: 'muted' });
  return out;
}

export function backupStatusLines(
  s: BackupRunnerStatus | null | undefined,
  fmtTime: (iso: string) => string,
): StatusLine[] {
  if (!s) return [];
  const out: StatusLine[] = [];
  if (s.next_schedule_at)
    out.push({
      key: 'features.status.nextBackup',
      params: { time: fmtTime(s.next_schedule_at), n: s.eligible_devices },
      tone: 'ok',
    });
  if (s.running || s.queued)
    out.push({
      key: 'features.status.backupRunning',
      params: { running: s.running, queued: s.queued },
      tone: 'muted',
    });
  if (s.driver_errors.length)
    out.push({
      key: 'features.status.driverErrors',
      params: { n: s.driver_errors.length },
      tone: 'error',
    });
  return out;
}

export function newDeviceStatusLines(
  s: NewDeviceStatus | null | undefined,
  fmtDate: (iso: string) => string,
): StatusLine[] {
  if (!s) return [];
  if (s.learning && s.learning_until)
    return [
      {
        key: 'features.status.learningUntil',
        params: { date: fmtDate(s.learning_until) },
        tone: 'muted',
      },
    ];
  return [
    {
      key: 'features.status.watching',
      params: { n: s.switches_in_scope, newCount: s.new_unacknowledged },
      tone: 'ok',
    },
  ];
}

export function kerberosStatusLines(k: KerberosSettings | null | undefined): StatusLine[] {
  if (!k) return [];
  const out: StatusLine[] = [];
  if (!k.platform_supported)
    out.push({ key: 'features.status.kerberosUnsupported', tone: 'error' });
  if (k.keytab_set)
    out.push({
      key: 'features.status.keytab',
      params: { n: k.keytab_entries.length },
      tone: k.state === 'ready' ? 'ok' : 'muted',
    });
  else out.push({ key: 'features.status.noKeytab', tone: 'error' });
  if (k.state === 'error')
    out.push({
      key: 'features.status.kerberosError',
      params: { error: k.error ?? '' },
      tone: 'error',
    });
  return out;
}

export function pollerStatusLines(p: TelegramPoller[] | null | undefined): StatusLine[] {
  if (!p) return [];
  if (!p.length) return [{ key: 'features.status.noPoller', tone: 'muted' }];
  return p.map((x): StatusLine =>
    x.state === 'error'
      ? {
          key: 'features.status.pollerError',
          params: { bot: x.bot_username, error: x.last_error ?? '' },
          tone: 'error' as const,
        }
      : {
          key: x.state === 'polling' ? 'features.status.pollerOk' : 'features.status.pollerIdle',
          params: { bot: x.bot_username },
          tone: x.state === 'polling' ? ('ok' as const) : ('muted' as const),
        },
  );
}
