import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { Channel, ChannelThresholdInput, Sensor } from '../api/types';
import { useToast } from './toast';
import { Button, ErrorBox, Input } from './ui';

const KEYS = ['warning_min', 'warning_max', 'down_min', 'down_max'] as const;
type Key = (typeof KEYS)[number];
type Row = Record<Key, string>;
const FIELD_ORDER: Key[] = ['warning_max', 'down_max', 'warning_min', 'down_min'];

function toRow(c: Channel): Row {
  return {
    warning_min: c.warning_min === null ? '' : String(c.warning_min),
    warning_max: c.warning_max === null ? '' : String(c.warning_max),
    down_min: c.down_min === null ? '' : String(c.down_min),
    down_max: c.down_max === null ? '' : String(c.down_max),
  };
}

function parse(s: string): number | null | 'invalid' {
  const v = s.trim();
  if (!v) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : 'invalid';
}

/** Builds the PATCH `channels` payload with only the changed keys (null clears a limit). */
export function diffThresholds(
  channels: Channel[],
  rows: Record<string, Row>,
): ChannelThresholdInput[] | 'invalid' {
  const out: ChannelThresholdInput[] = [];
  for (const c of channels) {
    const r = rows[c.key];
    if (!r) continue;
    const entry: ChannelThresholdInput = { key: c.key };
    let changed = false;
    for (const k of KEYS) {
      const v = parse(r[k]);
      if (v === 'invalid') return 'invalid';
      if (v !== c[k]) {
        entry[k] = v;
        changed = true;
      }
    }
    if (changed) out.push(entry);
  }
  return out;
}

export function ThresholdEditor({ sensor, readOnly }: { sensor: Sensor; readOnly: boolean }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const [rows, setRows] = useState<Record<string, Row>>({});
  const [invalid, setInvalid] = useState(false);

  const serverKey = JSON.stringify(sensor.channels.map((c) => [c.key, ...KEYS.map((k) => c[k])]));
  useEffect(() => {
    setRows(Object.fromEntries(sensor.channels.map((c) => [c.key, toRow(c)])));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverKey]);

  const m = useMutation({
    mutationFn: (channels: ChannelThresholdInput[]) =>
      endpoints.updateSensor(sensor.id, { channels }),
    onSuccess: (s) => {
      qc.setQueryData(qk.sensor(s.id), s);
      toast.push('success', t('common.saved'));
    },
  });

  const diff = diffThresholds(sensor.channels, rows);
  const dirty = diff === 'invalid' || diff.length > 0;

  const save = () => {
    if (diff === 'invalid') {
      setInvalid(true);
      return;
    }
    setInvalid(false);
    if (diff.length) m.mutate(diff);
  };

  return (
    <div className="space-y-4">
      {sensor.channels.map((c) => (
        <fieldset key={c.key} className="min-w-0">
          {sensor.channels.length > 1 && (
            <legend className="mb-2 text-[13px] font-semibold">
              {t(`channel.${c.key}`, { defaultValue: c.key })}{' '}
              {c.unit && (
                <span className="font-normal text-ink-subtle">
                  (<bdi dir="auto">{c.unit}</bdi>)
                </span>
              )}
            </legend>
          )}
          {/* Upper limits first ("Warning above", "Down above"), then the lower ones. */}
          <div className="grid grid-cols-2 gap-x-3 gap-y-3">
            {FIELD_ORDER.map((k) => (
              <label key={k} className="block min-w-0">
                <span className="mb-1.5 block text-xs font-medium text-ink-muted">
                  {t(`thresholds.${k}`)}
                  {sensor.channels.length === 1 && c.unit && (
                    <span className="text-ink-subtle">
                      {' '}
                      (<bdi dir="auto">{c.unit}</bdi>)
                    </span>
                  )}
                </span>
                <Input
                  className="font-mono text-[13px]"
                  inputMode="decimal"
                  dir="ltr"
                  disabled={readOnly}
                  value={rows[c.key]?.[k] ?? ''}
                  placeholder="—"
                  aria-label={`${c.key} ${t(`thresholds.${k}`)}`}
                  onChange={(e) =>
                    setRows((r) => ({
                      ...r,
                      [c.key]: { ...(r[c.key] ?? toRow(c)), [k]: e.target.value },
                    }))
                  }
                />
              </label>
            ))}
          </div>
        </fieldset>
      ))}
      <p className="text-xs text-ink-muted">{t('thresholds.help')}</p>
      {invalid && <ErrorBox error={new Error(t('validation.number'))} />}
      <ErrorBox error={m.error} />
      {!readOnly && (
        <div className="flex flex-wrap gap-2">
          <Button variant="primary" disabled={!dirty} busy={m.isPending} onClick={save}>
            {t('thresholds.save')}
          </Button>
          <Button
            variant="ghost"
            disabled={!dirty}
            onClick={() =>
              setRows(Object.fromEntries(sensor.channels.map((c) => [c.key, toRow(c)])))
            }
          >
            {t('common.reset')}
          </Button>
        </div>
      )}
    </div>
  );
}
