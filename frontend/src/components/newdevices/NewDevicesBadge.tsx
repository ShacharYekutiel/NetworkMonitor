import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { endpoints3, qk3 } from '../../api/endpoints3';
import type { Device } from '../../api/types';
import { useFeatures } from '../../hooks/features';

/** [3] "N new devices" on switches (feature new_device_alerts); links to the New devices page. */
export function NewDevicesBadge({ device }: { device: Device }) {
  const { t } = useTranslation();
  const { features } = useFeatures();
  const on = features.new_device_alerts && device.effective_class === 'switch';
  const q = useQuery({
    queryKey: qk3.newDevices({ device_id: device.id, state: ['new'], page_size: 1 }),
    queryFn: () => endpoints3.newDevices({ device_id: device.id, state: ['new'], page_size: 1 }),
    enabled: on,
    staleTime: 60_000,
  });
  const n = q.data?.total ?? 0;
  if (!on || !n) return null;
  return (
    <Link
      to={`/new-devices?device_id=${device.id}`}
      className="rounded bg-rose-100 px-1.5 py-0.5 text-xs font-medium text-rose-800 hover:underline dark:bg-rose-900/40 dark:text-rose-200"
      data-testid="new-devices-badge"
    >
      {t('newDevices.badge', { count: n })}
    </Link>
  );
}
