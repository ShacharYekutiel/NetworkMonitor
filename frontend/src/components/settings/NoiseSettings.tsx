// [if-noise] Settings → General: "Sensor event noise" (debounce + flapping, both ON by default)
// and "Important ports" (which port properties count; the pre-upgrade meaning is a choice).
import { useEffect, useState } from 'react';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { IMPORTANT_CRITERIA, type ImportantCriterion, type Settings } from '../../api/types';
import { cardAnchor } from '../../lib/settingsIndex';
import {
  holdSeconds,
  legacyCriteria,
  noiseFormErrors,
  normalizeCriteria,
  type NoiseForm,
} from '../../lib/noise';
import { useSaveSettings } from '../features/common';
import { Button, Card, Checkbox, ErrorBox, Field, Input, Notice } from '../ui';

function formFrom(s: Settings): NoiseForm {
  return {
    debounce: s.sensor_event_debounce_enabled ?? true,
    polls: String(s.sensor_event_debounce_polls ?? 1),
    minS: String(s.sensor_event_debounce_min_s ?? 0),
    flap: s.sensor_event_flap_enabled ?? true,
    threshold: String(s.flap_threshold ?? 5),
    windowMin: String(s.flap_window_min ?? 10),
    quietMin: String(s.flap_quiet_min ?? 15),
  };
}

function Err({ on, text, hint }: { on: boolean; text: string; hint?: string }) {
  return on ? <span className="text-status-down">{text}</span> : <>{hint}</>;
}

export function SensorEventNoiseCard({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const [f, setF] = useState<NoiseForm>(() => formFrom(settings));
  useEffect(() => setF(formFrom(settings)), [settings]);
  const save = useSaveSettings();
  const e = noiseFormErrors(f);
  const invalid = Object.values(e).some(Boolean);
  const set = (patch: Partial<NoiseForm>) => setF((x) => ({ ...x, ...patch }));
  const hold60 = Math.round(holdSeconds(60, Number(f.polls) || 1, Number(f.minS) || 0));
  return (
    <Card title={t('noise.title')} anchor={cardAnchor('sensorNoise')}>
      <form
        className="space-y-4"
        data-testid="sensor-noise-settings"
        onSubmit={(ev) => {
          ev.preventDefault();
          if (invalid) return;
          save.mutate({
            sensor_event_debounce_enabled: f.debounce,
            sensor_event_debounce_polls: Number(f.polls),
            sensor_event_debounce_min_s: Number(f.minS),
            sensor_event_flap_enabled: f.flap,
            flap_threshold: Number(f.threshold),
            flap_window_min: Number(f.windowMin),
            flap_quiet_min: Number(f.quietMin),
          });
        }}
      >
        <p className="text-xs text-ink-muted">{t('noise.intro')}</p>
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div className="space-y-2 rounded-lg border border-line px-4 py-3.5">
            <Checkbox
              label={t('noise.debounce')}
              checked={f.debounce}
              onChange={(v) => set({ debounce: v })}
            />
            <p className="text-xs text-ink-muted">{t('noise.debounceHint')}</p>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field
                label={t('noise.polls')}
                hint={<Err on={e.polls} text={t('noise.pollsErr')} hint={t('noise.pollsHint')} />}
              >
                <Input
                  type="number"
                  min={1}
                  max={10}
                  value={f.polls}
                  disabled={!f.debounce}
                  onChange={(x) => set({ polls: x.target.value })}
                  dir="ltr"
                  className="max-w-[8rem]"
                  data-testid="debounce-polls"
                />
              </Field>
              <Field
                label={t('noise.minS')}
                hint={<Err on={e.minS} text={t('noise.minSErr')} hint={t('noise.minSHint')} />}
              >
                <Input
                  type="number"
                  min={0}
                  max={3600}
                  value={f.minS}
                  disabled={!f.debounce}
                  onChange={(x) => set({ minS: x.target.value })}
                  dir="ltr"
                  className="max-w-[8rem]"
                  data-testid="debounce-min-s"
                />
              </Field>
            </div>
            {f.debounce && !e.polls && !e.minS && (
              <p className="text-xs text-ink-muted" data-testid="debounce-example">
                {t('noise.example', { s: hold60 })}
              </p>
            )}
          </div>
          <div className="space-y-2 rounded-lg border border-line px-4 py-3.5">
            <Checkbox label={t('noise.flap')} checked={f.flap} onChange={(v) => set({ flap: v })} />
            <p className="text-xs text-ink-muted">
              {t('noise.flapHint', {
                n: f.threshold || '?',
                w: f.windowMin || '?',
                q: f.quietMin || '?',
              })}
            </p>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <Field
                label={t('flapping.threshold')}
                hint={<Err on={e.threshold} text={t('flapping.thresholdErr')} />}
              >
                <Input
                  type="number"
                  min={3}
                  max={50}
                  value={f.threshold}
                  onChange={(x) => set({ threshold: x.target.value })}
                  dir="ltr"
                  data-testid="noise-flap-threshold"
                />
              </Field>
              <Field
                label={t('flapping.window')}
                hint={<Err on={e.window} text={t('flapping.windowErr')} />}
              >
                <Input
                  type="number"
                  min={1}
                  max={120}
                  value={f.windowMin}
                  onChange={(x) => set({ windowMin: x.target.value })}
                  dir="ltr"
                />
              </Field>
              <Field
                label={t('flapping.quiet')}
                hint={<Err on={e.quiet} text={t('flapping.quietErr')} />}
              >
                <Input
                  type="number"
                  min={1}
                  max={1440}
                  value={f.quietMin}
                  onChange={(x) => set({ quietMin: x.target.value })}
                  dir="ltr"
                />
              </Field>
            </div>
            <p className="text-xs text-ink-subtle">{t('noise.sharedHint')}</p>
          </div>
        </div>
        <p className="text-xs text-ink-muted">
          {t('noise.alertsNote')}{' '}
          <Link to="/events?view=noisy" className="font-medium text-accent-ink hover:underline">
            {t('noise.noisyLink')}
          </Link>
        </p>
        <ErrorBox error={save.error} />
        <div className="flex justify-end">
          <Button type="submit" variant="primary" busy={save.isPending} disabled={invalid}>
            {t('common.save')}
          </Button>
        </div>
      </form>
    </Card>
  );
}

const RE2_UNSUPPORTED = /\\[1-9]|\(\?[=!<]/; // backreferences / lookaround (the server uses RE2)

export function patternError(p: string): boolean {
  const s = p.trim();
  if (!s) return false;
  if (s.length > 200 || RE2_UNSUPPORTED.test(s)) return true;
  try {
    new RegExp(s, 'i');
    return false;
  } catch {
    return true;
  }
}

export function ImportantPortsCard({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const [crit, setCrit] = useState<ImportantCriterion[]>(
    settings.important_port_criteria ?? ['uplink', 'lag', 'trunk', 'irf'],
  );
  const [pattern, setPattern] = useState(settings.important_port_description_pattern ?? '');
  useEffect(() => {
    setCrit(settings.important_port_criteria ?? ['uplink', 'lag', 'trunk', 'irf']);
    setPattern(settings.important_port_description_pattern ?? '');
  }, [settings]);
  const save = useSaveSettings();
  const bad = patternError(pattern);
  const toggle = (c: ImportantCriterion, on: boolean) =>
    setCrit((x) => normalizeCriteria(on ? [...x, c] : x.filter((y) => y !== c)));
  return (
    <Card title={t('importantPorts.title')} anchor={cardAnchor('importantPorts')}>
      <form
        className="space-y-4"
        data-testid="important-ports-settings"
        onSubmit={(ev) => {
          ev.preventDefault();
          if (!bad)
            save.mutate({
              important_port_criteria: crit,
              important_port_description_pattern: pattern.trim(),
            });
        }}
      >
        <p className="text-xs text-ink-muted">{t('importantPorts.intro')}</p>
        {legacyCriteria(settings.important_port_criteria) && (
          <div data-testid="important-legacy-notice">
            <Notice tone="warning" className="text-sm">
              {t('importantPorts.legacyNotice')}
            </Notice>
          </div>
        )}
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {IMPORTANT_CRITERIA.map((c) => (
            <div key={c} className="space-y-0.5" data-testid={`criterion-${c}`}>
              <Checkbox
                label={t(`importantPorts.criterion.${c}`)}
                checked={crit.includes(c)}
                onChange={(v) => toggle(c, v)}
              />
              <p className="ms-6 text-xs text-ink-muted">{t(`importantPorts.hint.${c}`)}</p>
            </div>
          ))}
        </div>
        <Field
          label={t('importantPorts.pattern')}
          hint={
            <Err
              on={bad}
              text={t('importantPorts.patternErr')}
              hint={t('importantPorts.patternHint')}
            />
          }
        >
          <Input
            value={pattern}
            onChange={(x) => setPattern(x.target.value)}
            dir="ltr"
            className="font-mono"
            maxLength={200}
            placeholder="uplink|core|server"
            data-testid="important-pattern"
          />
        </Field>
        <p className="text-xs text-ink-muted">{t('importantPorts.marksHint')}</p>
        <ErrorBox error={save.error} />
        <div className="flex justify-end">
          <Button type="submit" variant="primary" busy={save.isPending} disabled={bad}>
            {t('common.save')}
          </Button>
        </div>
      </form>
    </Card>
  );
}
