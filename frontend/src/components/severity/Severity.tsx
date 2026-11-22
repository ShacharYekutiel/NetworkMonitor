// [severity] Alert severity UI pieces shared by Home, Events, sensors, rules and Settings. Every
// piece renders nothing (or is not rendered) while the feature `alert_severity` is OFF.
import { type ReactNode, useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import {
  qkSeverity,
  SEVERITIES,
  SEVERITY_MAPPING_KEYS,
  severityApi,
  type Severity,
  type SeverityCounts,
  type SeverityMapping,
  type SeverityMappingKey,
} from '../../api/severity';
import type { SensorImpact } from '../../api/types';
import { useAuth } from '../../hooks/auth';
import { useFeature } from '../../hooks/features';
import { automaticSeverity } from '../../lib/severity';
import { Popover, SeverityBars, SeverityTag, ToggleChip } from '../kit';
import { useToast } from '../toast';
import { Button, ConfirmDialog, ErrorBox, Field, Loading, Select, cx } from '../ui';

/** The feature switch (public settings; false while loading). */
export function useSeverityOn(): boolean {
  return useFeature('alert_severity');
}

/** GET /api/settings/severity-mapping (every role). */
export function useSeverityMapping(enabled = true) {
  return useQuery({
    queryKey: qkSeverity.mapping,
    queryFn: severityApi.mapping,
    enabled,
    staleTime: 30_000,
  });
}

/** The localized word of a level (also for option lists). */
export function useSeverityWord(): (s: Severity) => string {
  const { t } = useTranslation();
  return (s) => t(`severity.level.${s}`);
}

/**
 * Per-severity counts as toggle chips (Home): the number of alerts not acknowledged (the list
 * below), the total with acknowledged in the tooltip. A chip toggles that level in the filter.
 */
export function SeverityCountChips({
  counts,
  countsAll,
  selected,
  onToggle,
  onClear,
  extra,
}: {
  counts: SeverityCounts;
  countsAll?: SeverityCounts | null;
  selected: Severity[];
  onToggle: (s: Severity) => void;
  onClear: () => void;
  extra?: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <div
      role="group"
      aria-label={t('severity.filterLabel')}
      data-testid="severity-counts"
      className="mb-3 flex flex-wrap items-center gap-2"
    >
      {SEVERITIES.map((s) => {
        const n = counts[s] ?? 0;
        const all = countsAll?.[s] ?? n;
        const on = selected.includes(s);
        return (
          <button
            key={s}
            type="button"
            aria-pressed={on}
            data-testid="severity-count"
            data-severity={s}
            disabled={!on && n === 0 && all === 0}
            title={t('severity.countTitle', {
              level: t(`severity.level.${s}`),
              open: n,
              all,
            })}
            onClick={() => onToggle(s)}
            className={cx(
              'inline-flex h-8 items-center gap-2 rounded-full pe-3 ps-1 text-[13.5px] font-medium transition-colors disabled:cursor-default disabled:opacity-45',
              on
                ? 'bg-accent text-accent-on'
                : 'bg-surface text-ink shadow-[0_0_0_1px_var(--border)] hover:bg-surface-hover',
            )}
          >
            <SeverityTag severity={s} testId="severity-count-tag" />
            <span className="num font-semibold">{n}</span>
            {all !== n && (
              <span className={cx('num text-xs', on ? 'opacity-80' : 'text-ink-muted')}>
                /{all}
              </span>
            )}
          </button>
        );
      })}
      {selected.length > 0 && (
        <button
          type="button"
          onClick={onClear}
          className="inline-flex h-8 items-center rounded-full px-3 text-[13.5px] font-medium text-ink-muted hover:text-ink"
        >
          {t('severity.showAll')}
        </button>
      )}
      {extra && <span className="ms-auto">{extra}</span>}
    </div>
  );
}

/** Multi-select severity chips (checkbox semantics), e.g. the Events filter and rule forms. */
export function SeverityChips({
  value,
  onChange,
  label,
  testId,
  disabled,
}: {
  value: Severity[];
  onChange: (v: Severity[]) => void;
  label: string;
  testId?: string;
  disabled?: boolean;
}) {
  const word = useSeverityWord();
  return (
    <div role="group" aria-label={label} data-testid={testId} className="flex flex-wrap gap-2">
      {SEVERITIES.map((s) => (
        <ToggleChip
          key={s}
          disabled={disabled}
          checked={value.includes(s)}
          onChange={(c) => onChange(SEVERITIES.filter((x) => (x === s ? c : value.includes(x))))}
          label={
            <span className="inline-flex items-center gap-1.5" data-severity={s}>
              <SeverityBars severity={s} />
              {word(s)}
            </span>
          }
        />
      ))}
    </div>
  );
}

/** "" = Automatic, "keep" = leave unchanged (bulk only), else a level. */
export type SeverityChoice = Severity | '' | 'keep';

/** A severity select: [leave unchanged], Automatic (with what it resolves to), the five levels. */
export function SeveritySelect({
  value,
  onChange,
  automatic,
  allowKeep,
  id,
  testId,
  ariaLabel,
}: {
  value: SeverityChoice;
  onChange: (v: SeverityChoice) => void;
  /** the label of the Automatic option */
  automatic: string;
  allowKeep?: boolean;
  id?: string;
  testId?: string;
  ariaLabel?: string;
}) {
  const { t } = useTranslation();
  const word = useSeverityWord();
  return (
    <Select
      id={id}
      data-testid={testId}
      aria-label={ariaLabel}
      value={value}
      onChange={(e) => onChange(e.target.value as SeverityChoice)}
    >
      {allowKeep && <option value="keep">{t('severity.bulk.leave')}</option>}
      <option value="">{automatic}</option>
      {SEVERITIES.map((s) => (
        <option key={s} value={s}>
          {word(s)}
        </option>
      ))}
    </Select>
  );
}

/** The label of "Automatic" for a sensor: what the mapping resolves to for its impact. */
export function useAutomaticLabel(
  status: 'down' | 'warning',
  impact: SensorImpact,
  eventSensor: boolean,
): string {
  const { t } = useTranslation();
  const m = useSeverityMapping();
  const s = automaticSeverity(m.data?.mapping, status, impact);
  if (!s) return t('severity.automatic');
  const level = t(`severity.level.${s}`);
  return eventSensor
    ? t('severity.automaticEvent', { level })
    : t('severity.automaticIs', { level });
}

const ROWS: {
  key: SeverityMappingKey;
  status: 'down' | 'warning' | null;
  impact: string | null;
}[] = SEVERITY_MAPPING_KEYS.map((key) => {
  if (key === 'info') return { key, status: null, impact: null };
  const [status, impact] = key.split('_') as ['down' | 'warning', string];
  return { key, status, impact };
});

/**
 * The mapping table: one row per key (status × impact, then the informational row), each an
 * editable select (or the tag when read-only), the default next to it.
 */
export function SeverityMappingTable({
  value,
  defaults,
  onChange,
  readOnly,
}: {
  value: SeverityMapping;
  defaults: SeverityMapping;
  onChange?: (key: SeverityMappingKey, s: Severity) => void;
  readOnly?: boolean;
}) {
  const { t } = useTranslation();
  const word = useSeverityWord();
  const th = 'px-3 py-2 text-start text-xs font-semibold text-ink-muted';
  return (
    <div className="overflow-x-auto rounded-lg bg-surface-sunken">
      <table
        className="w-full min-w-[460px] border-collapse text-sm"
        data-testid="severity-mapping"
      >
        <thead>
          <tr className="border-b border-line">
            <th className={th}>{t('severity.mapping.status')}</th>
            <th className={th}>{t('severity.mapping.impact')}</th>
            <th className={th}>{t('severity.mapping.severity')}</th>
            <th className={th}>{t('severity.mapping.default')}</th>
          </tr>
        </thead>
        <tbody>
          {ROWS.map((r) => {
            const changed = value[r.key] !== defaults[r.key];
            const label =
              r.status === null
                ? t('severity.mapping.info')
                : `${t(`status.${r.status}`)} · ${t(`impact.option.${r.impact}`)}`;
            return (
              <tr
                key={r.key}
                data-testid="severity-mapping-row"
                data-key={r.key}
                className="border-b border-line last:border-b-0"
              >
                <td className="px-3 py-2 align-middle">
                  {r.status === null ? (
                    <span className="grid">
                      <b className="font-medium">{t('severity.mapping.info')}</b>
                      <span className="text-xs text-ink-muted">
                        {t('severity.mapping.infoHint')}
                      </span>
                    </span>
                  ) : (
                    <span className="font-medium">{t(`status.${r.status}`)}</span>
                  )}
                </td>
                <td className="px-3 py-2 align-middle text-ink-muted">
                  {r.impact ? t(`impact.option.${r.impact}`) : '—'}
                </td>
                <td className="px-3 py-1.5 align-middle">
                  {readOnly ? (
                    <SeverityTag severity={value[r.key]} />
                  ) : (
                    <span className="flex items-center gap-2">
                      <Select
                        aria-label={`${t('severity.mapping.severity')}: ${label}`}
                        value={value[r.key]}
                        onChange={(e) => onChange?.(r.key, e.target.value as Severity)}
                        className="!h-9 max-w-[180px] text-[13.5px]"
                      >
                        {SEVERITIES.map((s) => (
                          <option key={s} value={s}>
                            {word(s)}
                          </option>
                        ))}
                      </Select>
                      <SeverityBars severity={value[r.key]} className="text-ink-subtle" />
                    </span>
                  )}
                </td>
                <td className="px-3 py-2 align-middle">
                  <span className="inline-flex items-center gap-1.5">
                    <SeverityTag severity={defaults[r.key]} testId="severity-default" />
                    {changed && (
                      <span
                        className="text-xs font-medium text-accent-ink"
                        data-testid="severity-changed"
                      >
                        {t('severity.mapping.changed')}
                      </span>
                    )}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Settings card: the mapping, editable by admins (Save, Reset to defaults with confirmation),
 * read-only for viewers; explains that a change re-classifies the active alerts only.
 */
export function SeverityMappingEditor({ readOnly }: { readOnly?: boolean }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const q = useSeverityMapping();
  const [draft, setDraft] = useState<SeverityMapping | null>(null);
  const [confirm, setConfirm] = useState(false);
  useEffect(() => {
    if (q.data) setDraft(q.data.mapping);
  }, [q.data]);
  const onSaved = (key: string) => (r: Awaited<ReturnType<typeof severityApi.mapping>>) => {
    qc.setQueryData(qkSeverity.mapping, r);
    setDraft(r.mapping);
    // active alerts are re-classified: lists and counts follow (also via WS sensor.severity)
    void qc.invalidateQueries({ queryKey: ['dashboard'] });
    void qc.invalidateQueries({ queryKey: ['sensors'] });
    toast.push('success', t(key));
  };
  const save = useMutation({
    mutationFn: (body: Partial<SeverityMapping>) => severityApi.updateMapping(body),
    onSuccess: onSaved('severity.mapping.saved'),
  });
  const reset = useMutation({
    mutationFn: severityApi.resetMapping,
    onSuccess: (r) => {
      onSaved('severity.mapping.resetDone')(r);
      setConfirm(false);
    },
  });
  if (q.isLoading) return <Loading />;
  if (q.error || !q.data || !draft) return <ErrorBox error={q.error} />;
  const diff = Object.fromEntries(
    SEVERITY_MAPPING_KEYS.filter((k) => draft[k] !== q.data.mapping[k]).map((k) => [k, draft[k]]),
  ) as Partial<SeverityMapping>;
  const dirty = Object.keys(diff).length > 0;
  const atDefaults = SEVERITY_MAPPING_KEYS.every((k) => q.data.mapping[k] === q.data.defaults[k]);
  return (
    <div className="grid gap-3.5" data-testid="severity-mapping-card">
      <p className="m-0 text-sm text-ink-muted">{t('severity.mapping.intro')}</p>
      <SeverityMappingTable
        value={draft}
        defaults={q.data.defaults}
        readOnly={readOnly}
        onChange={(k, s) => setDraft((d) => (d ? { ...d, [k]: s } : d))}
      />
      <p className="m-0 text-xs text-ink-muted" data-testid="severity-reclassify-note">
        {t('severity.mapping.reclassify')}
      </p>
      {readOnly ? (
        <p className="m-0 text-xs text-ink-subtle">{t('severity.mapping.readOnly')}</p>
      ) : (
        <>
          <ErrorBox error={save.error} />
          <div className="flex flex-wrap justify-end gap-2">
            <Button
              variant="ghost"
              disabled={atDefaults && !dirty}
              onClick={() => setConfirm(true)}
            >
              {t('severity.mapping.reset')}
            </Button>
            <Button
              variant="primary"
              disabled={!dirty}
              busy={save.isPending}
              onClick={() => save.mutate(diff)}
            >
              {t('common.save')}
            </Button>
          </div>
          <ConfirmDialog
            open={confirm}
            title={t('severity.mapping.resetTitle')}
            message={t('severity.mapping.resetConfirm')}
            confirmLabel={t('severity.mapping.reset')}
            busy={reset.isPending}
            error={reset.error}
            onConfirm={() => reset.mutate()}
            onClose={() => setConfirm(false)}
          />
        </>
      )}
    </div>
  );
}

/**
 * "How severities are assigned": the mapping read-only in a popover, for every role (viewers
 * cannot open Settings); admins get a link to edit it.
 */
export function SeverityMappingInfo() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const q = useSeverityMapping();
  return (
    <Popover
      label={t('severity.mapping.view')}
      button={
        <>
          <SeverityBars severity="major" />
          {t('severity.mapping.view')}
        </>
      }
      panelClassName="w-[min(560px,92vw)]"
      testId="severity-mapping-popover"
    >
      <div className="grid gap-3" data-testid="severity-mapping-info">
        <b className="text-[15px] font-semibold">{t('severity.mapping.title')}</b>
        <p className="m-0 text-xs text-ink-muted">{t('severity.mapping.intro')}</p>
        {q.data ? (
          <SeverityMappingTable value={q.data.mapping} defaults={q.data.defaults} readOnly />
        ) : (
          <ErrorBox error={q.error} />
        )}
        {isAdmin && (
          <Link
            to="/settings?tab=general"
            className="text-sm font-medium text-accent-ink hover:underline"
          >
            {t('severity.mapping.edit')}
          </Link>
        )}
      </div>
    </Popover>
  );
}

/** Sensor form: "Severity when down" / "when in warning", first option Automatic (resolved). */
export function SeverityOverrideFields({
  impact,
  eventSensor,
  down,
  warning,
  onDown,
  onWarning,
}: {
  impact: SensorImpact;
  eventSensor: boolean;
  down: SeverityChoice;
  warning: SeverityChoice;
  onDown: (v: SeverityChoice) => void;
  onWarning: (v: SeverityChoice) => void;
}) {
  const { t } = useTranslation();
  const autoDown = useAutomaticLabel('down', impact, eventSensor);
  const autoWarn = useAutomaticLabel('warning', impact, eventSensor);
  return (
    <div className="grid gap-1.5" data-testid="severity-overrides">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label={t('severity.whenDown')}>
          <SeveritySelect
            testId="severity-down"
            value={down}
            onChange={onDown}
            automatic={autoDown}
          />
        </Field>
        <Field label={t('severity.whenWarning')}>
          <SeveritySelect
            testId="severity-warning"
            value={warning}
            onChange={onWarning}
            automatic={autoWarn}
          />
        </Field>
      </div>
      <p className="m-0 text-xs text-ink-subtle">{t('severity.overrideHint')}</p>
    </div>
  );
}
