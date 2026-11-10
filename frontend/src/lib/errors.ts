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
    // [3b] caps: which cap, used/limit
    if (e.code === 'assistant_cap_reached') {
      const d = e.details?.[0] as Record<string, unknown> | undefined;
      if (d && typeof d.cap === 'string')
        return `${base} ${t('assistant.capDetail', {
          cap: t(`assistant.cap.${d.cap}`, { defaultValue: d.cap }),
          used: String(d.used ?? ''),
          limit: String(d.limit ?? ''),
        })}`;
      return base;
    }
    if (e.code === 'assistant_busy' && e.status === 429) return t('errors.assistant_busy_global');
    if (e.code === 'feature_dependency') {
      const d = e.details?.[0] as Record<string, unknown> | undefined;
      if (d && typeof d.requires === 'string')
        return t('errors.feature_dependency_named', {
          feature: t(`features.name.${String(d.feature)}`, { defaultValue: String(d.feature) }),
          requires: t(`features.name.${d.requires}`, { defaultValue: d.requires }),
        });
      return base;
    }
    if (e.code === 'feature_idle') {
      const d = e.details?.[0] as Record<string, unknown> | undefined;
      const needs = Array.isArray(d?.needs) ? (d!.needs as string[]) : [];
      if (needs.length)
        return t('errors.feature_idle_named', {
          feature: t(`features.name.${String(d!.feature)}`, { defaultValue: String(d!.feature) }),
          needs: needs.map((n) => t(`features.name.${n}`, { defaultValue: n })).join(', '),
        });
      return base;
    }
    if (e.code === 'invalid_certificate') {
      const r = (e.details?.[0] as Record<string, unknown> | undefined)?.reason;
      return typeof r === 'string' ? `${base} (${r})` : base;
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
        e.code === 'forward_error' ||
        e.code === 'ssh_error' || // [3]
        e.code === 'oidc_error' ||
        e.code === 'sso_ntlm' ||
        // [3b] AWS text / host carries the useful part
        e.code === 'bedrock_access_denied' ||
        e.code === 'bedrock_validation' ||
        e.code === 'aws_auth_failed' ||
        e.code === 'rolesanywhere_failed' ||
        e.code === 'aws_network' ||
        e.code === 'kb_unavailable') &&
      e.message
    ) {
      return `${base}: ${e.message}`;
    }
    return base;
  }
  if (e instanceof Error) return e.message;
  return t('errors.internal_error');
}
