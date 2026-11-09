import { useEffect, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import type {
  Settings,
  SettingsInput,
  SnmpAuthProtocol,
  SnmpPrivProtocol,
  SnmpSecurityLevel,
} from '../../api/types';
import {
  intInRange,
  newV3Row,
  parseSources,
  v3Input,
  v3RowError,
  v3Rows,
  type V3Row,
} from '../../lib/ingestSettings';
import { PlusIcon, TrashIcon } from '../icons';
import {
  Button,
  Card,
  Checkbox,
  ErrorBox,
  Field,
  FieldGroup,
  Input,
  Select,
  Textarea,
} from '../ui';
import { FeatureSection, useSaveSettings } from './common';
import { IngestStatusCard } from './IngestStatus';
import { FeatureToggleCard } from './FeatureToggle';
import { featuresFromSettings, featuresOfTab, ingestOn } from '../../lib/features';

const LEVELS: SnmpSecurityLevel[] = ['noAuthNoPriv', 'authNoPriv', 'authPriv'];
const AUTH: SnmpAuthProtocol[] = ['MD5', 'SHA', 'SHA224', 'SHA256', 'SHA384', 'SHA512'];
const PRIV: SnmpPrivProtocol[] = ['DES', 'AES128', 'AES192', 'AES256'];

function V3UsersEditor({ rows, onChange }: { rows: V3Row[]; onChange: (r: V3Row[]) => void }) {
  const { t } = useTranslation();
  const set = (i: number, p: Partial<V3Row>) =>
    onChange(rows.map((r, j) => (j === i ? { ...r, ...p } : r)));
  return (
    <FieldGroup label={t('ingestSettings.v3.title')} hint={t('ingestSettings.v3.hint')}>
      <div className="space-y-2">
        {rows.map((r, i) => {
          const err = v3RowError(r, rows);
          const keep = r.storedName === r.username.trim();
          return (
            <div
              key={i}
              className="rounded-md border border-slate-200 p-2 dark:border-slate-700"
              data-testid="v3-row"
            >
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-6">
                <Input
                  value={r.username}
                  onChange={(e) => set(i, { username: e.target.value })}
                  placeholder={t('users.username')}
                  aria-label={t('users.username')}
                  dir="ltr"
                  className="sm:col-span-2"
                />
                <Select
                  value={r.security_level}
                  onChange={(e) => set(i, { security_level: e.target.value as SnmpSecurityLevel })}
                  aria-label={t('snmp.securityLevel')}
                  className="sm:col-span-2"
                >
                  {LEVELS.map((l) => (
                    <option key={l} value={l}>
                      {l}
                    </option>
                  ))}
                </Select>
                <div className="col-span-2 flex justify-end">
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={t('common.delete')}
                    onClick={() => onChange(rows.filter((_, j) => j !== i))}
                  >
                    <TrashIcon size={13} />
                  </Button>
                </div>
                {r.security_level !== 'noAuthNoPriv' && (
                  <>
                    <Select
                      value={r.auth_protocol}
                      onChange={(e) =>
                        set(i, { auth_protocol: e.target.value as SnmpAuthProtocol })
                      }
                      aria-label={t('snmp.authProtocol')}
                    >
                      {AUTH.map((a) => (
                        <option key={a}>{a}</option>
                      ))}
                    </Select>
                    <Input
                      type="password"
                      autoComplete="new-password"
                      value={r.authKey}
                      onChange={(e) => set(i, { authKey: e.target.value })}
                      placeholder={keep && r.authSet ? t('secret.stored') : t('snmp.authKey')}
                      aria-label={t('snmp.authKey')}
                      dir="ltr"
                      className="sm:col-span-2"
                    />
                  </>
                )}
                {r.security_level === 'authPriv' && (
                  <>
                    <Select
                      value={r.priv_protocol}
                      onChange={(e) =>
                        set(i, { priv_protocol: e.target.value as SnmpPrivProtocol })
                      }
                      aria-label={t('snmp.privProtocol')}
                    >
                      {PRIV.map((a) => (
                        <option key={a}>{a}</option>
                      ))}
                    </Select>
                    <Input
                      type="password"
                      autoComplete="new-password"
                      value={r.privKey}
                      onChange={(e) => set(i, { privKey: e.target.value })}
                      placeholder={keep && r.privSet ? t('secret.stored') : t('snmp.privKey')}
                      aria-label={t('snmp.privKey')}
                      dir="ltr"
                      className="sm:col-span-2"
                    />
                  </>
                )}
              </div>
              {err && <p className="mt-1 text-xs text-red-600">{t(err)}</p>}
            </div>
          );
        })}
        {rows.length < 32 && (
          <Button size="sm" onClick={() => onChange([...rows, newV3Row()])}>
            <PlusIcon size={12} /> {t('ingestSettings.v3.add')}
          </Button>
        )}
      </div>
    </FieldGroup>
  );
}

/** Owner decision: link-down/up and flapping alerts for important ports only, or for all ports. */
export function LinkScopeRadio({
  value,
  onChange,
}: {
  value: 'important' | 'all';
  onChange: (v: 'important' | 'all') => void;
}) {
  const { t } = useTranslation();
  return (
    <FieldGroup label={t('ingestSettings.linkScope.label')}>
      <div className="space-y-1.5" data-testid="linkdown-scope">
        {(['important', 'all'] as const).map((v) => (
          <label key={v} className="flex cursor-pointer items-start gap-2 text-sm">
            <input
              type="radio"
              name="event_linkdown_scope"
              value={v}
              checked={value === v}
              onChange={() => onChange(v)}
              className="mt-0.5 h-4 w-4 border-slate-300 text-sky-600 dark:border-slate-600 dark:bg-slate-900"
            />
            <span>
              <span className="font-medium">{t(`ingestSettings.linkScope.${v}`)}</span>
              <span className="block text-xs text-slate-500">
                {t(`ingestSettings.linkScope.${v}Hint`)}
              </span>
            </span>
          </label>
        ))}
      </div>
    </FieldGroup>
  );
}

export function IngestSettingsCard({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const [trapDays, setTrapDays] = useState('30');
  const [syslogDays, setSyslogDays] = useState('30');
  const [check, setCheck] = useState<'any' | 'known'>('any');
  const [replaceComm, setReplaceComm] = useState(false);
  const [communities, setCommunities] = useState('');
  const [unknown, setUnknown] = useState<'store' | 'drop'>('store');
  const [ignored, setIgnored] = useState('');
  const [v3, setV3] = useState<V3Row[]>([]);
  const [linkScope, setLinkScope] = useState<'important' | 'all'>('important');
  useEffect(() => {
    setLinkScope(settings.event_linkdown_scope ?? 'important');
    setTrapDays(String(settings.trap_retention_days ?? 30));
    setSyslogDays(String(settings.syslog_retention_days ?? 30));
    setCheck(settings.trap_community_check ?? 'any');
    setReplaceComm(false);
    setCommunities('');
    setUnknown(settings.ingest_unknown_sources ?? 'store');
    setIgnored((settings.ingest_ignored_sources ?? []).join('\n'));
    setV3(v3Rows(settings.trap_v3_users));
  }, [settings]);
  const save = useSaveSettings();
  const ign = parseSources(ignored);
  const comm = communities
    .split(/[\s,]+/)
    .map((x) => x.trim())
    .filter(Boolean);
  const commBad = replaceComm && (comm.length > 32 || comm.some((c) => c.length > 64));
  const errs = {
    trapDays: !intInRange(trapDays, 1, 3650),
    syslogDays: !intInRange(syslogDays, 1, 3650),
    ignored: ign.invalid.length > 0 || ign.tooMany,
    v3: v3.some((r) => v3RowError(r, v3) !== null),
    known: check === 'known' && !(replaceComm ? comm.length : settings.trap_communities_count),
  };
  const invalid = Object.values(errs).some(Boolean) || commBad;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (invalid) return;
    const body: SettingsInput = {
      trap_retention_days: Number(trapDays),
      syslog_retention_days: Number(syslogDays),
      trap_community_check: check,
      ingest_unknown_sources: unknown,
      ingest_ignored_sources: ign.list,
      trap_v3_users: v3Input(v3),
      event_linkdown_scope: linkScope,
    };
    if (replaceComm) body.trap_communities = comm;
    save.mutate(body);
  };
  const bad = (on: boolean, key: string, ok?: string) =>
    on ? <span className="text-red-600">{t(key)}</span> : ok;
  return (
    <Card title={t('ingestSettings.title')}>
      <form onSubmit={submit} className="space-y-4" data-testid="ingest-settings">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field
            label={t('ingestSettings.trapRetention')}
            hint={bad(errs.trapDays, 'ingestSettings.retentionErr')}
          >
            <Input
              type="number"
              min={1}
              max={3650}
              value={trapDays}
              onChange={(e) => setTrapDays(e.target.value)}
              dir="ltr"
            />
          </Field>
          <Field
            label={t('ingestSettings.syslogRetention')}
            hint={bad(errs.syslogDays, 'ingestSettings.retentionErr')}
          >
            <Input
              type="number"
              min={1}
              max={3650}
              value={syslogDays}
              onChange={(e) => setSyslogDays(e.target.value)}
              dir="ltr"
            />
          </Field>
          <Field
            label={t('ingestSettings.unknownPolicy')}
            hint={t(`ingestSettings.unknownHint.${unknown}`)}
          >
            <Select
              value={unknown}
              onChange={(e) => setUnknown(e.target.value as 'store' | 'drop')}
            >
              <option value="store">{t('ingestSettings.unknown.store')}</option>
              <option value="drop">{t('ingestSettings.unknown.drop')}</option>
            </Select>
          </Field>
        </div>
        <fieldset className="space-y-3 rounded-md border border-slate-200 p-3 dark:border-slate-700">
          <legend className="px-1 text-xs font-semibold text-slate-600 dark:text-slate-400">
            {t('ingestSettings.trapAuth')}
          </legend>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field
              label={t('ingestSettings.communityCheck')}
              hint={bad(
                errs.known,
                'ingestSettings.knownNeedsList',
                t(`ingestSettings.checkHint.${check}`),
              )}
            >
              <Select value={check} onChange={(e) => setCheck(e.target.value as 'any' | 'known')}>
                <option value="any">{t('ingestSettings.check.any')}</option>
                <option value="known">{t('ingestSettings.check.known')}</option>
              </Select>
            </Field>
            <div className="space-y-1.5">
              <div className="text-xs font-medium text-slate-600 dark:text-slate-400">
                {t('ingestSettings.communities')}
              </div>
              <p className="text-xs text-slate-500">
                {t('ingestSettings.communitiesStored', { n: settings.trap_communities_count ?? 0 })}
              </p>
              <Checkbox
                label={t('ingestSettings.replaceCommunities')}
                checked={replaceComm}
                onChange={setReplaceComm}
              />
              {replaceComm && (
                <Textarea
                  rows={2}
                  value={communities}
                  onChange={(e) => setCommunities(e.target.value)}
                  placeholder={t('ingestSettings.communitiesPlaceholder')}
                  aria-label={t('ingestSettings.communities')}
                  dir="ltr"
                  autoComplete="off"
                  className="font-mono"
                />
              )}
              {commBad && (
                <p className="text-xs text-red-600">{t('ingestSettings.communitiesErr')}</p>
              )}
            </div>
          </div>
          <V3UsersEditor rows={v3} onChange={setV3} />
        </fieldset>
        <LinkScopeRadio value={linkScope} onChange={setLinkScope} />
        <Field
          label={t('ingestSettings.ignored')}
          hint={
            errs.ignored ? (
              <span className="text-red-600">
                {ign.tooMany
                  ? t('ingestSettings.ignoredTooMany')
                  : t('ingestSettings.ignoredInvalid', { list: ign.invalid.join(', ') })}
              </span>
            ) : (
              t('ingestSettings.ignoredHint')
            )
          }
        >
          <Textarea
            rows={3}
            value={ignored}
            onChange={(e) => setIgnored(e.target.value)}
            dir="ltr"
            className="font-mono"
          />
        </Field>
        <ErrorBox error={save.error} />
        <div className="flex justify-end">
          <Button type="submit" variant="primary" busy={save.isPending} disabled={invalid}>
            {t('common.save')}
          </Button>
        </div>
      </form>
    </Card>
  );
}

export function FlappingCard({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const [threshold, setThreshold] = useState('5');
  const [windowMin, setWindowMin] = useState('10');
  const [quiet, setQuiet] = useState('15');
  useEffect(() => {
    setThreshold(String(settings.flap_threshold ?? 5));
    setWindowMin(String(settings.flap_window_min ?? 10));
    setQuiet(String(settings.flap_quiet_min ?? 15));
  }, [settings]);
  const save = useSaveSettings();
  const e = {
    threshold: !intInRange(threshold, 3, 50),
    window: !intInRange(windowMin, 1, 120),
    quiet: !intInRange(quiet, 1, 1440),
  };
  const invalid = e.threshold || e.window || e.quiet;
  return (
    <Card title={t('flapping.title')}>
      <form
        className="space-y-4"
        data-testid="flapping-settings"
        onSubmit={(ev) => {
          ev.preventDefault();
          if (!invalid)
            save.mutate({
              flap_threshold: Number(threshold),
              flap_window_min: Number(windowMin),
              flap_quiet_min: Number(quiet),
            });
        }}
      >
        <p className="text-xs text-slate-500">
          {t('flapping.intro', { n: threshold || '?', w: windowMin || '?', q: quiet || '?' })}
        </p>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field
            label={t('flapping.threshold')}
            hint={
              e.threshold ? (
                <span className="text-red-600">{t('flapping.thresholdErr')}</span>
              ) : (
                t('flapping.thresholdHint')
              )
            }
          >
            <Input
              type="number"
              min={3}
              max={50}
              value={threshold}
              onChange={(x) => setThreshold(x.target.value)}
              dir="ltr"
            />
          </Field>
          <Field
            label={t('flapping.window')}
            hint={
              e.window ? <span className="text-red-600">{t('flapping.windowErr')}</span> : undefined
            }
          >
            <Input
              type="number"
              min={1}
              max={120}
              value={windowMin}
              onChange={(x) => setWindowMin(x.target.value)}
              dir="ltr"
            />
          </Field>
          <Field
            label={t('flapping.quiet')}
            hint={
              e.quiet ? (
                <span className="text-red-600">{t('flapping.quietErr')}</span>
              ) : (
                t('flapping.quietHint')
              )
            }
          >
            <Input
              type="number"
              min={1}
              max={1440}
              value={quiet}
              onChange={(x) => setQuiet(x.target.value)}
              dir="ltr"
            />
          </Field>
        </div>
        <ErrorBox error={save.error} />
        <div className="flex justify-end">
          <Button type="submit" variant="primary" busy={save.isPending} disabled={invalid}>
            {t('common.save')}
          </Button>
        </div>
      </form>
    </Card>
  );
}

export function AuditRetentionCard({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const [days, setDays] = useState('365');
  useEffect(() => setDays(String(settings.audit_retention_days ?? 365)), [settings]);
  const save = useSaveSettings();
  const bad = !intInRange(days, 30, 3650);
  return (
    <Card title={t('audit.settingsTitle')}>
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (!bad) save.mutate({ audit_retention_days: Number(days) });
        }}
      >
        <Field
          label={t('audit.retention')}
          hint={
            bad ? (
              <span className="text-red-600">{t('audit.retentionErr')}</span>
            ) : (
              t('audit.retentionHint')
            )
          }
          className="w-64"
        >
          <Input
            type="number"
            min={30}
            max={3650}
            value={days}
            onChange={(e) => setDays(e.target.value)}
            dir="ltr"
          />
        </Field>
        <Button type="submit" variant="primary" busy={save.isPending} disabled={bad}>
          {t('common.save')}
        </Button>
        <ErrorBox error={save.error} className="w-full" />
      </form>
    </Card>
  );
}

/** Settings tab "Traps & syslog": switches at the top; settings, status and flapping while on. */
export function IngestTab({ settings }: { settings: Settings }) {
  const f = featuresFromSettings(settings);
  return (
    <div className="space-y-6">
      <FeatureToggleCard names={featuresOfTab('ingest')} settings={settings} />
      <FeatureSection feature={['traps', 'syslog']} settings={settings}>
        <IngestSettingsCard settings={settings} />
      </FeatureSection>
      {ingestOn(f) && <IngestStatusCard />}
      <FeatureSection feature="flapping" settings={settings}>
        <FlappingCard settings={settings} />
      </FeatureSection>
    </div>
  );
}
