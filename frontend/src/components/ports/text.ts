import { useTranslation } from 'react-i18next';
import type { IfStatus, Port } from '../../api/types';
import { connectedView, formatSpeed, poeView } from '../../lib/ports';
import { formatNumber, formatValue } from '../../lib/format';

/** Translated texts of a port shared by the tiles, the list (and its filter) and the modal. */
export function usePortText() {
  const { t } = useTranslation();
  const state = (p: Pick<Port, 'state'>) => t(`ports.state.${p.state}`);
  const ifStatus = (s: IfStatus | string) => t(`ports.ifStatus.${s}`, { defaultValue: s });
  const connected = (p: Port): string => {
    const c = connectedView(p);
    switch (c.kind) {
      case 'neighbor':
        return c.name;
      case 'uplinkMacs':
        return t('ports.macCount', { count: c.count });
      case 'mac':
        return c.more ? `${c.text} +${c.more}` : c.text;
      default:
        return '';
    }
  };
  const poe = (p: Port): string => {
    const v = poeView(p);
    if (!v) return '—';
    if (v.kind === 'watts') return `${formatNumber(v.w, 1)} W`;
    return v.cls === null ? t('ports.poeOn') : t('ports.poeOnClass', { cls: v.cls });
  };
  const speed = (p: Pick<Port, 'speed_bps' | 'state'>) =>
    p.state === 'up' ? (formatSpeed(p.speed_bps) ?? '—') : '—';
  const rxtx = (p: Pick<Port, 'in_bps' | 'out_bps'>) =>
    p.in_bps === null && p.out_bps === null
      ? '—'
      : `${formatValue(p.in_bps, 'bit/s')} / ${formatValue(p.out_bps, 'bit/s')}`;
  const mode = (p: Pick<Port, 'mode'>) => (p.mode ? t(`ports.mode.${p.mode}`) : '—');
  const kindLabel = (p: Pick<Port, 'connected_kind' | 'state'>) =>
    t(`ports.connectedKind.${p.connected_kind}`);
  /** "Port 7, up, client, VLAN 10" */
  const ariaLabel = (p: Port) => {
    const parts = [t('ports.portN', { name: p.name || p.descr || p.if_index }), state(p)];
    if (p.state === 'up') parts.push(kindLabel(p));
    if (p.alert) parts.push(t('ports.alert'));
    if (p.pvid !== null) parts.push(t('ports.vlanN', { vlan: p.pvid }));
    // [2b-3]
    if (p.lag) parts.push(t('ports.lag.memberOf', { name: p.lag.name }));
    return parts.join(', ');
  };
  /** Translated column texts searched by the list filter (on top of the raw fields). */
  const searchTexts = (p: Port) => [
    state(p),
    connected(p),
    mode(p),
    poe(p),
    kindLabel(p),
    p.lag?.name ?? '',
  ];
  return { state, ifStatus, connected, poe, speed, rxtx, mode, ariaLabel, searchTexts, kindLabel };
}
