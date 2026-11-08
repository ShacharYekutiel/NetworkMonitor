import type { TFunction } from 'i18next';
import { isApiError } from '../api/client';

export type ErrorContext = {
  /** Resolves device ids (e.g. a dependency_cycle path) to names. */
  deviceName?: (id: number) => string | undefined;
};

/** Translates an error by its API code; falls back to the server message. */
export function errorMessage(t: TFunction, e: unknown, ctx: ErrorContext = {}): string {
  if (isApiError(e)) {
    const key = `errors.${e.code}`;
    const translated = t(key, { defaultValue: '' });
    const base = translated || e.message || t('errors.internal_error');
    if (e.code === 'validation_error' && e.details?.length) {
      const d = e.details
        .map((x) =>
          [x.loc?.filter((p) => p !== 'body').join('.'), x.msg].filter(Boolean).join(': '),
        )
        .join('; ');
      return `${base} (${d})`;
    }
    if (e.code === 'feature_disabled') {
      const f = e.details?.find((d) => typeof d.feature === 'string')?.feature;
      if (f)
        return t('errors.feature_disabled_named', {
          feature: t(`features.name.${f}`, { defaultValue: f }),
        });
      return base;
    }
    if (e.code === 'too_many_hosts') {
      const d = e.details?.[0];
      if (d?.count !== undefined)
        return t('errors.too_many_hosts_count', { hosts: d.count, max: d.max ?? 4096 });
      return base;
    }
    if (e.code === 'dependency_cycle') {
      const ids = e.details?.find((d) => Array.isArray(d.device_ids))?.device_ids;
      if (ids?.length) {
        const path = ids.map((id) => ctx.deviceName?.(id) ?? `#${id}`).join(' → ');
        return t('errors.dependency_cycle_path', { path });
      }
      return base;
    }
    if (
      (e.code === 'snmp_error' ||
        e.code === 'smtp_error' ||
        e.code === 'webhook_error' ||
        e.code === 'telegram_error' ||
        e.code === 'teams_error' || // [2b-3]
        e.code === 'forward_error') &&
      e.message
    ) {
      return `${base}: ${e.message}`;
    }
    return base;
  }
  if (e instanceof Error) return e.message;
  return t('errors.internal_error');
}
