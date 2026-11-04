import type { TFunction } from 'i18next';
import { isApiError } from '../api/client';

/** Translates an error by its API code; falls back to the server message. */
export function errorMessage(t: TFunction, e: unknown): string {
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
    if (e.code === 'too_many_hosts') {
      const d = e.details?.[0];
      if (d?.count !== undefined)
        return t('errors.too_many_hosts_count', { hosts: d.count, max: d.max ?? 4096 });
      return base;
    }
    if (
      (e.code === 'snmp_error' ||
        e.code === 'smtp_error' ||
        e.code === 'webhook_error' ||
        e.code === 'telegram_error') &&
      e.message
    ) {
      return `${base}: ${e.message}`;
    }
    return base;
  }
  if (e instanceof Error) return e.message;
  return t('errors.internal_error');
}
