// [severity] Alert severity (api.md "[severity] Alert severity"). Feature `alert_severity`, OFF by
// default: every severity field below is null while it is OFF and the UI then shows nothing of it.
import { api } from './client';

/** Highest first. */
export type Severity = 'critical' | 'major' | 'minor' | 'warning' | 'info';
export const SEVERITIES: Severity[] = ['critical', 'major', 'minor', 'warning', 'info'];

/** Counts of the current problems per severity. */
export type SeverityCounts = Record<Severity, number> & { total: number };

export type SeverityMappingKey =
  | 'down_critical'
  | 'down_warning'
  | 'down_none'
  | 'warning_critical'
  | 'warning_warning'
  | 'warning_none'
  | 'info';
export const SEVERITY_MAPPING_KEYS: SeverityMappingKey[] = [
  'down_critical',
  'down_warning',
  'down_none',
  'warning_critical',
  'warning_warning',
  'warning_none',
  'info',
];
export type SeverityMapping = Record<SeverityMappingKey, Severity>;
export type SeverityMappingSettings = {
  mapping: SeverityMapping;
  defaults: SeverityMapping;
  levels: Severity[];
};

export type BulkSeverityInput = {
  sensor_ids: number[];
  severity_down?: Severity | null;
  severity_warning?: Severity | null;
};

export const qkSeverity = {
  mapping: ['settings', 'severity-mapping'] as const,
};

export const severityApi = {
  mapping: () => api.get<SeverityMappingSettings>('/settings/severity-mapping'),
  updateMapping: (body: Partial<SeverityMapping>) =>
    api.put<SeverityMappingSettings>('/settings/severity-mapping', body),
  resetMapping: () => api.post<SeverityMappingSettings>('/settings/severity-mapping/reset'),
  bulkSeverity: (body: BulkSeverityInput) =>
    api.post<{ updated: number }>('/sensors/bulk-severity', body),
};
