import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints3, qk3 } from '../../api/endpoints3';
import type { BackupMode, SshCredentials, SshTestResult } from '../../api/types3';
import { BACKUP_MODES } from '../../api/types3';
import { useFeatures } from '../../hooks/features';
import {
  sshFormErrors,
  sshFormToInput,
  type SecretState,
  type SshForm,
} from '../../lib/configBackup';
import { CheckIcon, XIcon } from '../icons';
import { Button, Checkbox, ErrorBox, Field, Input, Select, Textarea } from '../ui';

function Secret({
  label,
  state,
  isSet,
  onChange,
  multiline,
}: {
  label: string;
  state: SecretState;
  isSet: boolean;
  onChange: (s: SecretState) => void;
  multiline?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <div>
      <Field label={label} hint={isSet ? t('secret.keepHint') : undefined}>
        {multiline ? (
          <Textarea
            rows={3}
            value={state.value}
            disabled={state.clear}
            placeholder={isSet ? t('secret.stored') : '-----BEGIN OPENSSH PRIVATE KEY-----'}
            onChange={(e) => onChange({ ...state, value: e.target.value })}
            dir="ltr"
            spellCheck={false}
            autoComplete="off"
            className="font-mono text-xs"
          />
        ) : (
          <Input
            type="password"
            autoComplete="new-password"
            value={state.value}
            disabled={state.clear}
            placeholder={isSet ? t('secret.stored') : ''}
            onChange={(e) => onChange({ ...state, value: e.target.value })}
            dir="ltr"
          />
        )}
      </Field>
      {isSet && (
        <span className="mt-1 block">
          <Checkbox
            label={t('secret.clear')}
            checked={state.clear}
            onChange={(v) => onChange({ ...state, clear: v })}
          />
        </span>
      )}
    </div>
  );
}

export function SshTestView({ r }: { r: SshTestResult }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-2 text-sm" data-testid="ssh-test-result">
      <ul className="space-y-1">
        {r.steps.map((s) => (
          <li key={s.step} className="flex flex-wrap items-center gap-2">
            {s.ok ? (
              <CheckIcon size={14} className="text-status-up" aria-label={t('ldap.ok')} />
            ) : (
              <XIcon size={14} className="text-status-down" aria-label={t('ldap.failed')} />
            )}
            <span className="font-medium">{t(`ssh.step.${s.step}`)}</span>
            <bdi dir="ltr" className="font-mono text-xs text-ink-muted">
              {s.detail}
            </bdi>
            <span className="text-xs tabular-nums text-ink-subtle">{s.ms} ms</span>
          </li>
        ))}
      </ul>
      {r.driver && (
        <p className="text-xs text-ink-muted">
          {t('driver.label')}: <bdi dir="ltr">{r.driver}</bdi>
        </p>
      )}
      {r.output_preview && (
        <pre
          dir="ltr"
          className="max-h-48 overflow-auto rounded bg-surface-sunken p-2 text-left font-mono text-xs text-ink"
        >
          {r.output_preview}
        </pre>
      )}
    </div>
  );
}

/**
 * [3] SSH credentials (groups and devices; inheritance like SNMP) with, for saved devices, the
 * backup mode, driver override and a connection test (no version is stored).
 */
export function SshEditor({
  value,
  onChange,
  existing,
  inheritLabel,
  deviceId,
  mode,
  onMode,
  driver,
  onDriver,
}: {
  value: SshForm;
  onChange: (f: SshForm) => void;
  existing: SshCredentials | null | undefined;
  inheritLabel: string;
  deviceId?: number;
  mode?: BackupMode;
  onMode?: (m: BackupMode) => void;
  driver?: string;
  onDriver?: (d: string) => void;
}) {
  const { t } = useTranslation();
  const { features } = useFeatures();
  const [open, setOpen] = useState(!!existing || (mode !== undefined && mode !== 'auto'));
  const set = <K extends keyof SshForm>(k: K, v: SshForm[K]) => onChange({ ...value, [k]: v });
  const errs = sshFormErrors(value, existing);
  const runner = useQuery({
    queryKey: qk3.backupRunner,
    queryFn: endpoints3.backupRunner,
    enabled: open && features.config_backup && onDriver !== undefined,
    staleTime: 60_000,
  });
  const [result, setResult] = useState<SshTestResult | null>(null);
  const test = useMutation({
    mutationFn: () =>
      endpoints3.sshTest(deviceId as number, {
        ...(value.own ? { ssh_credentials: sshFormToInput(value) ?? undefined } : {}),
        driver: driver || null,
      }),
    onMutate: () => setResult(null),
    onSuccess: setResult,
  });
  const drivers = runner.data?.drivers ?? [];
  return (
    <details
      open={open}
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
      className="rounded-md border border-line p-3"
      data-testid="ssh-editor"
    >
      <summary className="cursor-pointer text-xs font-semibold text-ink-muted">
        {t('ssh.title')}
        {existing?.credentials_unreadable && (
          <span className="ms-2 text-status-down">{t('ssh.unreadable')}</span>
        )}
      </summary>
      <div className="mt-3 space-y-3">
        {!features.config_backup && (
          <p className="text-xs text-ink-muted">{t('ssh.prepareHint')}</p>
        )}
        <Checkbox label={inheritLabel} checked={!value.own} onChange={(v) => set('own', !v)} />
        {value.own && (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label={t('ssh.username')} required>
              <Input
                value={value.username}
                onChange={(e) => set('username', e.target.value)}
                maxLength={64}
                dir="ltr"
                autoComplete="off"
              />
            </Field>
            <Field label={t('ssh.port')}>
              <Input
                type="number"
                min={1}
                max={65535}
                value={value.port}
                onChange={(e) => set('port', e.target.value)}
                dir="ltr"
              />
            </Field>
            <Secret
              label={t('ssh.password')}
              state={value.password}
              isSet={!!existing?.password_set}
              onChange={(s) => set('password', s)}
            />
            <Secret
              label={t('ssh.enablePassword')}
              state={value.enable_password}
              isSet={!!existing?.enable_password_set}
              onChange={(s) => set('enable_password', s)}
            />
            <div className="sm:col-span-2">
              <Secret
                label={t('ssh.privateKey')}
                state={value.private_key}
                isSet={!!existing?.private_key_set}
                onChange={(s) => set('private_key', s)}
                multiline
              />
            </div>
            <Secret
              label={t('ssh.passphrase')}
              state={value.passphrase}
              isSet={!!existing?.passphrase_set}
              onChange={(s) => set('passphrase', s)}
            />
            <div className="flex items-end pb-1.5">
              <Checkbox
                label={t('ssh.legacy')}
                checked={value.legacy_algorithms}
                onChange={(v) => set('legacy_algorithms', v)}
              />
            </div>
            <p className="text-xs text-ink-muted sm:col-span-2">{t('ssh.legacyHint')}</p>
            {errs.length > 0 && (
              <ul className="text-xs text-status-down sm:col-span-2">
                {errs.map((e) => (
                  <li key={e}>{t(e)}</li>
                ))}
              </ul>
            )}
          </div>
        )}
        {mode !== undefined && onMode && (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label={t('configBackup.mode')} hint={t(`configBackup.modeHint.${mode}`)}>
              <Select value={mode} onChange={(e) => onMode(e.target.value as BackupMode)}>
                {BACKUP_MODES.map((m) => (
                  <option key={m} value={m}>
                    {t(`configBackup.modes.${m}`)}
                  </option>
                ))}
              </Select>
            </Field>
            {onDriver && (
              <Field label={t('driver.override')} hint={t('driver.overrideHint')}>
                {drivers.length ? (
                  <Select value={driver ?? ''} onChange={(e) => onDriver(e.target.value)}>
                    <option value="">{t('driver.auto')}</option>
                    {drivers.map((d) => (
                      <option key={d.id} value={d.id}>
                        {d.name}
                        {d.confidence === 'unverified' ? ` (${t('driver.unverified')})` : ''}
                      </option>
                    ))}
                  </Select>
                ) : (
                  <Input
                    value={driver ?? ''}
                    onChange={(e) => onDriver(e.target.value.trim())}
                    placeholder={t('driver.auto')}
                    dir="ltr"
                  />
                )}
              </Field>
            )}
          </div>
        )}
        {deviceId !== undefined && (
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                busy={test.isPending}
                disabled={value.own && errs.length > 0}
                onClick={() => test.mutate()}
              >
                {t('ssh.test')}
              </Button>
              <span className="text-xs text-ink-muted">{t('ssh.testHint')}</span>
            </div>
            <ErrorBox error={test.error} />
            {result && <SshTestView r={result} />}
          </div>
        )}
      </div>
    </details>
  );
}
