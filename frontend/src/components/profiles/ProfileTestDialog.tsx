// [profiles] Connection test of a saved or an unsaved profile (read-only probing, nothing stored).
import { useEffect, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints3, qk3 } from '../../api/endpoints3';
import {
  profilesApi,
  type ProfileCredentialsInput,
  type ProfileTestTarget,
} from '../../api/profiles';
import type { CredentialKind, ProfileTestResult } from '../../api/types';
import { useFeatures } from '../../hooks/features';
import { DevicePicker } from '../Dependency';
import { ModeRadios } from '../CredentialModePicker';
import { DriverSelect, SshTestView } from '../configbackup/SshEditor';
import { CheckIcon } from '../icons';
import { Button, ErrorBox, Field, Input, KeyValue, Ltr, Modal, TruncLtr } from '../ui';

type TargetKind = 'device' | 'host';

/** The request target, or null when incomplete. */
export function testTarget(
  kind: TargetKind,
  deviceId: number | null,
  host: string,
): ProfileTestTarget | null {
  if (kind === 'device') return deviceId === null ? null : { device_id: deviceId };
  const h = host.trim();
  // one address or name: no range, CIDR, list or whitespace (the server checks the rest)
  if (!h || /[\s,;/]/.test(h)) return null;
  return { host: h };
}

export function ProfileTestResultView({ r }: { r: ProfileTestResult }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-2 text-sm" data-testid="profile-test-result">
      <p className="m-0 flex items-center gap-2 font-medium text-status-up">
        <CheckIcon size={14} aria-hidden="true" />
        <span>
          {t('profiles.testOk', { ms: r.ms })} · <Ltr>{r.host}</Ltr>
        </span>
      </p>
      {r.kind === 'snmp' ? (
        <KeyValue
          items={[
            { k: t('profiles.version'), v: <Ltr>{r.version}</Ltr> },
            { k: t('profiles.sysName'), v: r.sys_name ? <Ltr>{r.sys_name}</Ltr> : '—' },
            {
              k: t('profiles.sysDescr'),
              v: r.sys_descr ? (
                <TruncLtr title={r.sys_descr} className="max-w-full">
                  {r.sys_descr}
                </TruncLtr>
              ) : (
                '—'
              ),
            },
            {
              k: t('profiles.sysObjectId'),
              v: r.sys_object_id ? <Ltr className="font-mono">{r.sys_object_id}</Ltr> : '—',
            },
          ]}
        />
      ) : (
        <SshTestView r={r} />
      )}
    </div>
  );
}

export function ProfileTestDialog({
  open,
  onClose,
  kind,
  name,
  profileId,
  credentials,
}: {
  open: boolean;
  onClose: () => void;
  kind: CredentialKind;
  /** shown in the title (empty for a new profile) */
  name: string;
  /** a saved profile; absent = a profile not saved yet (`credentials` required) */
  profileId?: number;
  /** unsaved values (merged over the stored profile, or the whole object for a new one) */
  credentials?: ProfileCredentialsInput;
}) {
  const { t } = useTranslation();
  const { features } = useFeatures();
  const [targetKind, setTargetKind] = useState<TargetKind>('device');
  const [deviceId, setDeviceId] = useState<number | null>(null);
  const [host, setHost] = useState('');
  const [driver, setDriver] = useState('');
  const runner = useQuery({
    queryKey: qk3.backupRunner,
    queryFn: endpoints3.backupRunner,
    enabled: open && kind === 'ssh' && features.config_backup,
    staleTime: 60_000,
  });
  const test = useMutation({
    mutationFn: (target: ProfileTestTarget) => {
      const extra = kind === 'ssh' ? { driver: driver || null } : {};
      if (profileId !== undefined)
        return profilesApi.test(profileId, {
          ...target,
          ...(credentials ? { credentials } : {}),
          ...extra,
        });
      return profilesApi.testNew({ ...target, kind, credentials: credentials ?? {}, ...extra });
    },
  });
  const { reset } = test;
  useEffect(() => {
    if (open) reset();
  }, [open, reset]);

  const target = testTarget(targetKind, deviceId, host);
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={name ? t('profiles.testTitle', { name }) : t('profiles.testUnsaved')}
      testId="profile-test-dialog"
      footer={
        <>
          <Button
            variant="primary"
            busy={test.isPending}
            disabled={!target}
            onClick={() => target && test.mutate(target)}
          >
            {t('profiles.runTest')}
          </Button>
          <Button onClick={onClose}>{t('common.close')}</Button>
        </>
      }
    >
      <div className="space-y-3">
        <p className="m-0 text-xs text-ink-muted">
          {t(kind === 'snmp' ? 'profiles.snmpTestHint' : 'profiles.sshTestHint')}
          {credentials && <> {t('profiles.testUnsavedHint')}</>}
        </p>
        <ModeRadios
          label={t('profiles.target')}
          value={targetKind}
          onChange={setTargetKind}
          options={[
            { value: 'device', label: t('profiles.targetDevice') },
            { value: 'host', label: t('profiles.targetHost') },
          ]}
        />
        {targetKind === 'device' ? (
          <Field label={t('profiles.device')}>
            <DevicePicker value={deviceId} onChange={setDeviceId} label={t('profiles.device')} />
          </Field>
        ) : (
          <Field label={t('profiles.host')} hint={t('profiles.hostHint')}>
            <Input
              value={host}
              onChange={(e) => setHost(e.target.value)}
              dir="ltr"
              maxLength={255}
              className="font-mono"
              placeholder="10.0.0.5"
            />
          </Field>
        )}
        {kind === 'ssh' && (
          <Field label={t('profiles.driver')} hint={t('driver.overrideHint')}>
            <DriverSelect
              drivers={runner.data?.drivers ?? []}
              value={driver}
              onChange={setDriver}
            />
          </Field>
        )}
        <ErrorBox error={test.error} />
        {test.data && <ProfileTestResultView r={test.data} />}
      </div>
    </Modal>
  );
}
