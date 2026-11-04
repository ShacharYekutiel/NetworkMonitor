import { useTranslation } from 'react-i18next';
import type { SnmpCredentials } from '../api/types';
import type { SnmpFormState } from '../lib/snmp';
import { Checkbox, Field, Input, Select } from './ui';

export function SnmpEditor({
  value,
  onChange,
  existing,
  inheritLabel,
}: {
  value: SnmpFormState;
  onChange: (v: SnmpFormState) => void;
  existing: SnmpCredentials | null;
  inheritLabel: string;
}) {
  const { t } = useTranslation();
  const set = <K extends keyof SnmpFormState>(k: K, v: SnmpFormState[K]) =>
    onChange({ ...value, [k]: v });
  const auth = value.v3_security_level !== 'noAuthNoPriv';
  const priv = value.v3_security_level === 'authPriv';
  return (
    <fieldset className="rounded-md border border-slate-200 p-3 dark:border-slate-700">
      <legend className="px-1 text-xs font-semibold text-slate-600 dark:text-slate-400">
        {t('snmp.title')}
      </legend>
      <Checkbox label={inheritLabel} checked={value.inherit} onChange={(v) => set('inherit', v)} />
      {!value.inherit && (
        <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Field label={t('snmp.version')}>
            <Select
              value={value.version}
              onChange={(e) => set('version', e.target.value as SnmpFormState['version'])}
            >
              <option value="v1">v1</option>
              <option value="v2c">v2c</option>
              <option value="v3">v3</option>
            </Select>
          </Field>
          <Field label={t('snmp.port')}>
            <Input
              type="number"
              min={1}
              max={65535}
              value={value.port}
              onChange={(e) => set('port', e.target.value)}
              dir="ltr"
            />
          </Field>
          <Field label={t('snmp.timeout')}>
            <Input
              type="number"
              min={1}
              max={30}
              value={value.timeout_s}
              onChange={(e) => set('timeout_s', e.target.value)}
              dir="ltr"
            />
          </Field>
          <Field label={t('snmp.retries')}>
            <Input
              type="number"
              min={0}
              max={5}
              value={value.retries}
              onChange={(e) => set('retries', e.target.value)}
              dir="ltr"
            />
          </Field>
          {value.version !== 'v3' ? (
            <SecretField
              label={t('snmp.community')}
              className="col-span-2"
              value={value.community}
              isSet={value.community_set}
              clear={value.community_clear}
              onValue={(v) => set('community', v)}
              onClear={(v) => set('community_clear', v)}
              emptyHint={t('snmp.communityDefaultHint')}
            />
          ) : (
            <>
              <Field label={t('snmp.v3Username')} className="col-span-2">
                <Input
                  value={value.v3_username}
                  onChange={(e) => set('v3_username', e.target.value)}
                  dir="ltr"
                />
              </Field>
              <Field label={t('snmp.securityLevel')} className="col-span-2">
                <Select
                  value={value.v3_security_level}
                  onChange={(e) =>
                    set('v3_security_level', e.target.value as SnmpFormState['v3_security_level'])
                  }
                >
                  <option value="noAuthNoPriv">noAuthNoPriv</option>
                  <option value="authNoPriv">authNoPriv</option>
                  <option value="authPriv">authPriv</option>
                </Select>
              </Field>
              {auth && (
                <>
                  <Field label={t('snmp.authProtocol')}>
                    <Select
                      value={value.v3_auth_protocol}
                      onChange={(e) =>
                        set('v3_auth_protocol', e.target.value as SnmpFormState['v3_auth_protocol'])
                      }
                    >
                      {['MD5', 'SHA', 'SHA224', 'SHA256', 'SHA384', 'SHA512'].map((p) => (
                        <option key={p}>{p}</option>
                      ))}
                    </Select>
                  </Field>
                  <SecretField
                    label={t('snmp.authKey')}
                    value={value.v3_auth_key}
                    isSet={!!existing?.v3_auth_key_set}
                    clear={value.v3_auth_key_clear}
                    onValue={(v) => set('v3_auth_key', v)}
                    onClear={(v) => set('v3_auth_key_clear', v)}
                  />
                </>
              )}
              {priv && (
                <>
                  <Field label={t('snmp.privProtocol')}>
                    <Select
                      value={value.v3_priv_protocol}
                      onChange={(e) =>
                        set('v3_priv_protocol', e.target.value as SnmpFormState['v3_priv_protocol'])
                      }
                    >
                      {['DES', 'AES128', 'AES192', 'AES256'].map((p) => (
                        <option key={p}>{p}</option>
                      ))}
                    </Select>
                  </Field>
                  <SecretField
                    label={t('snmp.privKey')}
                    value={value.v3_priv_key}
                    isSet={!!existing?.v3_priv_key_set}
                    clear={value.v3_priv_key_clear}
                    onValue={(v) => set('v3_priv_key', v)}
                    onClear={(v) => set('v3_priv_key_clear', v)}
                  />
                </>
              )}
            </>
          )}
        </div>
      )}
    </fieldset>
  );
}

export function SecretField({
  label,
  value,
  isSet,
  clear,
  onValue,
  onClear,
  className,
  emptyHint,
}: {
  label: string;
  value: string;
  isSet: boolean;
  clear: boolean;
  onValue: (v: string) => void;
  onClear: (v: boolean) => void;
  className?: string;
  /** Hint when nothing is stored yet. */
  emptyHint?: string;
}) {
  const { t } = useTranslation();
  return (
    <div className={className}>
      <Field label={label} hint={isSet ? t('secret.keepHint') : emptyHint}>
        <Input
          type="password"
          autoComplete="new-password"
          value={value}
          disabled={clear}
          placeholder={isSet ? t('secret.stored') : ''}
          onChange={(e) => onValue(e.target.value)}
          dir="ltr"
        />
      </Field>
      {isSet && (
        <span className="mt-1 block">
          <Checkbox label={t('secret.clear')} checked={clear} onChange={onClear} />
        </span>
      )}
    </div>
  );
}
