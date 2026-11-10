import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../../api/endpoints';
import { useAuth } from '../../hooks/auth';
import { useFeatures } from '../../hooks/features';
import { Checkbox, ErrorBox } from '../ui';

/** [3] Port modal: "Ignore new devices on this port" (device newdev_excluded_ports). */
export function NewdevPortToggle({ deviceId, ifIndex }: { deviceId: number; ifIndex: number }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const { features } = useFeatures();
  const qc = useQueryClient();
  const on = features.new_device_alerts && isAdmin;
  const dev = useQuery({
    queryKey: qk.device(deviceId),
    queryFn: () => endpoints.device(deviceId),
    enabled: on,
  });
  const excluded = dev.data?.newdev_excluded_ports ?? [];
  const m = useMutation({
    mutationFn: (v: boolean) =>
      endpoints.updateDevice(deviceId, {
        newdev_excluded_ports: v
          ? [...new Set([...excluded, ifIndex])].sort((a, b) => a - b)
          : excluded.filter((x) => x !== ifIndex),
      }),
    onSuccess: (d) => qc.setQueryData(qk.device(deviceId), d),
  });
  if (!on || !dev.data) return null;
  return (
    <div
      className="mt-3 border-t border-slate-200 pt-3 dark:border-slate-800"
      data-testid="newdev-port"
    >
      <Checkbox
        label={t('newDevices.ignorePort')}
        checked={excluded.includes(ifIndex)}
        disabled={m.isPending}
        onChange={(v) => m.mutate(v)}
      />
      <ErrorBox error={m.error} className="mt-2" />
    </div>
  );
}
