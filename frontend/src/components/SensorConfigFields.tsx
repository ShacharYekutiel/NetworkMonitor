import { useTranslation } from 'react-i18next';
import type { ConfigField, SensorType } from '../api/types';
import type { ConfigErrors, RawConfig } from '../lib/sensorConfig';
import { Checkbox, Field, Input, Select, Textarea } from './ui';

/** Free text that may be Hebrew (labels, units, match strings): direction follows the content. */
const FREE_TEXT_KEYS = ['label', 'unit', 'expected', 'keyword'];
const WIDE_KEYS = ['url', 'query_name', 'keyword', 'oid', 'percent_of_oid', 'storage_descr'];

export function SensorConfigFields({
  type,
  fields,
  raw,
  onChange,
  errors,
  disabled,
}: {
  type: SensorType;
  fields: ConfigField[];
  raw: RawConfig;
  onChange: (r: RawConfig) => void;
  errors: ConfigErrors;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  if (!fields.length) return <p className="text-sm text-slate-500">{t('sensors.noConfig')}</p>;
  const set = (k: string, v: string | boolean) => onChange({ ...raw, [k]: v });
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      {fields.map((f) => {
        const label = t(`sensorConfig.${type}.${f.key}`, { defaultValue: f.key });
        const range =
          f.min !== null || f.max !== null ? `${f.min ?? '…'} – ${f.max ?? '…'}` : undefined;
        const err = errors[f.key] ? t(errors[f.key], { min: f.min, max: f.max }) : undefined;
        const hintKey = `sensorConfigHint.${type}.${f.key}`;
        const hintText = t(hintKey, { defaultValue: '' });
        const hint = (
          <>
            {err && <span className="text-red-600">{err} </span>}
            {hintText || (range && <bdi dir="ltr">{range}</bdi>)}
          </>
        );
        const v = raw[f.key];
        switch (f.type) {
          case 'bool':
            return (
              <div key={f.key} className="flex items-end pb-1.5">
                <Checkbox
                  label={label}
                  checked={Boolean(v)}
                  onChange={(x) => set(f.key, x)}
                  disabled={disabled}
                />
              </div>
            );
          case 'enum':
            return (
              <Field key={f.key} label={label} required={f.required} hint={hint}>
                <Select
                  value={String(v ?? '')}
                  onChange={(e) => set(f.key, e.target.value)}
                  disabled={disabled}
                >
                  {(f.options ?? []).map((o) => (
                    <option key={o} value={o}>
                      {t(`sensorConfigOption.${type}.${f.key}.${o}`, { defaultValue: o })}
                    </option>
                  ))}
                </Select>
              </Field>
            );
          case 'headers':
            return (
              <Field
                key={f.key}
                label={label}
                hint={t('sensors.headersHint')}
                className="sm:col-span-2"
              >
                <Textarea
                  rows={3}
                  value={String(v ?? '')}
                  onChange={(e) => set(f.key, e.target.value)}
                  dir="ltr"
                  disabled={disabled}
                  className="font-mono"
                />
              </Field>
            );
          default: {
            const numeric = f.type === 'int' || f.type === 'int_nullable' || f.type === 'float';
            const wide = WIDE_KEYS.includes(f.key);
            return (
              <Field
                key={f.key}
                label={label}
                required={f.required}
                hint={hint}
                className={wide ? 'sm:col-span-2' : undefined}
              >
                <Input
                  type={numeric ? 'number' : 'text'}
                  min={f.min ?? undefined}
                  max={f.max ?? undefined}
                  step={f.type === 'float' ? 'any' : undefined}
                  value={String(v ?? '')}
                  onChange={(e) => set(f.key, e.target.value)}
                  dir={FREE_TEXT_KEYS.includes(f.key) ? 'auto' : 'ltr'}
                  disabled={disabled}
                  placeholder={
                    t(`sensorConfigPlaceholder.${type}.${f.key}`, { defaultValue: '' }) ||
                    (f.type === 'str_nullable' || f.type === 'int_nullable' || f.type === 'int_list'
                      ? t('common.optional')
                      : undefined)
                  }
                />
              </Field>
            );
          }
        }
      })}
    </div>
  );
}
