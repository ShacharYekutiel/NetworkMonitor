// [if-noise] Per-sensor / device / group override of the event-log noise protections (debounce,
// flapping collapse): the editor of the sensor / device / group forms, the card that shows what
// applies and where it comes from, the quick-action dialog and the sensor bulk dialog.
import { useEffect, useState, type ReactNode } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../../api/endpoints';
import {
  NOISE_MODES,
  type EffectiveEventNoise,
  type EventNoiseOverride,
  type NoiseMode,
  type NoiseResolved,
} from '../../api/types';
import {
  BULK_KEEP,
  bulkNoiseErrors,
  bulkNoiseInput,
  decidedByOverride,
  noiseFormErrors,
  noiseFormFrom,
  noiseFormToInput,
  type BulkNoiseForm,
  type KeepMode,
  type NoiseForm,
  type NoiseKey,
  type NoiseLevel,
  type ValueChoice,
} from '../../lib/noiseOverride';
import { useToast } from '../toast';
import { Button, Card, ErrorBox, Field, Input, Modal, Select, TAG, TAG_TONE, cx } from '../ui';

type TFn = (k: string, o?: Record<string, unknown>) => string;

/** "On" / "Off" / "3" / "120 s" */
function valueText(t: TFn, key: NoiseKey, value: boolean | number): string {
  if (key === 'debounce' || key === 'flap')
    return t(value ? 'noiseOv.value.on' : 'noiseOv.value.off');
  if (key === 'debounce_min_s') return t('noiseOv.seconds', { count: Number(value) });
  return String(value);
}

/** "set on this sensor" / "from device “sw-1”" / "global setting" */
export function sourceText(
  t: TFn,
  r: NoiseResolved<boolean | number>,
  self: NoiseLevel,
  selfId?: number,
): string {
  if (r.source === 'global') return t('noiseOv.source.global');
  if (r.source === self && (selfId === undefined || r.source_id === selfId))
    return t(`noiseOv.source.self.${self}`);
  // isolated (FSI … PDI): a Latin name inside Hebrew quotes keeps the quotes in place
  const name = `\u2068${r.source_name ?? `#${r.source_id}`}\u2069`;
  return t(`noiseOv.source.${r.source}`, { name });
}

const KEY_LABEL: Record<NoiseKey, string> = {
  debounce: 'noiseOv.key.debounce',
  flap: 'noiseOv.key.flap',
  debounce_polls: 'noiseOv.key.polls',
  debounce_min_s: 'noiseOv.key.minS',
};

function nowHint(
  t: TFn,
  eff: EffectiveEventNoise | null | undefined,
  key: NoiseKey,
  self: NoiseLevel,
  selfId?: number,
): ReactNode {
  if (!eff) return undefined;
  const r = eff[key] as NoiseResolved<boolean | number>;
  return t('noiseOv.now', {
    value: valueText(t, key, r.value),
    source: sourceText(t, r, self, selfId),
  });
}

function ModeSelect({
  value,
  onChange,
  testId,
  keep,
}: {
  value: KeepMode;
  onChange: (v: KeepMode) => void;
  testId?: string;
  keep?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <Select
      value={value}
      data-testid={testId}
      onChange={(e) => onChange(e.target.value as KeepMode)}
    >
      {keep && <option value="keep">{t('noiseOv.bulk.leave')}</option>}
      {NOISE_MODES.map((m) => (
        <option key={m} value={m}>
          {t(`noiseOv.mode.${m}`)}
        </option>
      ))}
    </Select>
  );
}

/**
 * The editor (sensor / device / group form). `effective` is what applies to the SAVED item: the
 * hints say what "Inherit" gives now and where it comes from.
 */
export function NoiseOverrideFields({
  value,
  onChange,
  level,
  effective,
  selfId,
  testId = 'noise-override',
}: {
  value: NoiseForm;
  onChange: (f: NoiseForm) => void;
  level: NoiseLevel;
  effective?: EffectiveEventNoise | null;
  selfId?: number;
  testId?: string;
}) {
  const { t } = useTranslation();
  const err = noiseFormErrors(value);
  const inherited = (key: 'debounce_polls' | 'debounce_min_s') => {
    const r = effective?.[key];
    // the placeholder shows the inherited value only while this level does not decide it
    if (!r || (r.source === level && (selfId === undefined || r.source_id === selfId)))
      return t('noiseOv.mode.inherit');
    return t('noiseOv.inheritValue', { value: valueText(t, key, r.value) });
  };
  return (
    <fieldset className="rounded-lg border border-line px-4 py-3.5" data-testid={testId}>
      <legend className="px-1 text-xs font-semibold text-ink-muted">{t('noiseOv.title')}</legend>
      <p className="mb-3 mt-0 text-xs text-ink-muted">{t(`noiseOv.intro.${level}`)}</p>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label={t('noise.debounce')} hint={nowHint(t, effective, 'debounce', level, selfId)}>
          <ModeSelect
            testId={`${testId}-debounce`}
            value={value.debounce}
            onChange={(v) => onChange({ ...value, debounce: v as NoiseMode })}
          />
        </Field>
        <Field label={t('noise.flap')} hint={nowHint(t, effective, 'flap', level, selfId)}>
          <ModeSelect
            testId={`${testId}-flap`}
            value={value.flap}
            onChange={(v) => onChange({ ...value, flap: v as NoiseMode })}
          />
        </Field>
        {value.debounce !== 'off' && (
          <>
            <Field
              label={t('noise.polls')}
              hint={
                err.polls ? (
                  <span className="text-status-down">{t('noise.pollsErr')}</span>
                ) : (
                  t('noiseOv.valueHint')
                )
              }
            >
              <Input
                type="number"
                min={1}
                max={10}
                dir="ltr"
                data-testid={`${testId}-polls`}
                value={value.polls}
                placeholder={inherited('debounce_polls')}
                onChange={(e) => onChange({ ...value, polls: e.target.value })}
              />
            </Field>
            <Field
              label={t('noise.minS')}
              hint={
                err.minS ? (
                  <span className="text-status-down">{t('noise.minSErr')}</span>
                ) : (
                  t('noiseOv.valueHint')
                )
              }
            >
              <Input
                type="number"
                min={0}
                max={3600}
                dir="ltr"
                data-testid={`${testId}-min-s`}
                value={value.minS}
                placeholder={inherited('debounce_min_s')}
                onChange={(e) => onChange({ ...value, minS: e.target.value })}
              />
            </Field>
          </>
        )}
      </div>
    </fieldset>
  );
}

/**
 * Sensor / device / group page: what applies when some level (not the global settings) decides,
 * with the source. Nothing while every value is the global setting.
 */
export function NoiseOverrideCard({
  effective,
  level,
  selfId,
  onEdit,
}: {
  effective?: EffectiveEventNoise | null;
  level: NoiseLevel;
  selfId?: number;
  onEdit?: () => void;
}) {
  const { t } = useTranslation();
  const decided = decidedByOverride(effective);
  if (!effective || !decided.length) return null;
  return (
    <Card
      title={t('noiseOv.cardTitle')}
      actions={
        onEdit && (
          <Button size="sm" onClick={onEdit} data-testid="noise-override-edit">
            {t('noiseOv.edit')}
          </Button>
        )
      }
    >
      <div data-testid="noise-override-card" className="space-y-2">
        <dl className="m-0 grid grid-cols-[max-content_minmax(0,1fr)] gap-x-[18px] gap-y-2 text-[14px]">
          {(['debounce', 'flap', 'debounce_polls', 'debounce_min_s'] as NoiseKey[])
            .filter((k) => k === 'debounce' || k === 'flap' || decided.some((d) => d.key === k))
            .map((k) => {
              const r = effective[k] as NoiseResolved<boolean | number>;
              const own = r.source !== 'global';
              return (
                <div key={k} className="contents" data-testid={`noise-override-${k}`}>
                  <dt className="text-ink-muted">{t(KEY_LABEL[k])}</dt>
                  <dd className="m-0 min-w-0 text-end">
                    <span
                      className={cx(
                        TAG,
                        own
                          ? r.value === false
                            ? TAG_TONE.warning
                            : TAG_TONE.accent
                          : TAG_TONE.neutral,
                      )}
                    >
                      {valueText(t, k, r.value)}
                    </span>{' '}
                    <span className="text-xs text-ink-muted">
                      <bdi>{sourceText(t, r, level, selfId)}</bdi>
                    </span>
                  </dd>
                </div>
              );
            })}
        </dl>
        <p className="m-0 text-xs text-ink-subtle">{t('noiseOv.cardHint')}</p>
      </div>
    </Card>
  );
}

/** A small "Custom" tag when the item has its own override. */
export function NoiseOverrideTag({ title }: { title?: string }) {
  const { t } = useTranslation();
  return (
    <span className={cx(TAG, TAG_TONE.accent)} title={title} data-testid="noise-override-tag">
      {t('noiseOv.tag')}
    </span>
  );
}

/** Quick edit of one sensor's override (noisiest sensors table, sensor page card). */
export function SensorNoiseModal({
  open,
  onClose,
  sensor,
}: {
  open: boolean;
  onClose: () => void;
  sensor: {
    id: number;
    name: string;
    device_id: number;
    event_noise?: EventNoiseOverride;
    effective_event_noise?: EffectiveEventNoise | null;
  };
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState<NoiseForm>(noiseFormFrom(sensor.event_noise));
  useEffect(() => {
    if (open) setForm(noiseFormFrom(sensor.event_noise));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  const err = noiseFormErrors(form);
  const body = noiseFormToInput(form, sensor.event_noise);
  const m = useMutation({
    mutationFn: () => endpoints.updateSensor(sensor.id, { event_noise: body }),
    onSuccess: (s) => {
      qc.setQueryData(qk.sensor(s.id), s);
      void qc.invalidateQueries({ queryKey: ['sensors'] });
      void qc.invalidateQueries({ queryKey: ['noisy-sensors'] });
      void qc.invalidateQueries({ queryKey: qk.device(sensor.device_id) });
      void qc.invalidateQueries({ queryKey: ['events'] });
      toast.push('success', t('common.saved'));
      onClose();
    },
  });
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t('noiseOv.modalTitle', { name: sensor.name })}
      testId="sensor-noise-modal"
      wide
      footer={
        <>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button
            variant="primary"
            busy={m.isPending}
            disabled={err.polls || err.minS || !body}
            onClick={() => m.mutate()}
          >
            {t('common.save')}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <NoiseOverrideFields
          value={form}
          onChange={setForm}
          level="sensor"
          selfId={sensor.id}
          effective={sensor.effective_event_noise}
        />
        <ErrorBox error={m.error} />
      </div>
    </Modal>
  );
}

function ValueChoiceField({
  label,
  value,
  onChange,
  min,
  max,
  error,
  errorText,
  testId,
}: {
  label: string;
  value: ValueChoice;
  onChange: (v: ValueChoice) => void;
  min: number;
  max: number;
  error: boolean;
  errorText: string;
  testId: string;
}) {
  const { t } = useTranslation();
  return (
    <Field
      label={label}
      hint={error ? <span className="text-status-down">{errorText}</span> : undefined}
    >
      <div className="flex gap-2">
        <Select
          value={value.mode}
          data-testid={`${testId}-mode`}
          onChange={(e) => onChange({ ...value, mode: e.target.value as ValueChoice['mode'] })}
        >
          <option value="keep">{t('noiseOv.bulk.leave')}</option>
          <option value="inherit">{t('noiseOv.mode.inherit')}</option>
          <option value="set">{t('noiseOv.bulk.set')}</option>
        </Select>
        {value.mode === 'set' && (
          <Input
            type="number"
            min={min}
            max={max}
            dir="ltr"
            className="w-28"
            aria-label={label}
            data-testid={testId}
            value={value.value}
            onChange={(e) => onChange({ ...value, value: e.target.value })}
          />
        )}
      </div>
    </Field>
  );
}

/** Sensor bulk bar: "Noise protection…" (POST /api/sensors/bulk-event-noise). */
export function BulkNoiseModal({
  open,
  onClose,
  sensorIds,
  deviceId,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  sensorIds: number[];
  deviceId?: number;
  onDone?: () => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState<BulkNoiseForm>(BULK_KEEP);
  useEffect(() => {
    if (open) setForm(BULK_KEEP);
  }, [open]);
  const err = bulkNoiseErrors(form);
  const body = bulkNoiseInput(form);
  const count = sensorIds.length;
  const m = useMutation({
    mutationFn: () => endpoints.bulkEventNoise(sensorIds, body ?? {}),
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ['sensors'] });
      void qc.invalidateQueries({ queryKey: ['sensor'] });
      void qc.invalidateQueries({ queryKey: ['noisy-sensors'] });
      if (deviceId !== undefined) void qc.invalidateQueries({ queryKey: qk.device(deviceId) });
      toast.push('success', t('noiseOv.bulk.done', { count: r.updated }));
      onDone?.();
      onClose();
    },
  });
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t('noiseOv.bulk.title', { count })}
      testId="bulk-noise"
      wide
      footer={
        <>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button
            variant="primary"
            busy={m.isPending}
            disabled={!body || !count}
            onClick={() => m.mutate()}
          >
            {t('impact.apply')}
          </Button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t('noise.debounce')}>
          <ModeSelect
            keep
            testId="bulk-noise-debounce"
            value={form.debounce}
            onChange={(v) => setForm({ ...form, debounce: v })}
          />
        </Field>
        <Field label={t('noise.flap')}>
          <ModeSelect
            keep
            testId="bulk-noise-flap"
            value={form.flap}
            onChange={(v) => setForm({ ...form, flap: v })}
          />
        </Field>
        <ValueChoiceField
          label={t('noise.polls')}
          value={form.polls}
          onChange={(v) => setForm({ ...form, polls: v })}
          min={1}
          max={10}
          error={err.polls}
          errorText={t('noise.pollsErr')}
          testId="bulk-noise-polls"
        />
        <ValueChoiceField
          label={t('noise.minS')}
          value={form.minS}
          onChange={(v) => setForm({ ...form, minS: v })}
          min={0}
          max={3600}
          error={err.minS}
          errorText={t('noise.minSErr')}
          testId="bulk-noise-min-s"
        />
        <p className="m-0 text-xs text-ink-subtle sm:col-span-2">
          {body ? t('noiseOv.bulk.hint') : t('noiseOv.bulk.pickOne')}
        </p>
        <ErrorBox error={m.error} />
      </div>
    </Modal>
  );
}
