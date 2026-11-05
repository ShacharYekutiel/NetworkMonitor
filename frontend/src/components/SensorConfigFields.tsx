import { useTranslation } from 'react-i18next';
import type { ConfigField, SensorType } from '../api/types';
import {
  parseValueMapRows,
  VALUE_MAP_MAX,
  type ConfigErrors,
  type RawConfig,
  type ValueMapRow,
  type ValueMapStatus,
} from '../lib/sensorConfig';
import { PlusIcon, TrashIcon } from './icons';
import { Button, Checkbox, Field, FieldGroup, Input, Select, Textarea } from './ui';

const MAP_STATUSES: ValueMapStatus[] = ['up', 'warning', 'down'];

/** [2b-1] Editable table of an enum value map (value, status, text). */
export function ValueMapEditor({
  rows,
  onChange,
  disabled,
}: {
  rows: ValueMapRow[];
  onChange: (rows: ValueMapRow[]) => void;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  const setRow = (i: number, patch: Partial<ValueMapRow>) =>
    onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <div className="space-y-1.5">
      {rows.length > 0 && (
        <div className="hidden grid-cols-[6rem_8rem_minmax(0,1fr)_2rem] gap-2 text-[11px] font-medium uppercase tracking-wide text-slate-500 sm:grid">
          <span>{t('valueMap.value')}</span>
          <span>{t('valueMap.status')}</span>
          <span>{t('valueMap.text')}</span>
          <span />
        </div>
      )}
      {rows.map((r, i) => (
        <div
          key={i}
          className="grid grid-cols-[5rem_minmax(0,1fr)_2rem] gap-2 sm:grid-cols-[6rem_8rem_minmax(0,1fr)_2rem]"
        >
          <Input
            value={r.value}
            onChange={(e) => setRow(i, { value: e.target.value })}
            inputMode="numeric"
            dir="ltr"
            aria-label={t('valueMap.value')}
            disabled={disabled}
            className="font-mono"
          />
          <Select
            value={r.status}
            onChange={(e) => setRow(i, { status: e.target.value as ValueMapStatus })}
            aria-label={t('valueMap.status')}
            disabled={disabled}
          >
            {MAP_STATUSES.map((s) => (
              <option key={s} value={s}>
                {t(`status.${s}`)}
              </option>
            ))}
          </Select>
          <Input
            value={r.text}
            onChange={(e) => setRow(i, { text: e.target.value })}
            maxLength={32}
            dir="auto"
            placeholder={t('valueMap.textPlaceholder')}
            aria-label={t('valueMap.text')}
            disabled={disabled}
            className="col-span-2 sm:col-span-1"
          />
          <Button
            size="sm"
            variant="ghost"
            aria-label={t('common.delete')}
            disabled={disabled}
            onClick={() => onChange(rows.filter((_, j) => j !== i))}
            className="row-start-1 col-start-3 sm:row-auto sm:col-auto"
          >
            <TrashIcon size={14} />
          </Button>
        </div>
      ))}
      <Button
        size="sm"
        disabled={disabled || rows.length >= VALUE_MAP_MAX}
        onClick={() =>
          onChange([...rows, { value: String(rows.length + 1), status: 'up', text: '' }])
        }
      >
        <PlusIcon size={12} /> {t('valueMap.add')}
      </Button>
    </div>
  );
}

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
                      {t(`sensorConfigOption.${type}.${f.key}.${o}`, {
                        defaultValue: f.key === 'value_map_default' ? t(`status.${o}`) : o,
                      })}
                    </option>
                  ))}
                </Select>
              </Field>
            );
          case 'value_map':
            return (
              <FieldGroup
                key={f.key}
                label={label}
                required
                hint={
                  <>
                    {err && <span className="text-red-600">{err} </span>}
                    {t('valueMap.hint')}
                  </>
                }
                className="sm:col-span-2"
              >
                <ValueMapEditor
                  rows={parseValueMapRows(v)}
                  onChange={(rows) => set(f.key, JSON.stringify(rows))}
                  disabled={disabled}
                />
              </FieldGroup>
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
