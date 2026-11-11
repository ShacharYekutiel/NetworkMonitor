import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type {
  ApplyTemplateResult,
  Confidence,
  Device,
  DeviceTemplate,
  SkippedSensor,
} from '../api/types';
import { applySummary, describeEntry, groupSkipped, l10n, skipReasonKey } from '../lib/templates';
import { CheckIcon } from './icons';
import { Button, cx, ErrorBox, Field, Loading, Modal, Notice, Select } from './ui';

export function useDeviceTemplates() {
  return useQuery({
    queryKey: qk.deviceTemplates,
    queryFn: endpoints.deviceTemplates,
    staleTime: Infinity,
    select: (d) => [...d.items].sort((a, b) => b.priority - a.priority),
  });
}

/** [2b-2] The full response (items + library status) of GET /api/device-templates. */
export function useTemplateLibrary() {
  return useQuery({
    queryKey: qk.deviceTemplates,
    queryFn: endpoints.deviceTemplates,
    staleTime: Infinity,
  });
}

const CONFIDENCE_TONE: Record<Confidence, string> = {
  high: 'bg-status-up-soft text-status-up',
  medium: 'bg-status-warning-soft text-status-warning',
  low: 'bg-status-warning-soft text-status-warning',
};

/** [2b-2] Confidence chip of a template entry. */
export function ConfidenceChip({ confidence }: { confidence: Confidence }) {
  const { t } = useTranslation();
  return (
    <span
      title={t('templates.confidenceTitle')}
      className={cx(
        'inline-flex shrink-0 items-center whitespace-nowrap rounded-sm px-1.5 py-px text-[11px] font-medium',
        CONFIDENCE_TONE[confidence] ?? CONFIDENCE_TONE.low,
      )}
    >
      {t(`confidence.${confidence}`)}
    </span>
  );
}

/** [2b-2] Verified (green) / Unverified (gray, tooltip = verified_note) badge. */
export function VerifiedBadge({ tpl }: { tpl: DeviceTemplate }) {
  const { t } = useTranslation();
  const ok = tpl.verified === true;
  return (
    <span
      title={tpl.verified_note || (ok ? undefined : t('templates.unverifiedHint'))}
      className={cx(
        'inline-flex items-center gap-1 whitespace-nowrap rounded-sm px-1.5 py-0.5 text-[11px] font-medium',
        ok
          ? 'bg-status-up-soft text-status-up'
          : 'border border-line bg-surface-sunken text-ink-muted',
      )}
    >
      {ok && <CheckIcon size={11} />}
      {ok ? t('templates.verified') : t('templates.unverified')}
    </span>
  );
}

/** Native select of templates with localized names (explicit colours via the base Select). */
export function TemplateSelect({
  templates,
  value,
  onChange,
  className,
  suggested,
  ariaLabel,
}: {
  templates: DeviceTemplate[];
  value: string;
  onChange: (id: string) => void;
  className?: string;
  suggested?: string;
  ariaLabel?: string;
}) {
  const { t, i18n } = useTranslation();
  const known = templates.some((x) => x.id === value);
  return (
    <Select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={className}
      aria-label={ariaLabel ?? t('templates.template')}
    >
      {!known && value && <option value={value}>{value}</option>}
      {templates.map((tpl) => (
        <option key={tpl.id} value={tpl.id}>
          {l10n(tpl.name, i18n.language)}
          {tpl.id === suggested ? ` (${t('templates.suggested')})` : ''}
        </option>
      ))}
    </Select>
  );
}

/** Skipped template entries, grouped by translated reason. */
export function SkippedList({ skipped, compact }: { skipped: SkippedSensor[]; compact?: boolean }) {
  const { t } = useTranslation();
  if (!skipped.length) return null;
  return (
    <div className={cx('space-y-2', compact && 'text-xs')}>
      {groupSkipped(skipped).map((g) => (
        <div key={g.reason}>
          <div
            className={cx(
              'font-medium',
              g.reason === 'exists' || g.reason === 'not_applicable'
                ? 'text-ink-muted'
                : 'text-status-warning',
            )}
          >
            {t(skipReasonKey(g.reason))} ({g.items.length})
          </div>
          <ul className="ms-4 list-disc space-y-0.5 text-ink-muted">
            {g.items.map((s, i) => (
              <li key={`${s.name}-${i}`}>
                <bdi dir="auto">{s.name}</bdi>{' '}
                <span className="text-ink-muted">({t(`sensorType.${s.type}`)})</span>
                {s.message && (
                  <span className="text-ink-muted">
                    {' '}
                    —{' '}
                    {s.message === 'Renamed' ? (
                      t('templates.renamed')
                    ) : (
                      <bdi dir="ltr">{s.message}</bdi>
                    )}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

export function ApplyResultView({ result }: { result: ApplyTemplateResult }) {
  const { t } = useTranslation();
  const sum = applySummary(result);
  return (
    <div className="space-y-3 text-sm" data-testid="apply-result">
      <Notice tone={sum.created ? 'success' : 'info'}>
        {t('templates.applySummary', {
          created: sum.created,
          skipped: sum.skipped,
          existing: sum.existing,
        })}
      </Notice>
      {result.created.length > 0 && (
        <div>
          <div className="mb-1 font-medium">{t('templates.createdTitle')}</div>
          <ul className="space-y-0.5">
            {result.created.map((c) => (
              <li key={c.sensor_id} className="flex items-center gap-1.5">
                <CheckIcon size={14} className="shrink-0 text-status-up" />
                <Link to={`/sensors/${c.sensor_id}`} className="text-accent-ink hover:underline">
                  <bdi dir="auto">{c.name}</bdi>
                </Link>
                <span className="text-ink-muted">({t(`sensorType.${c.type}`)})</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {result.skipped.length > 0 && (
        <div>
          <div className="mb-1 font-medium">{t('templates.skippedTitle')}</div>
          <SkippedList skipped={result.skipped} />
        </div>
      )}
    </div>
  );
}

export function ApplyTemplateModal({
  open,
  onClose,
  device,
  initialTemplate,
}: {
  open: boolean;
  onClose: () => void;
  device: Device;
  /** [2b-1] preselected template (e.g. the identity's suggestion) */
  initialTemplate?: string;
}) {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const templates = useDeviceTemplates();
  const [id, setId] = useState('');
  const [result, setResult] = useState<ApplyTemplateResult | null>(null);
  const tpl = useMemo(() => templates.data?.find((x) => x.id === id), [templates.data, id]);

  useEffect(() => {
    if (!open) return;
    setResult(null);
    if (initialTemplate) setId(initialTemplate);
    apply.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  useEffect(() => {
    if (!id && templates.data?.length) setId(templates.data[0].id);
  }, [templates.data, id]);

  const apply = useMutation({
    mutationFn: () => endpoints.applyTemplate(device.id, id),
    onSuccess: (r) => {
      setResult(r);
      void qc.invalidateQueries({ queryKey: ['sensors'] });
      void qc.invalidateQueries({ queryKey: qk.device(device.id) });
      void qc.invalidateQueries({ queryKey: qk.tree });
    },
  });

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={result ? t('templates.resultTitle', { name: device.name }) : t('templates.apply')}
      wide
      footer={
        result ? (
          <Button variant="primary" onClick={onClose}>
            {t('common.close')}
          </Button>
        ) : (
          <>
            <Button onClick={onClose}>{t('common.cancel')}</Button>
            <Button
              variant="primary"
              busy={apply.isPending}
              disabled={!tpl}
              onClick={() => apply.mutate()}
            >
              {t('templates.applyButton')}
            </Button>
          </>
        )
      }
    >
      {result ? (
        <ApplyResultView result={result} />
      ) : (
        <div className="space-y-3">
          {templates.isLoading && <Loading />}
          <ErrorBox error={templates.error} />
          {templates.data && (
            <Field label={t('templates.template')}>
              <TemplateSelect templates={templates.data} value={id} onChange={setId} />
            </Field>
          )}
          {tpl && (
            <div className="rounded-md bg-surface-sunken p-3 text-sm ring-1 ring-inset ring-line">
              <p className="text-ink">{l10n(tpl.description, i18n.language)}</p>
              <div className="mt-2 text-xs font-medium text-ink-muted">
                {t('templates.creates')}
              </div>
              <ul className="ms-4 mt-1 list-disc text-xs text-ink-muted">
                {tpl.entries.map((e, i) => (
                  <li key={i}>
                    <bdi dir="auto">{describeEntry(e, i18n.language, t)}</bdi>
                  </li>
                ))}
              </ul>
              {tpl.requires_snmp && (
                <p className="mt-2 text-xs text-ink-muted">
                  {t('sensors.requiresSnmp', {
                    source: t(`snmp.source.${device.effective_snmp_source}`),
                  })}
                </p>
              )}
            </div>
          )}
          <p className="text-xs text-ink-muted">{t('templates.applyHint')}</p>
          {apply.isPending && <p className="text-xs text-ink-muted">{t('templates.applying')}</p>}
          <ErrorBox error={apply.error} />
        </div>
      )}
    </Modal>
  );
}
