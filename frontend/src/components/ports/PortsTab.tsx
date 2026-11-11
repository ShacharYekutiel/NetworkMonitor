import { useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../../api/endpoints';
import type { Device, DevicePorts, Port } from '../../api/types';
import { useAuth } from '../../hooks/auth';
import { errorMessage } from '../../lib/errors';
import { formatRelative } from '../../lib/format';
import { onPortsUpdated, refetchIfInvalidated } from '../../lib/liveCache';
import { ListIcon, PanelIcon, RefreshIcon } from '../icons';
import { useToast } from '../toast';
import { Button, Card, cx, ErrorBox, Loading, Notice } from '../ui';
import { Faceplate, FaceplateLegend } from './Faceplate';
import { HealthStrip } from './HealthStrip';
import { PortList } from './PortList';
import { PortModal } from './PortModal';

export type PortsView = 'faceplate' | 'list';
const REFRESH_TIMEOUT_MS = 90_000;

function StatusLine({ data }: { data: DevicePorts }) {
  const { t } = useTranslation();
  const s = data.status;
  let line: string;
  if (s.running) line = t('ports.collecting');
  else if (!s.last_run_at) line = t('ports.neverRun');
  else if (s.next_run_at)
    line = t('ports.statusLine', {
      ago: formatRelative(s.last_ok_at ?? s.last_run_at),
      next: formatRelative(s.next_run_at),
    });
  else line = t('ports.statusLineNoNext', { ago: formatRelative(s.last_ok_at ?? s.last_run_at) });
  return (
    <div className="text-xs text-ink-muted" data-testid="ports-status">
      <span>{line}</span>
      {data.settings.interval_min === 0 && <span> · {t('ports.autoOff')}</span>}
      {s.error && (
        <span className="text-status-down">
          {' '}
          · {t('ports.lastError')}: <bdi dir="ltr">{s.error}</bdi>
        </span>
      )}
    </div>
  );
}

function Hints({ data }: { data: DevicePorts }) {
  const { t } = useTranslation();
  const hints: string[] = [];
  if (data.status.last_run_at && data.status.sources.fdb === 'empty')
    hints.push(t('ports.hint.fdbEmpty'));
  if (data.status.arp_sources === 0) hints.push(t('ports.hint.noArp'));
  if (data.status.sources.poe === 'unmapped') hints.push(t('ports.hint.poeUnmapped'));
  if (!hints.length) return null;
  return (
    <ul className="space-y-1" data-testid="ports-hints">
      {hints.map((h) => (
        <li
          key={h}
          className="rounded-md bg-status-warning-soft px-3 py-1.5 text-xs text-status-warning"
        >
          {h}
        </li>
      ))}
    </ul>
  );
}

/** Shown when the inventory is not effective: why, and (admin) a way to switch it on. */
function NotEffective({ device, data }: { device: Device; data?: DevicePorts }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const toast = useToast();
  const enable = useMutation({
    mutationFn: () => endpoints.updateDevice(device.id, { port_inventory: 'on' }),
    onSuccess: (d) => {
      qc.setQueryData(qk.device(d.id), d);
      void qc.invalidateQueries({ queryKey: qk.devicePorts(d.id) });
      toast.push('success', t('ports.enabledToast'));
    },
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  const mode = data?.port_inventory ?? device.port_inventory;
  const cls = data?.effective_class ?? device.effective_class;
  return (
    <Notice>
      <div className="space-y-2">
        <p>
          {t('ports.notEffective', {
            mode: t(`inventoryMode.${mode}`),
            cls: t(`deviceClass.${cls}`),
          })}
        </p>
        {mode === 'auto' && <p className="text-xs">{t('ports.notEffectiveAuto')}</p>}
        {isAdmin && mode !== 'on' && (
          <Button
            size="sm"
            variant="primary"
            busy={enable.isPending}
            onClick={() => enable.mutate()}
          >
            {t('ports.enable')}
          </Button>
        )}
      </div>
    </Notice>
  );
}

export function PortsTab({ device }: { device: Device }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const view: PortsView = params.get('view') === 'list' ? 'list' : 'faceplate';
  const portParam = params.get('port');
  const selected = portParam && /^\d+$/.test(portParam) ? Number(portParam) : null;

  const q = useQuery({
    queryKey: qk.devicePorts(device.id),
    queryFn: () => endpoints.devicePorts(device.id),
    refetchOnWindowFocus: refetchIfInvalidated,
  });

  // Refresh: spinner until ports.updated for this device, or 90 s.
  const [waiting, setWaiting] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () =>
      onPortsUpdated((d) => {
        if (d.device_id !== device.id) return;
        setWaiting(false);
        if (timer.current) clearTimeout(timer.current);
        if (!d.ok) toast.push('error', t('ports.refreshFailed'));
      }),
    [device.id, t, toast],
  );
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );
  const refresh = useMutation({
    mutationFn: () => endpoints.refreshPorts(device.id),
    onSuccess: () => {
      setWaiting(true);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setWaiting(false), REFRESH_TIMEOUT_MS);
    },
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });

  const setParam = (k: string, v: string | null) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set('tab', 'ports');
        if (v === null) next.delete(k);
        else next.set(k, v);
        return next;
      },
      { replace: k !== 'port' },
    );
  const openPort = (p: Port) => setParam('port', String(p.if_index));
  const closePort = () => setParam('port', null);

  const data = q.data;
  const busy = waiting || refresh.isPending || !!data?.status.running;

  return (
    <div className="space-y-3" data-testid="ports-tab">
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} />
      {data && <HealthStrip items={data.health} />}
      {data && !data.ports_effective && <NotEffective device={device} data={data} />}
      {data && (
        <Card bodyClassName="space-y-3 p-3 sm:p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <StatusLine data={data} />
            <div className="flex flex-wrap items-center gap-2">
              {isAdmin && (
                <Button size="sm" busy={busy} onClick={() => refresh.mutate()} disabled={busy}>
                  {!busy && <RefreshIcon size={13} />} {t('ports.refresh')}
                </Button>
              )}
              <div
                className="inline-flex gap-0.5 rounded-md border border-line-strong bg-surface-sunken p-0.5"
                role="tablist"
                aria-label={t('ports.viewToggle')}
              >
                {(['faceplate', 'list'] as const).map((v) => (
                  <button
                    key={v}
                    type="button"
                    role="tab"
                    aria-selected={view === v}
                    onClick={() => setParam('view', v === 'faceplate' ? null : v)}
                    className={cx(
                      'inline-flex h-6 items-center gap-1.5 rounded-sm px-2.5 text-xs font-medium',
                      view === v
                        ? 'bg-surface-raised text-ink ring-1 ring-line-strong'
                        : 'text-ink-muted hover:text-ink',
                    )}
                  >
                    {v === 'faceplate' ? <PanelIcon size={14} /> : <ListIcon size={14} />}
                    {t(`ports.view.${v}`)}
                  </button>
                ))}
              </div>
            </div>
          </div>
          <Hints data={data} />
          {data.ports.length === 0 ? (
            <p className="py-6 text-center text-sm text-ink-muted">
              {data.status.last_run_at ? t('ports.noPorts') : t('ports.noSnapshot')}
            </p>
          ) : view === 'faceplate' ? (
            <>
              <Faceplate ports={data.ports} selected={selected} onSelect={openPort} />
              <FaceplateLegend />
            </>
          ) : (
            <PortList ports={data.ports} selected={selected} onSelect={openPort} />
          )}
        </Card>
      )}
      <PortModal
        deviceId={device.id}
        ifIndex={selected}
        onClose={closePort}
        onOpenPort={(i) => setParam('port', String(i))}
      />
    </div>
  );
}
