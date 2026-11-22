// [template-switch] Apply template → "Sensors this template replaces": the device's sensors the
// new template supersedes (e.g. generic HOST-RESOURCES CPU / memory under a vendor template).
// Nothing is deleted unless ticked; failing ones start ticked.
import { useEffect, useLayoutEffect, useMemo, useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { DeviceTemplate, ReplaceCandidate } from '../api/types';
import { initialReplacePick, templateName } from '../lib/templates';
import { CHECKBOX } from './kit';
import { StatusBadge } from './Status';
import { cx, TAG, TAG_TONE } from './ui';

function ReplaceRow({
  c,
  checked,
  onToggle,
  templates,
}: {
  c: ReplaceCandidate;
  checked: boolean;
  onToggle: (on: boolean) => void;
  templates: DeviceTemplate[] | undefined;
}) {
  const { t, i18n } = useTranslation();
  const reason =
    c.reason === 'replaced'
      ? t('applyReplaces.reason.replaced')
      : c.from_template
        ? t('applyReplaces.reason.other_template', {
            template: templateName(templates, c.from_template, i18n.language),
          })
        : t('applyReplaces.reason.other_template_unknown');
  return (
    <li>
      <label
        className={cx(
          'flex cursor-pointer items-start gap-3 px-3 py-2.5',
          checked ? 'bg-status-down-soft' : 'hover:bg-surface-hover',
        )}
        data-testid="apply-replace-row"
      >
        <input
          type="checkbox"
          className={cx(CHECKBOX, 'mt-0.5')}
          checked={checked}
          onChange={(e) => onToggle(e.target.checked)}
          aria-label={t('applyReplaces.rowLabel', { name: c.name })}
        />
        <span className="grid min-w-0 flex-1 gap-0.5">
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <bdi dir="auto" className="text-[14.5px] font-medium text-ink">
              {c.name}
            </bdi>
            <span className="text-xs text-ink-muted">{t(`sensorType.${c.type}`)}</span>
            <StatusBadge status={c.status} />
            {c.failing && (
              <span className={cx(TAG, TAG_TONE.down)} data-testid="apply-replace-failing">
                {t('applyReplaces.failing')}
              </span>
            )}
          </span>
          <span className="text-xs text-ink-muted">{reason}</span>
          {c.status_message && (
            // the reading side of the page, the (English) message itself left to right
            <span className="block truncate text-xs text-ink-subtle">
              <bdi dir="ltr">{c.status_message}</bdi>
            </span>
          )}
        </span>
      </label>
    </li>
  );
}

export function ApplyReplacesSection({
  deviceId,
  templateId,
  picked,
  onPicked,
  templates,
}: {
  deviceId: number;
  templateId: string;
  /** to name the template that created a sensor */
  templates: DeviceTemplate[] | undefined;
  /** null until the list has loaded once (then the failing sensors are preselected) */
  picked: Set<number> | null;
  onPicked: (s: Set<number>) => void;
}) {
  const { t } = useTranslation();
  const q = useQuery({
    queryKey: qk.applyReplaces(deviceId, templateId),
    queryFn: () => endpoints.applyTemplateReplaces(deviceId, templateId),
    enabled: !!templateId,
    staleTime: 0,
  });
  const items = useMemo(() => q.data?.items ?? [], [q.data]);
  useEffect(() => {
    if (q.data && picked === null) onPicked(new Set(initialReplacePick(q.data.items)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q.data, picked]);
  const sel = picked ?? new Set<number>();
  const n = items.filter((c) => sel.has(c.sensor_id)).length;
  const all = items.length > 0 && n === items.length;
  const allRef = useRef<HTMLInputElement>(null);
  useLayoutEffect(() => {
    if (allRef.current) allRef.current.indeterminate = n > 0 && !all;
  }, [n, all]);

  // no candidates (or the list could not be loaded): the dialog looks as before
  if (!items.length) return null;
  const toggle = (id: number, on: boolean) => {
    const next = new Set(sel);
    if (on) next.add(id);
    else next.delete(id);
    onPicked(next);
  };

  return (
    <section
      className="space-y-3 rounded-lg border border-line px-4 py-3.5"
      aria-labelledby="apply-replaces-title"
      data-testid="apply-replaces"
    >
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="grid min-w-0 flex-1 basis-60 gap-0.5">
          <h3 id="apply-replaces-title" className="text-[14.5px] font-semibold text-ink">
            {t('applyReplaces.title')}
          </h3>
          <p className="text-xs text-ink-muted">{t('applyReplaces.hint')}</p>
        </div>
        <label className="inline-flex shrink-0 cursor-pointer items-center gap-2 text-sm">
          <input
            ref={allRef}
            type="checkbox"
            className={CHECKBOX}
            checked={all}
            onChange={(e) =>
              onPicked(e.target.checked ? new Set(items.map((c) => c.sensor_id)) : new Set())
            }
          />
          {t('applyReplaces.selectAll')}
        </label>
      </div>
      <ul
        className="max-h-72 divide-y divide-line overflow-y-auto rounded-lg border border-line bg-surface"
        data-testid="apply-replaces-list"
      >
        {items.map((c) => (
          <ReplaceRow
            key={c.sensor_id}
            c={c}
            checked={sel.has(c.sensor_id)}
            onToggle={(on) => toggle(c.sensor_id, on)}
            templates={templates}
          />
        ))}
      </ul>
      <p
        className={cx('text-xs font-medium', n ? 'text-status-down' : 'text-ink-muted')}
        data-testid="apply-replaces-count"
        aria-live="polite"
      >
        {n ? t('applyReplaces.deleteCount', { count: n }) : t('applyReplaces.keepAll')}
      </p>
    </section>
  );
}
