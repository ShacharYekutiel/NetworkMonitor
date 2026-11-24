// [if-noise] Device page: where the device's effective SNMP credentials come from, naming the group
// they are inherited from (a link), and a warning for the built-in default while SNMP sensors use it.
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import type { Device, Sensor } from '../api/types';
import { useSensorTypes, useTree } from '../hooks/queries';
import { snmpSourceInfo } from '../lib/noise';
import { flattenGroups } from '../lib/tree';
import { BANNER, BANNER_TONE, cx } from './ui';

export function SnmpSourceLine({ device, sensors }: { device: Device; sensors: Sensor[] }) {
  const { t } = useTranslation();
  const tree = useTree();
  const types = useSensorTypes();
  const info = snmpSourceInfo(device, sensors, types.data);
  const group =
    info.groupId !== null
      ? flattenGroups(tree.data?.groups ?? []).find((x) => x.group.id === info.groupId)?.group
      : undefined;
  const version = device.snmp_credentials ? ` (${device.snmp_credentials.version})` : '';
  if (info.warn)
    return (
      <span
        className={cx(BANNER, BANNER_TONE.warning, 'py-1 text-xs')}
        data-testid="snmp-source"
        data-source="default"
        role="note"
      >
        <b className="font-semibold">{t('snmp.label')}:</b> {t('snmp.defaultWarning')}
      </span>
    );
  return (
    <span className="text-xs text-ink-muted" data-testid="snmp-source" data-source={info.source}>
      {t('snmp.label')}:{' '}
      {info.source === 'group' && info.groupId !== null ? (
        <>
          {t('snmp.inheritedFrom')}{' '}
          <Link
            to={`/groups/${info.groupId}`}
            className="font-medium text-accent-ink hover:underline"
            data-testid="snmp-source-group"
          >
            <bdi dir="auto">{group?.name ?? `#${info.groupId}`}</bdi>
          </Link>
        </>
      ) : (
        t(`snmp.source.${info.source}`)
      )}
      {version}
    </span>
  );
}
