import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { Channel, ChannelThresholdInput, Sensor } from '../api/types';
import { useToast } from './toast';
import { Button, ErrorBox, Input, Table, td, th } from './ui';

const KEYS = ['warning_min', 'warning_max', 'down_min', 'down_max'] as const;
type Key = (typeof KEYS)[number];
type Row = Record<Key, string>;

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
    <div className="space-y-3">
      <Table>
        <thead>
          <tr>
            <th className={th}>{t('sensors.channel')}</th>
            <th className={th}>{t('thresholds.warning_min')}</th>
            <th className={th}>{t('thresholds.warning_max')}</th>
            <th className={th}>{t('thresholds.down_min')}</th>
            <th className={th}>{t('thresholds.down_max')}</th>
          </tr>
        </thead>
        <tbody>
          {sensor.channels.map((c) => (
            <tr key={c.key}>
              <td className={td}>
                {t(`channel.${c.key}`, { defaultValue: c.key })}{' '}
                {c.unit && (
                  <span className="text-xs text-slate-500">
                    (<bdi dir="auto">{c.unit}</bdi>)
                  </span>
                )}
              </td>
              {KEYS.map((k) => (
                <td key={k} className={td}>
                  <Input
                    className="w-28"
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
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </Table>
      <p className="text-xs text-slate-500">{t('thresholds.help')}</p>
      {invalid && <ErrorBox error={new Error(t('validation.number'))} />}
      <ErrorBox error={m.error} />
      {!readOnly && (
        <div className="flex justify-end gap-2">
          <Button
            disabled={!dirty}
            onClick={() =>
              setRows(Object.fromEntries(sensor.channels.map((c) => [c.key, toRow(c)])))
            }
          >
            {t('common.reset')}
          </Button>
          <Button variant="primary" disabled={!dirty} busy={m.isPending} onClick={save}>
            {t('thresholds.save')}
          </Button>
        </div>
      )}
    </div>
  );
}
