// [profiles] An ordered list of connection profiles of one kind (discovery scan options).
import { useId } from 'react';
import { useTranslation } from 'react-i18next';
import type { CredentialKind } from '../../api/types';
import { useKindProfiles } from '../CredentialModePicker';
import { ChevronIcon, TrashIcon } from '../icons';
import { Button, ErrorBox, ICON_BUTTON, Select } from '../ui';

export function moveItem<T>(list: T[], from: number, to: number): T[] {
  if (to < 0 || to >= list.length) return list;
  const out = [...list];
  const [x] = out.splice(from, 1);
  out.splice(to, 0, x);
  return out;
}

export function ProfileListPicker({
  kind,
  value,
  onChange,
  max,
  label,
  hint,
  error,
  testId,
}: {
  kind: CredentialKind;
  value: number[];
  onChange: (ids: number[]) => void;
  max: number;
  label: string;
  hint: string;
  error?: string;
  testId?: string;
}) {
  const { t } = useTranslation();
  const addId = useId();
  const q = useKindProfiles(kind);
  const profiles = (q.data?.items ?? []).filter((p) => p.kind === kind);
  const nameOf = (id: number) =>
    profiles.find((p) => p.id === id)?.name ?? t('discovery.deletedProfile', { id });
  const available = profiles.filter((p) => !value.includes(p.id));
  return (
    <div className="min-w-0 space-y-1.5" data-testid={testId}>
      <span className="block text-xs font-medium text-ink-muted" id={`${addId}-label`}>
        {label}
      </span>
      {value.length > 0 && (
        <ol className="m-0 list-none space-y-1 p-0" aria-labelledby={`${addId}-label`}>
          {value.map((id, i) => {
            const name = nameOf(id);
            return (
              <li
                key={id}
                className="flex items-center gap-1 rounded-md bg-surface px-2 py-1 text-sm"
              >
                <span className="w-5 shrink-0 text-xs tabular-nums text-ink-muted">{i + 1}.</span>
                <bdi className="min-w-0 flex-1 truncate">{name}</bdi>
                <Button
                  size="sm"
                  variant="ghost"
                  className={ICON_BUTTON.sm}
                  disabled={i === 0}
                  aria-label={t('discovery.moveUp', { name })}
                  onClick={() => onChange(moveItem(value, i, i - 1))}
                >
                  <ChevronIcon size={14} className="-rotate-90" />
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  className={ICON_BUTTON.sm}
                  disabled={i === value.length - 1}
                  aria-label={t('discovery.moveDown', { name })}
                  onClick={() => onChange(moveItem(value, i, i + 1))}
                >
                  <ChevronIcon size={14} className="rotate-90" />
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  className={ICON_BUTTON.sm}
                  aria-label={t('discovery.removeConnProfile', { name })}
                  onClick={() => onChange(value.filter((x) => x !== id))}
                >
                  <TrashIcon size={14} />
                </Button>
              </li>
            );
          })}
        </ol>
      )}
      {value.length < max && available.length > 0 && (
        <Select
          id={addId}
          value=""
          aria-label={t('discovery.addConnProfile')}
          onChange={(e) => e.target.value && onChange([...value, Number(e.target.value)])}
          className="sm:w-72"
        >
          <option value="">{t('discovery.addConnProfile')}…</option>
          {available.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </Select>
      )}
      {q.isSuccess && profiles.length === 0 && (
        <p className="m-0 text-xs text-ink-muted">
          {t(kind === 'snmp' ? 'discovery.noConnProfiles' : 'discovery.noSshProfiles')}
        </p>
      )}
      <ErrorBox error={q.error} />
      <span className="block text-xs text-ink-muted">
        {error ? <span className="text-status-down">{t(error)}</span> : hint}
      </span>
    </div>
  );
}
