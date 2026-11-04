import { useTranslation } from 'react-i18next';
import type { ConfigField, SensorType } from '../api/types';
import type { ConfigErrors, RawConfig } from '../lib/sensorConfig';
import { Checkbox, Field, Input, Select, Textarea } from './ui';

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
    <div className="grid gap-3 sm:grid-cols-2">
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
                      {o}
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
            const numeric = f.type === 'int' || f.type === 'int_nullable';
            const wide = f.key === 'url' || f.key === 'query_name' || f.key === 'keyword';
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
                  value={String(v ?? '')}
                  onChange={(e) => set(f.key, e.target.value)}
                  dir="ltr"
                  disabled={disabled}
                  placeholder={
                    f.type === 'str_nullable' || f.type === 'int_nullable' || f.type === 'int_list'
                      ? t('common.optional')
                      : undefined
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
