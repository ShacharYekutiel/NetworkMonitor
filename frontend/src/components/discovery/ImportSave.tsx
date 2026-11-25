// [profiles] Import: per-host "save the SNMP / SSH settings that worked" and the column toggles.
import { useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import type { DiscoveryResult } from '../../api/types';
import {
  canSave,
  headerState,
  headerToggle,
  isSaved,
  setAll,
  type SaveKind,
  type SaveOff,
} from '../../lib/importSave';
import { CHECKBOX } from '../kit';
import { cx } from '../ui';

const BOX = cx(CHECKBOX, 'disabled:cursor-not-allowed disabled:opacity-40');

export function SaveCheckbox({
  r,
  kind,
  save,
  onSave,
  disabled,
  withLabel,
}: {
  r: DiscoveryResult;
  kind: SaveKind;
  save: SaveOff;
  onSave: (kind: SaveKind, on: boolean) => void;
  disabled?: boolean;
  /** show the label next to the box (cards); the table relies on the column header */
  withLabel?: boolean;
}) {
  const { t } = useTranslation();
  const can = canSave(r, kind);
  const box = (
    <input
      type="checkbox"
      className={BOX}
      aria-label={withLabel ? undefined : t(`discovery.save.${kind}For`, { ip: r.ip })}
      title={can ? undefined : t(`discovery.save.${kind}None`)}
      checked={isSaved(save, r, kind)}
      disabled={!can || disabled}
      onChange={(e) => onSave(kind, e.target.checked)}
      data-testid={`save-${kind}-${r.id}`}
    />
  );
  if (!withLabel) return box;
  return (
    <label
      className={cx(
        'inline-flex items-center gap-2 text-[13px]',
        can ? 'cursor-pointer' : 'text-ink-subtle',
      )}
    >
      {box}
      {t(`discovery.save.${kind}`)}
    </label>
  );
}

/** Tri-state select-all / clear-all of one column (over the selectable visible hosts). */
export function SaveHeaderCheckbox({
  rows,
  kind,
  save,
  onChange,
  disabled,
  withLabel,
}: {
  rows: DiscoveryResult[];
  kind: SaveKind;
  save: SaveOff;
  onChange: (next: SaveOff) => void;
  disabled?: boolean;
  withLabel?: boolean;
}) {
  const { t } = useTranslation();
  const ref = useRef<HTMLInputElement>(null);
  const state = headerState(save, rows, kind);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = state === 'mixed';
  }, [state]);
  const label = t(`discovery.save.${kind}All`);
  return (
    <label className="inline-flex cursor-pointer items-center gap-2 whitespace-nowrap">
      <input
        ref={ref}
        type="checkbox"
        className={BOX}
        aria-label={withLabel ? undefined : label}
        checked={state === 'all'}
        aria-checked={state === 'mixed' ? 'mixed' : state === 'all'}
        disabled={state === 'disabled' || disabled}
        onChange={() => onChange(setAll(save, rows, kind, headerToggle(state)))}
        data-testid={`save-${kind}-all`}
      />
      <span className={withLabel ? 'text-[13px]' : undefined}>
        {withLabel ? label : t(`discovery.save.${kind}`)}
      </span>
    </label>
  );
}
