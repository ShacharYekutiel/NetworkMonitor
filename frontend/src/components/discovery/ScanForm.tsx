import { useMemo, useState } from 'react';
import { Trans, useTranslation } from 'react-i18next';
import type { SnmpAuthProtocol, SnmpPrivProtocol, SnmpSecurityLevel } from '../../api/types';
import {
  DEFAULT_TCP_PORTS,
  MAX_EXTRA_COMMUNITIES,
  newExtraV3,
  type OptionsErrors,
  type ScanOptionsForm,
} from '../../lib/discovery';
import { formatNumber } from '../../lib/format';
import { MAX_HOSTS, MAX_TARGETS, summarizeTargets, type TargetsSummary } from '../../lib/targets';
import { PlusIcon, TrashIcon } from '../icons';
import { Button, Checkbox, cx, Field, FieldGroup, Input, Ltr, Select, Textarea } from '../ui';

export function useTargetsSummary(text: string): TargetsSummary {
  return useMemo(() => summarizeTargets(text), [text]);
}

/** Targets textarea with live validation hints. */
export function TargetsField({
  value,
  onChange,
  summary,
  showErrors,
}: {
  value: string;
  onChange: (v: string) => void;
  summary: TargetsSummary;
  showErrors?: boolean;
}) {
  const { t } = useTranslation();
  const n = summary.entries.length;
  const normalized = summary.entries.filter((e) => e.ok && e.normalized);
  // Do not flag the entry that is still being typed (no separator after it yet).
  const typing = !showErrors && !/[\s,;]$/.test(value) ? summary.entries[n - 1] : undefined;
  const errors = summary.errors.filter((e) => e !== typing);
  return (
    <Field
      label={t('discovery.targets')}
      required
      hint={
        <Trans
          i18nKey="discovery.targetsHint"
          components={{ c: <bdi dir="ltr" className="font-mono" /> }}
        />
      }
    >
      <Textarea
        rows={5}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        dir="ltr"
        spellCheck={false}
        className="font-mono"
        placeholder={'192.168.1.0/24\n192.168.10.1-30\nnas.lan'}
        aria-invalid={summary.errors.length > 0 || summary.tooManyHosts}
      />
      <span className="mt-1 block space-y-0.5 text-xs" aria-live="polite">
        {n > 0 && (
          <span
            className={cx(
              'block',
              summary.tooManyHosts
                ? 'font-medium text-red-600'
                : 'text-slate-600 dark:text-slate-300',
            )}
          >
            {t('discovery.targetsCount', { count: n })} ·{' '}
            {t('discovery.hostsCount', { count: summary.totalHosts })}
            {summary.tooManyHosts && (
              <> — {t('discovery.tooManyHosts', { max: formatNumber(MAX_HOSTS, 0) })}</>
            )}
          </span>
        )}
        {summary.tooManyEntries && (
          <span className="block font-medium text-red-600">
            {t('discovery.tooManyTargets', { max: MAX_TARGETS })}
          </span>
        )}
        {errors.slice(0, 5).map((e, i) => (
          <span key={`${e.text}-${i}`} className="block text-red-600">
            <Ltr className="font-mono">{e.text}</Ltr>: {t(`discovery.targetError.${e.error}`)}
          </span>
        ))}
        {errors.length > 5 && (
          <span className="block text-red-600">
            {t('discovery.moreErrors', { count: errors.length - 5 })}
          </span>
        )}
        {normalized.slice(0, 3).map((e) =>
          e.ok ? (
            <span key={e.text} className="block text-slate-500">
              <Ltr className="font-mono">
                {e.text} → {e.normalized}
              </Ltr>
            </span>
          ) : null,
        )}
      </span>
    </Field>
  );
}

const LEVELS: SnmpSecurityLevel[] = ['noAuthNoPriv', 'authNoPriv', 'authPriv'];
const AUTH: SnmpAuthProtocol[] = ['MD5', 'SHA', 'SHA224', 'SHA256', 'SHA384', 'SHA512'];
const PRIV: SnmpPrivProtocol[] = ['DES', 'AES128', 'AES192', 'AES256'];

function Err({ k }: { k?: string }) {
  const { t } = useTranslation();
  return k ? <span className="text-red-600">{t(k)}</span> : null;
}

function EyeIcon({ off }: { off: boolean }) {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      aria-hidden="true"
    >
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" />
      <circle cx="12" cy="12" r="3" />
      {off && <path d="M4 4l16 16" />}
    </svg>
  );
}

/** Write-only extra communities: one real password input per row (max 5), show/hide per row. */
function ExtraCommunities({
  values,
  onChange,
  error,
}: {
  values: string[];
  onChange: (v: string[]) => void;
  error?: string;
}) {
  const { t } = useTranslation();
  const [shown, setShown] = useState<boolean[]>([]);
  const label = t('discovery.extraCommunities');
  return (
    // Not a labelled group on purpose: each input carries the full label ("… 1", "… 2"), so
    // getByLabel / screen readers resolve to the inputs themselves.
    <div className="min-w-0">
      <span className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-400">
        {label}
      </span>
      <div className="space-y-1.5">
        {values.map((v, i) => (
          <div key={i} className="flex items-center gap-1.5">
            <Input
              type={shown[i] ? 'text' : 'password'}
              autoComplete="off"
              spellCheck={false}
              value={v}
              onChange={(e) => onChange(values.map((x, j) => (j === i ? e.target.value : x)))}
              dir="ltr"
              maxLength={64}
              className="font-mono"
              aria-label={`${label} ${i + 1}`}
            />
            <Button
              size="sm"
              variant="ghost"
              className="shrink-0"
              aria-label={shown[i] ? t('discovery.hideSecret') : t('discovery.showSecret')}
              aria-pressed={!!shown[i]}
              title={shown[i] ? t('discovery.hideSecret') : t('discovery.showSecret')}
              onClick={() => setShown((s) => values.map((_, j) => (j === i ? !s[j] : !!s[j])))}
            >
              <EyeIcon off={!!shown[i]} />
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="shrink-0"
              aria-label={t('discovery.removeCommunity', { n: i + 1 })}
              onClick={() => {
                onChange(values.length > 1 ? values.filter((_, j) => j !== i) : ['']);
                setShown((s) => s.filter((_, j) => j !== i));
              }}
            >
              <TrashIcon size={14} />
            </Button>
          </div>
        ))}
        {values.length < MAX_EXTRA_COMMUNITIES && (
          <Button size="sm" onClick={() => onChange([...values, ''])}>
            <PlusIcon size={14} /> {t('discovery.addCommunity')}
          </Button>
        )}
      </div>
      <span className="mt-1 block text-xs text-slate-500">
        {error ? <Err k={error} /> : t('discovery.extraCommunitiesHint')}
      </span>
    </div>
  );
}

/** Scan options (shared by the New scan form and the profile form). */
export function ScanOptionsEditor({
  value,
  onChange,
  errors,
  editingProfile,
}: {
  value: ScanOptionsForm;
  onChange: (v: ScanOptionsForm) => void;
  errors: OptionsErrors;
  /** Editing a stored profile: secret lists are kept unless the user chooses to replace them. */
  editingProfile?: boolean;
}) {
  const { t } = useTranslation();
  const [advanced, setAdvanced] = useState(false);
  const set = <K extends keyof ScanOptionsForm>(k: K, v: ScanOptionsForm[K]) =>
    onChange({ ...value, [k]: v });
  const setV3 = (i: number, patch: Partial<ScanOptionsForm['extra_v3'][number]>) =>
    set(
      'extra_v3',
      value.extra_v3.map((x, j) => (j === i ? { ...x, ...patch } : x)),
    );
  const secretsEditable = value.replace_secrets;

  return (
    <div className="space-y-3">
      <FieldGroup
        label={t('discovery.probes')}
        hint={errors.probes ? <Err k={errors.probes} /> : undefined}
      >
        <div className="flex flex-wrap gap-x-5 gap-y-2">
          <Checkbox
            label={t('discovery.optPing')}
            checked={value.ping}
            onChange={(v) => set('ping', v)}
          />
          <Checkbox
            label={t('discovery.optSnmp')}
            checked={value.snmp}
            onChange={(v) => set('snmp', v)}
          />
          <Checkbox
            label={t('discovery.optTcp')}
            checked={value.tcp}
            onChange={(v) => set('tcp', v)}
          />
          <Checkbox
            label={t('discovery.optDns')}
            checked={value.dns}
            onChange={(v) => set('dns', v)}
          />
        </div>
      </FieldGroup>

      {value.tcp && (
        <Field
          label={t('discovery.tcpPorts')}
          hint={errors.tcp_ports ? <Err k={errors.tcp_ports} /> : t('discovery.tcpPortsHint')}
        >
          <div className="flex gap-2">
            <Input
              value={value.tcp_ports}
              onChange={(e) => set('tcp_ports', e.target.value)}
              dir="ltr"
              className="font-mono"
              aria-invalid={!!errors.tcp_ports}
            />
            <Button
              size="sm"
              variant="ghost"
              className="shrink-0"
              onClick={() => set('tcp_ports', DEFAULT_TCP_PORTS.join(', '))}
            >
              {t('common.reset')}
            </Button>
          </div>
        </Field>
      )}

      {value.snmp && (
        <fieldset className="min-w-0 rounded-md border border-slate-200 p-3 dark:border-slate-700">
          <legend className="px-1 text-xs font-semibold text-slate-600 dark:text-slate-400">
            {t('discovery.extraCreds')}
          </legend>
          <p className="mb-2 text-xs text-slate-500">{t('discovery.extraCredsHint')}</p>
          {editingProfile && (
            <div className="mb-2 space-y-1 text-xs text-slate-600 dark:text-slate-300">
              <div>
                {t('discovery.storedSecrets', {
                  communities: value.stored_communities,
                  v3: value.stored_v3,
                })}
              </div>
              <Checkbox
                label={t('discovery.replaceSecrets')}
                checked={value.replace_secrets}
                onChange={(v) => set('replace_secrets', v)}
              />
            </div>
          )}
          {secretsEditable && (
            <div className="space-y-3">
              <ExtraCommunities
                values={value.extra_communities}
                onChange={(v) => set('extra_communities', v)}
                error={errors.extra_communities}
              />
              <div className="space-y-2">
                {value.extra_v3.map((v, i) => {
                  const auth = v.v3_security_level !== 'noAuthNoPriv';
                  const priv = v.v3_security_level === 'authPriv';
                  return (
                    <div
                      key={i}
                      className="grid grid-cols-1 gap-2 rounded bg-slate-50 p-2 ring-1 ring-inset ring-slate-200 dark:bg-slate-800/50 dark:ring-slate-700 sm:grid-cols-3"
                    >
                      <Field label={t('snmp.v3Username')}>
                        <Input
                          value={v.v3_username}
                          onChange={(e) => setV3(i, { v3_username: e.target.value })}
                          dir="ltr"
                          autoComplete="off"
                        />
                      </Field>
                      <Field label={t('snmp.securityLevel')}>
                        <Select
                          value={v.v3_security_level}
                          onChange={(e) =>
                            setV3(i, { v3_security_level: e.target.value as SnmpSecurityLevel })
                          }
                        >
                          {LEVELS.map((l) => (
                            <option key={l}>{l}</option>
                          ))}
                        </Select>
                      </Field>
                      <div className="flex items-end justify-end">
                        <Button
                          size="sm"
                          variant="ghost"
                          aria-label={t('common.delete')}
                          onClick={() =>
                            set(
                              'extra_v3',
                              value.extra_v3.filter((_, j) => j !== i),
                            )
                          }
                        >
                          <TrashIcon size={14} />
                        </Button>
                      </div>
                      {auth && (
                        <>
                          <Field label={t('snmp.authProtocol')}>
                            <Select
                              value={v.v3_auth_protocol}
                              onChange={(e) =>
                                setV3(i, { v3_auth_protocol: e.target.value as SnmpAuthProtocol })
                              }
                            >
                              {AUTH.map((p) => (
                                <option key={p}>{p}</option>
                              ))}
                            </Select>
                          </Field>
                          <Field label={t('snmp.authKey')} className="sm:col-span-2">
                            <Input
                              type="password"
                              autoComplete="new-password"
                              value={v.v3_auth_key}
                              onChange={(e) => setV3(i, { v3_auth_key: e.target.value })}
                              dir="ltr"
                            />
                          </Field>
                        </>
                      )}
                      {priv && (
                        <>
                          <Field label={t('snmp.privProtocol')}>
                            <Select
                              value={v.v3_priv_protocol}
                              onChange={(e) =>
                                setV3(i, { v3_priv_protocol: e.target.value as SnmpPrivProtocol })
                              }
                            >
                              {PRIV.map((p) => (
                                <option key={p}>{p}</option>
                              ))}
                            </Select>
                          </Field>
                          <Field label={t('snmp.privKey')} className="sm:col-span-2">
                            <Input
                              type="password"
                              autoComplete="new-password"
                              value={v.v3_priv_key}
                              onChange={(e) => setV3(i, { v3_priv_key: e.target.value })}
                              dir="ltr"
                            />
                          </Field>
                        </>
                      )}
                    </div>
                  );
                })}
                {errors.extra_v3 && (
                  <p className="text-xs">
                    <Err k={errors.extra_v3} />
                  </p>
                )}
                {value.extra_v3.length < 3 && (
                  <Button
                    size="sm"
                    onClick={() => set('extra_v3', [...value.extra_v3, newExtraV3()])}
                  >
                    <PlusIcon size={14} /> {t('discovery.addV3')}
                  </Button>
                )}
              </div>
            </div>
          )}
        </fieldset>
      )}

      <div>
        <button
          type="button"
          className="text-xs font-medium text-sky-700 hover:underline dark:text-sky-400"
          aria-expanded={advanced}
          onClick={() => setAdvanced((a) => !a)}
        >
          {advanced ? '▾' : '▸'} {t('discovery.advanced')}
        </button>
        {advanced && (
          <div className="mt-2 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Field
              label={t('discovery.snmpPort')}
              hint={errors.snmp_port ? <Err k={errors.snmp_port} /> : t('discovery.snmpPortHint')}
            >
              <Input
                type="number"
                min={1}
                max={65535}
                value={value.snmp_port}
                placeholder={t('common.default')}
                onChange={(e) => set('snmp_port', e.target.value)}
                dir="ltr"
              />
            </Field>
            <Field
              label={t('discovery.snmpTimeout')}
              hint={errors.snmp_timeout_s ? <Err k={errors.snmp_timeout_s} /> : undefined}
            >
              <Input
                type="number"
                min={1}
                max={10}
                value={value.snmp_timeout_s}
                onChange={(e) => set('snmp_timeout_s', e.target.value)}
                dir="ltr"
              />
            </Field>
            <Field
              label={t('discovery.pingTimeout')}
              hint={errors.ping_timeout_ms ? <Err k={errors.ping_timeout_ms} /> : undefined}
            >
              <Input
                type="number"
                min={100}
                max={5000}
                step={100}
                value={value.ping_timeout_ms}
                onChange={(e) => set('ping_timeout_ms', e.target.value)}
                dir="ltr"
              />
            </Field>
            <Field
              label={t('discovery.tcpTimeout')}
              hint={errors.tcp_timeout_ms ? <Err k={errors.tcp_timeout_ms} /> : undefined}
            >
              <Input
                type="number"
                min={100}
                max={5000}
                step={100}
                value={value.tcp_timeout_ms}
                onChange={(e) => set('tcp_timeout_ms', e.target.value)}
                dir="ltr"
              />
            </Field>
          </div>
        )}
      </div>
    </div>
  );
}
