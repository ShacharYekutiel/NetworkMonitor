// [profiles] SNMP and SSH credential state of the group / device forms (modes + manual editors).
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { CredentialKind, Device, Group } from '../api/types';
import type { SshCredentials } from '../api/types3';
import {
  buildCredentialPatch,
  choiceValid,
  hasSavedManual,
  initialChoice,
  nodeMode,
  nodeProfileId,
  type CredentialPatch,
  type KindChoice,
} from '../lib/credentials';
import { sshFormErrors, sshFormFrom, sshFormToInput, type SshForm } from '../lib/configBackup';
import { snmpFormFrom, snmpFormToInput, type SnmpFormState } from '../lib/snmp';
import { CredentialModePicker, type PickerNode } from './CredentialModePicker';
import { SnmpFields } from './SnmpEditor';
import { SshFields } from './configbackup/SshEditor';

type Node = Group | Device;

export type NodeCredentials = ReturnType<typeof useNodeCredentials>;

/**
 * Form state of both kinds. `saved` follows the node prop and is replaced after a
 * "Copy into manual" (which the server saves at once).
 */
export function useNodeCredentials(node: Node | undefined, open: boolean) {
  const [saved, setSaved] = useState<Node | undefined>(node);
  const [snmpChoice, setSnmpChoice] = useState<KindChoice>(initialChoice(node, 'snmp'));
  const [sshChoice, setSshChoice] = useState<KindChoice>(initialChoice(node, 'ssh'));
  const [snmp, setSnmp] = useState<SnmpFormState>(snmpFormFrom(null));
  const [ssh, setSsh] = useState<SshForm>(sshFormFrom(null));

  const reset = (n: Node | undefined, kinds: CredentialKind[]) => {
    setSaved(n);
    if (kinds.includes('snmp')) {
      setSnmpChoice(initialChoice(n, 'snmp'));
      setSnmp({ ...snmpFormFrom(n?.snmp_credentials ?? null), inherit: false });
    }
    if (kinds.includes('ssh')) {
      setSshChoice(initialChoice(n, 'ssh'));
      setSsh({ ...sshFormFrom(n?.ssh_credentials), own: true });
    }
  };

  useEffect(() => {
    if (open) reset(node, ['snmp', 'ssh']);
  }, [open, node]);

  const sshExisting: SshCredentials | null | undefined = saved?.ssh_credentials;
  const sshManualActive =
    sshChoice.mode === 'manual' &&
    !(sshChoice.restore && nodeMode(saved, 'ssh') === 'profile' && hasSavedManual(saved, 'ssh'));
  const sshErrors = sshManualActive ? sshFormErrors({ ...ssh, own: true }, sshExisting) : [];
  const valid = choiceValid(snmpChoice) && choiceValid(sshChoice) && sshErrors.length === 0;

  /** The request keys of both kinds (call only when `valid`). */
  const patch = (): CredentialPatch => {
    const isNew = !saved;
    const common = (kind: CredentialKind, c: KindChoice) => ({
      kind,
      isNew,
      initialMode: nodeMode(saved, kind),
      initialProfileId: nodeProfileId(saved, kind),
      mode: c.mode,
      profileId: c.profileId,
      manualTouched: c.manualTouched,
      savedManual: hasSavedManual(saved, kind),
      restore: c.restore,
    });
    return {
      ...buildCredentialPatch({
        ...common('snmp', snmpChoice),
        manual: snmpFormToInput({ ...snmp, inherit: false }),
      }),
      ...buildCredentialPatch({
        ...common('ssh', sshChoice),
        manual: sshFormToInput({ ...ssh, own: true }),
      }),
    };
  };

  return {
    saved,
    snmpChoice,
    setSnmpChoice,
    sshChoice,
    setSshChoice,
    snmp,
    setSnmp: (v: SnmpFormState) => {
      setSnmp(v);
      setSnmpChoice((c) => ({ ...c, manualTouched: true }));
    },
    ssh,
    setSsh: (v: SshForm) => {
      setSsh(v);
      setSshChoice((c) => ({ ...c, manualTouched: true }));
    },
    sshExisting,
    sshManualActive,
    valid,
    patch,
    reset,
  };
}

function pickerNode(saved: Node | undefined, forGroup: boolean): PickerNode | undefined {
  if (!saved) return undefined;
  return forGroup ? { type: 'group', id: saved.id } : { type: 'device', id: saved.id };
}

/** The SNMP fieldset of a group / device form. */
export function SnmpCredentialsSection({
  state,
  forGroup,
  nodeName,
}: {
  state: NodeCredentials;
  forGroup: boolean;
  nodeName: string;
}) {
  const { t } = useTranslation();
  const s = state.saved;
  return (
    <fieldset className="min-w-0 rounded-lg border border-line px-4 py-3.5">
      <legend className="px-1 text-xs font-semibold text-ink-muted">{t('snmp.title')}</legend>
      <CredentialModePicker
        kind="snmp"
        forGroup={forGroup}
        nodeName={nodeName}
        node={pickerNode(s, forGroup)}
        initialMode={nodeMode(s, 'snmp')}
        savedManual={hasSavedManual(s, 'snmp')}
        origin={s?.effective_snmp}
        value={state.snmpChoice}
        onChange={state.setSnmpChoice}
        onCopied={(n) => state.reset(n, ['snmp'])}
        manual={
          <SnmpFields
            value={state.snmp}
            onChange={state.setSnmp}
            existing={s?.snmp_credentials ?? null}
          />
        }
      />
    </fieldset>
  );
}

/** The SSH credential choice (inside SshSection). */
export function SshCredentialsPicker({
  state,
  forGroup,
  nodeName,
}: {
  state: NodeCredentials;
  forGroup: boolean;
  nodeName: string;
}) {
  const s = state.saved;
  return (
    <CredentialModePicker
      kind="ssh"
      forGroup={forGroup}
      nodeName={nodeName}
      node={pickerNode(s, forGroup)}
      initialMode={nodeMode(s, 'ssh')}
      savedManual={hasSavedManual(s, 'ssh')}
      origin={s?.effective_ssh}
      value={state.sshChoice}
      onChange={state.setSshChoice}
      onCopied={(n) => state.reset(n, ['ssh'])}
      manual={<SshFields value={state.ssh} onChange={state.setSsh} existing={state.sshExisting} />}
    />
  );
}
