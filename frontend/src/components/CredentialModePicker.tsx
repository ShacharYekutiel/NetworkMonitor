// [profiles] Per kind, the three-way choice of a group / device: Inherit / Profile / Manual.
import { useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { Link } from 'react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { profilesApi, qkp } from '../api/profiles';
import { qk } from '../api/endpoints';
import type { CredentialKind, CredentialMode, CredentialOrigin, Device, Group } from '../api/types';
import { CREDENTIAL_MODES } from '../api/types';
import { useAuth } from '../hooks/auth';
import {
  copyNeedsConfirm,
  needsDiscardConfirm,
  originText,
  type KindChoice,
} from '../lib/credentials';
import { useToast } from './toast';
import {
  Button,
  ConfirmDialog,
  cx,
  ErrorBox,
  Field,
  Notice,
  SEG_TRACK,
  segClass,
  Select,
} from './ui';

export type PickerNode = { type: 'group'; id: number } | { type: 'device'; id: number };

/** Admin-only list of the profiles of one kind (shared cache with the Settings tab). */
export function useKindProfiles(kind: CredentialKind, enabled = true) {
  const { isAdmin } = useAuth();
  return useQuery({
    queryKey: qkp.list({ kind }),
    queryFn: () => profilesApi.list({ kind }),
    enabled: enabled && isAdmin,
    staleTime: 30_000,
  });
}

/** A radio group drawn as a segmented control (arrow keys move and select, like native radios). */
export function ModeRadios<T extends string>({
  label,
  value,
  options,
  onChange,
  testId,
  wrap,
}: {
  label: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
  testId?: string;
  /** long labels: options wrap instead of scrolling (narrow screens) */
  wrap?: boolean;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKey = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    const rtl = getComputedStyle(e.currentTarget).direction === 'rtl';
    const next = { ArrowRight: rtl ? -1 : 1, ArrowLeft: rtl ? 1 : -1, ArrowDown: 1, ArrowUp: -1 }[
      e.key
    ];
    if (next === undefined) return;
    e.preventDefault();
    const j = (i + next + options.length) % options.length;
    refs.current[j]?.focus();
    onChange(options[j].value);
  };
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className={cx(SEG_TRACK, wrap && 'flex-wrap')}
      data-testid={testId}
    >
      {options.map((o, i) => (
        <button
          key={o.value}
          ref={(el) => {
            refs.current[i] = el;
          }}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          tabIndex={value === o.value ? 0 : -1}
          onClick={() => onChange(o.value)}
          onKeyDown={(e) => onKey(e, i)}
          className={cx(
            segClass(value === o.value),
            wrap && 'h-auto min-h-7 whitespace-normal py-1 text-start',
            'focus:outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus',
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function CredentialModePicker({
  kind,
  forGroup,
  nodeName,
  node,
  initialMode,
  savedManual,
  origin,
  value,
  onChange,
  manual,
  onCopied,
}: {
  kind: CredentialKind;
  forGroup: boolean;
  /** name of the group / device, for the confirmations */
  nodeName: string;
  /** the saved node (absent while creating) */
  node?: PickerNode;
  initialMode: CredentialMode;
  /** the saved node keeps hidden manual data under its profile */
  savedManual: boolean;
  origin?: CredentialOrigin;
  value: KindChoice;
  onChange: (c: KindChoice) => void;
  /** the manual editor (SnmpFields / SshFields) */
  manual: ReactNode;
  /** after "Copy into manual" (saved at once) */
  onCopied: (n: Group | Device) => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const selectId = useId();
  const kindName = t(`profiles.kind.${kind}`);
  const isNew = !node;
  const profiles = useKindProfiles(kind);
  const list = (profiles.data?.items ?? []).filter((p) => p.kind === kind);
  const selected = list.find((p) => p.id === value.profileId);
  const [confirmInherit, setConfirmInherit] = useState(false);
  const [confirmCopy, setConfirmCopy] = useState(false);
  const fromProfileWithSaved = !isNew && initialMode === 'profile' && savedManual;

  const pick = (m: CredentialMode) => {
    if (m === value.mode) return;
    if (needsDiscardConfirm(initialMode, savedManual, isNew, m)) {
      setConfirmInherit(true);
      return;
    }
    onChange({ ...value, mode: m });
  };

  const copy = useMutation({
    mutationFn: (): Promise<Group | Device> => {
      const body = value.profileId !== null ? { profile_id: value.profileId } : {};
      return node!.type === 'group'
        ? profilesApi.copyToGroup(node!.id, kind, body)
        : profilesApi.copyToDevice(node!.id, kind, body);
    },
    onSuccess: (n) => {
      if (node!.type === 'group') qc.setQueryData(qk.group(n.id), n);
      else qc.setQueryData(qk.device(n.id), n);
      void qc.invalidateQueries({ queryKey: qk.tree });
      void qc.invalidateQueries({ queryKey: qkp.all });
      toast.push('success', t('credMode.copyDone', { profile: selected?.name ?? '' }));
      setConfirmCopy(false);
      onCopied(n);
    },
  });

  const label = t(forGroup ? 'credMode.groupLabel' : 'credMode.deviceLabel', { kind: kindName });
  return (
    <div className="space-y-3" data-testid={`cred-picker-${kind}`}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <ModeRadios
          label={label}
          value={value.mode}
          onChange={pick}
          options={CREDENTIAL_MODES.map((m) => ({ value: m, label: t(`credMode.${m}`) }))}
          testId={`cred-mode-${kind}`}
        />
        {origin && (
          <span className="min-w-0 text-xs text-ink-muted" data-testid={`cred-origin-${kind}`}>
            {t('credMode.inEffect')}: <bdi>{originText(origin, t)}</bdi>
          </span>
        )}
      </div>

      {value.mode === 'inherit' && (
        <p className="m-0 text-xs text-ink-muted">
          {t(forGroup ? 'credMode.inheritHintGroup' : 'credMode.inheritHintDevice')}
        </p>
      )}

      {value.mode === 'profile' && (
        <div className="space-y-2">
          <Field
            label={t('credMode.profileLabel')}
            required
            hint={
              value.profileId === null ? (
                <span className="text-status-down">{t('credMode.profileRequired')}</span>
              ) : (
                t('credMode.profileHint')
              )
            }
          >
            <Select
              id={selectId}
              value={value.profileId === null ? '' : String(value.profileId)}
              onChange={(e) =>
                onChange({
                  ...value,
                  profileId: e.target.value === '' ? null : Number(e.target.value),
                })
              }
              aria-invalid={value.profileId === null}
              data-testid={`cred-profile-${kind}`}
            >
              <option value="">{t('credMode.chooseProfile')}</option>
              {/* keep a saved reference visible even before the list has loaded */}
              {value.profileId !== null && !selected && (
                <option value={value.profileId}>
                  {origin?.profile_id === value.profileId && origin.profile_name
                    ? origin.profile_name
                    : `#${value.profileId}`}
                </option>
              )}
              {list.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                  {p.credentials.credentials_unreadable ? ` (${t('credMode.unreadable')})` : ''}
                </option>
              ))}
            </Select>
          </Field>
          <ErrorBox error={profiles.error} />
          {profiles.isSuccess && list.length === 0 && (
            <p className="m-0 text-xs text-ink-muted">
              {t('credMode.noProfiles', { kind: kindName })}
            </p>
          )}
          {selected?.description && (
            <p className="m-0 text-xs text-ink-muted" dir="auto">
              {selected.description}
            </p>
          )}
          {!isNew && initialMode === 'manual' && (
            <Notice tone="neutral" className="text-[13px]">
              {t('credMode.keptNote')}
            </Notice>
          )}
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <Link
              to="/settings?tab=connectionProfiles"
              className="text-xs font-medium text-accent-ink hover:underline"
            >
              {t('credMode.manageProfiles')}
            </Link>
          </div>
          {node ? (
            <div className="space-y-1">
              <Button
                size="sm"
                disabled={value.profileId === null}
                busy={copy.isPending}
                onClick={() =>
                  copyNeedsConfirm(initialMode, savedManual) ? setConfirmCopy(true) : copy.mutate()
                }
                data-testid={`cred-copy-${kind}`}
              >
                {t('credMode.copy')}
              </Button>
              <p className="m-0 text-xs text-ink-muted">{t('credMode.copyHint')}</p>
              {!confirmCopy && <ErrorBox error={copy.error} />}
            </div>
          ) : (
            <p className="m-0 text-xs text-ink-subtle">{t('credMode.newNodeCopyHint')}</p>
          )}
        </div>
      )}

      {value.mode === 'manual' && (
        <div className="space-y-3">
          {fromProfileWithSaved && (
            <ModeRadios
              label={t('credMode.manual')}
              value={value.restore ? 'restore' : 'new'}
              onChange={(v) => onChange({ ...value, restore: v === 'restore' })}
              options={[
                { value: 'restore', label: t('credMode.restoreSaved') },
                { value: 'new', label: t('credMode.enterNew') },
              ]}
              testId={`cred-restore-${kind}`}
              wrap
            />
          )}
          {fromProfileWithSaved && value.restore ? (
            <p className="m-0 text-xs text-ink-muted">{t('credMode.restoreHint')}</p>
          ) : (
            manual
          )}
        </div>
      )}

      <ConfirmDialog
        open={confirmInherit}
        title={t('credMode.inheritConfirmTitle')}
        message={t('credMode.inheritConfirm', { kind: kindName, name: nodeName })}
        confirmLabel={t('credMode.inheritConfirmAction')}
        onConfirm={() => {
          setConfirmInherit(false);
          onChange({ ...value, mode: 'inherit' });
        }}
        onClose={() => setConfirmInherit(false)}
      />
      <ConfirmDialog
        open={confirmCopy}
        title={t('credMode.copyConfirmTitle')}
        message={t('credMode.copyConfirm', {
          kind: kindName,
          name: nodeName,
          profile: selected?.name ?? '',
        })}
        confirmLabel={t('credMode.copyConfirmAction')}
        onConfirm={() => copy.mutate()}
        onClose={() => setConfirmCopy(false)}
        busy={copy.isPending}
        error={copy.error}
      />
    </div>
  );
}
