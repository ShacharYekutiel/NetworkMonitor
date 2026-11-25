// [profiles] Create / edit a connection profile. Secrets are write-only (stored hints only).
import { useEffect, useState, type FormEvent } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { isApiError } from '../../api/client';
import {
  profilesApi,
  qkp,
  type ConnectionProfileInput,
  type ConnectionProfilePatch,
  type ProfileCredentialsInput,
} from '../../api/profiles';
import type { ConnectionProfile, CredentialKind } from '../../api/types';
import { CREDENTIAL_KINDS } from '../../api/types';
import { sshFormErrors, sshFormFrom, sshFormToInput, type SshForm } from '../../lib/configBackup';
import { snmpFormFrom, snmpFormToInput, type SnmpFormState } from '../../lib/snmp';
import { ModeRadios } from '../CredentialModePicker';
import { SnmpFields } from '../SnmpEditor';
import { SshFields } from '../configbackup/SshEditor';
import { useToast } from '../toast';
import { Button, ErrorBox, Field, FieldGroup, Input, Modal, Notice, Textarea } from '../ui';
import { ProfileTestDialog } from './ProfileTestDialog';

/** The request value of the credential form of a kind. */
export function profileCredentials(
  kind: CredentialKind,
  snmp: SnmpFormState,
  ssh: SshForm,
): ProfileCredentialsInput {
  return kind === 'snmp'
    ? (snmpFormToInput({ ...snmp, inherit: false }) ?? {})
    : (sshFormToInput({ ...ssh, own: true }) ?? { username: '' });
}

export function ProfileForm({
  open,
  onClose,
  profile,
  defaultKind = 'snmp',
}: {
  open: boolean;
  onClose: () => void;
  /** absent = create */
  profile?: ConnectionProfile;
  defaultKind?: CredentialKind;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const [kind, setKind] = useState<CredentialKind>(defaultKind);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [snmp, setSnmp] = useState<SnmpFormState>(snmpFormFrom(null));
  const [ssh, setSsh] = useState<SshForm>(sshFormFrom(null));
  const [touched, setTouched] = useState(false);
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    if (!open) return;
    setKind(profile?.kind ?? defaultKind);
    setName(profile?.name ?? '');
    setDescription(profile?.description ?? '');
    setSnmp({
      ...snmpFormFrom(profile?.kind === 'snmp' ? profile.credentials : null),
      inherit: false,
    });
    setSsh({ ...sshFormFrom(profile?.kind === 'ssh' ? profile.credentials : null), own: true });
    setTouched(false);
  }, [open, profile, defaultKind]);

  const sshExisting = profile?.kind === 'ssh' ? profile.credentials : null;
  const snmpExisting = profile?.kind === 'snmp' ? profile.credentials : null;
  const sshErrors = kind === 'ssh' ? sshFormErrors({ ...ssh, own: true }, sshExisting) : [];
  const nameOk = name.trim().length > 0 && name.trim().length <= 128;
  const valid = nameOk && description.length <= 1000 && sshErrors.length === 0;
  const credentials = () => profileCredentials(kind, snmp, ssh);

  const save = useMutation({
    mutationFn: () => {
      if (profile) {
        const body: ConnectionProfilePatch = { name: name.trim(), description };
        // secrets are write-only: credentials only when edited (merged over the stored object)
        if (touched) body.credentials = credentials();
        return profilesApi.update(profile.id, body);
      }
      const body: ConnectionProfileInput = {
        kind,
        name: name.trim(),
        description,
        credentials: credentials(),
      };
      return profilesApi.create(body);
    },
    onSuccess: (p) => {
      qc.setQueryData(qkp.one(p.id), p);
      void qc.invalidateQueries({ queryKey: qkp.all });
      // names and origins on group / device pages
      void qc.invalidateQueries({ queryKey: ['group'] });
      void qc.invalidateQueries({ queryKey: ['device'] });
      toast.push('success', t('profiles.saved'));
      onClose();
    },
  });

  const nameTaken = isApiError(save.error) && save.error.code === 'conflict';
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (valid) save.mutate();
  };
  const affected = profile?.usage.devices_effective ?? 0;

  return (
    <Modal
      open={open}
      onClose={onClose}
      wide
      title={profile ? t('profiles.editTitle') : t('profiles.addTitle')}
      testId="profile-form"
    >
      <form onSubmit={submit} className="space-y-4">
        {profile && affected > 0 && (
          <Notice tone="warning">{t('profiles.affects', { count: affected })}</Notice>
        )}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field
            label={t('profiles.name')}
            required
            hint={
              nameTaken ? (
                <span className="text-status-down">{t('errors.conflict')}</span>
              ) : undefined
            }
          >
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              maxLength={128}
              autoFocus
              aria-invalid={nameTaken}
            />
          </Field>
          <FieldGroup
            label={t('profiles.kindLabel')}
            hint={profile ? t('profiles.kindFixed') : undefined}
          >
            {profile ? (
              <span className="text-[14.5px] font-medium">
                {t(`profiles.kind.${profile.kind}`)}
              </span>
            ) : (
              <ModeRadios
                label={t('profiles.kindLabel')}
                value={kind}
                onChange={(k) => {
                  setKind(k);
                  setTouched(false);
                }}
                options={CREDENTIAL_KINDS.map((k) => ({
                  value: k,
                  label: t(`profiles.kind.${k}`),
                }))}
                testId="profile-kind"
              />
            )}
          </FieldGroup>
          <Field label={t('profiles.description')} className="sm:col-span-2">
            <Textarea
              rows={2}
              value={description}
              maxLength={1000}
              onChange={(e) => setDescription(e.target.value)}
              dir="auto"
            />
          </Field>
        </div>
        <fieldset className="min-w-0 rounded-lg border border-line px-4 py-3.5">
          <legend className="px-1 text-xs font-semibold text-ink-muted">
            {t(kind === 'snmp' ? 'snmp.title' : 'ssh.title')}
          </legend>
          {kind === 'snmp' ? (
            <SnmpFields
              value={snmp}
              onChange={(v) => {
                setSnmp(v);
                setTouched(true);
              }}
              existing={snmpExisting}
            />
          ) : (
            <SshFields
              value={ssh}
              onChange={(v) => {
                setSsh(v);
                setTouched(true);
              }}
              existing={sshExisting}
            />
          )}
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              disabled={sshErrors.length > 0}
              onClick={() => setTesting(true)}
              data-testid="profile-form-test"
            >
              {t('profiles.test')}
            </Button>
            <span className="text-xs text-ink-muted">{t('profiles.testUnsavedHint')}</span>
          </div>
        </fieldset>
        {!nameTaken && <ErrorBox error={save.error} />}
        <div className="flex flex-wrap justify-end gap-2">
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="submit" variant="primary" busy={save.isPending} disabled={!valid}>
            {t('common.save')}
          </Button>
        </div>
      </form>
      <ProfileTestDialog
        open={testing}
        onClose={() => setTesting(false)}
        kind={kind}
        name={profile?.name ?? name.trim()}
        profileId={profile?.id}
        credentials={!profile || touched ? credentials() : undefined}
      />
    </Modal>
  );
}
