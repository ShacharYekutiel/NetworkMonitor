// [profiles] Import: per-host "save the SNMP / SSH settings that worked" (unticked by default,
// owner decision 1.6.0), the column toggles and the inheritance hint per host.
import { useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import type { DiscoveryResult } from '../../api/types';
import {
  canSave,
  headerState,
  headerToggle,
  isSaved,
  setAll,
  type InheritHint,
  type SaveKind,
  type SaveOn,
} from '../../lib/importSave';
import { AlertIcon, CheckIcon } from '../icons';
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
  hint,
}: {
  r: DiscoveryResult;
  kind: SaveKind;
  save: SaveOn;
  onSave: (kind: SaveKind, on: boolean) => void;
  disabled?: boolean;
  /** show the label next to the box (cards); the table relies on the column header */
  withLabel?: boolean;
  /** what inheriting from the import group gives when the box stays unticked */
  hint?: InheritHint;
}) {
  const { t } = useTranslation();
  const can = canSave(r, kind);
  const mark = hint ? <InheritMark kind={kind} hint={hint} id={r.id} long={withLabel} /> : null;
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
  if (!withLabel)
    return (
      <span className="inline-flex flex-col items-center gap-0.5">
        {box}
        {mark}
      </span>
    );
  return (
    <span className="inline-flex flex-col gap-0.5">
      <label
        className={cx(
          'inline-flex items-center gap-2 text-[13px]',
          can ? 'cursor-pointer' : 'text-ink-subtle',
        )}
      >
        {box}
        {t(`discovery.save.${kind}`)}
      </label>
      {mark}
    </span>
  );
}

/**
 * The per-host inheritance hint under an unticked box: "risk" (the group gives other settings:
 * the device will probably not answer) in warning colour, "same" (nothing needs saving) muted.
 * Short in the narrow table column (icon + a word, full text as title and for screen readers),
 * the full sentence in cards.
 */
export function InheritMark({
  kind,
  hint,
  id,
  long,
}: {
  kind: SaveKind;
  hint: InheritHint;
  id: number;
  long?: boolean;
}) {
  const { t } = useTranslation();
  if (hint !== 'risk' && hint !== 'same') return null;
  const full = t(`discovery.inherit.${hint}Host${kind === 'snmp' ? 'Snmp' : 'Ssh'}`);
  const risk = hint === 'risk';
  return (
    <span
      className={cx(
        'inline-flex items-center gap-1 text-[11px] leading-tight',
        risk ? 'font-medium text-status-warning' : 'text-ink-muted',
      )}
      title={long ? undefined : full}
      data-testid={`inherit-${kind}-${id}`}
      data-hint={hint}
    >
      {risk ? (
        <AlertIcon size={12} className="shrink-0" />
      ) : (
        <CheckIcon size={12} className="shrink-0" />
      )}
      {long ? (
        full
      ) : (
        <>
          <span aria-hidden>{t(`discovery.inherit.${hint}Short`)}</span>
          <span className="sr-only">{full}</span>
        </>
      )}
    </span>
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
  save: SaveOn;
  onChange: (next: SaveOn) => void;
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
  const box = (
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
  );
  if (withLabel)
    return (
      <label className="inline-flex cursor-pointer items-center gap-2 whitespace-nowrap">
        {box}
        <span className="text-[13px]">{label}</span>
      </label>
    );
  // table header: the short label wraps above the box in a narrow column
  return (
    <label className="inline-flex cursor-pointer flex-col items-center gap-1.5" title={label}>
      <span className="whitespace-normal text-center leading-tight">
        {t(`discovery.save.${kind}`)}
      </span>
      {box}
    </label>
  );
}
