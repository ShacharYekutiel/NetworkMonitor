import { useEffect, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { Language, Settings, SettingsInput, SmtpSecurity } from '../api/types';
import { SecretField } from '../components/SnmpEditor';
import { useToast } from '../components/toast';
import {
  Button,
  Card,
  Checkbox,
  ErrorBox,
  Field,
  Input,
  Loading,
  Notice,
  PageHeader,
  Select,
} from '../components/ui';
import { errorMessage } from '../lib/errors';
import { timeZones } from '../lib/maintenance';
import { portSettingsErrors } from '../lib/ports';
import { useSearchParams } from 'react-router-dom';
import { FeaturesTab } from '../components/features/FeaturesTab';
import { AuditRetentionCard, IngestTab } from '../components/features/IngestSettings';
import { LdapTab } from '../components/features/LdapSettings';
import { SiemTab } from '../components/features/SiemSettings';
import { FeatureSection } from '../components/features/common';
import { FeatureToggleCard } from '../components/features/FeatureToggle';
import { ALL_OFF, featuresFromSettings, SETTINGS_TABS, type SettingsTab } from '../lib/features';
import { SettingsSearch } from '../components/SettingsSearch';
import { TabBar } from '../components/TabBar';
import {
  BackupSettingsTab,
  ChatAckSettingsTab,
  FlowSettingsTab,
  NewDeviceSettingsTab,
  ReportSettingsTab,
} from '../components/features/Phase3Settings';
import { SecurityCard } from '../components/settings/SecuritySettings';
import { SsoTab } from '../components/features/SsoSettings';
import { NocTab } from '../components/features/NocSettings';
import { cardAnchor } from '../lib/settingsIndex';
import { AssistantSettingsTab } from '../components/assistant/AssistantSettingsTab';
import { AwsTab } from '../components/aws/AwsTab';

function SmtpCard({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const s = settings.smtp;
  const [host, setHost] = useState(s.host);
  const [port, setPort] = useState(String(s.port));
  const [security, setSecurity] = useState<SmtpSecurity>(s.security);
  const [username, setUsername] = useState(s.username ?? '');
  const [password, setPassword] = useState('');
  const [clearPw, setClearPw] = useState(false);
  const [fromAddress, setFromAddress] = useState(s.from_address);
  const [fromName, setFromName] = useState(s.from_name);
  const [testTo, setTestTo] = useState('');
  const [testMsg, setTestMsg] = useState<string | null>(null);

  useEffect(() => {
    setHost(s.host);
    setPort(String(s.port));
    setSecurity(s.security);
    setUsername(s.username ?? '');
    setFromAddress(s.from_address);
    setFromName(s.from_name);
    setPassword('');
    setClearPw(false);
  }, [s]);

  const save = useMutation({
    mutationFn: (body: SettingsInput) => endpoints.updateSettings(body),
    onSuccess: (r) => {
      qc.setQueryData(qk.settings, r);
      toast.push('success', t('common.saved'));
    },
  });
  const test = useMutation({
    mutationFn: () => endpoints.testSmtp(testTo.trim()),
    onSuccess: (r) => setTestMsg(r.ok ? t('settings.smtpTestOk') : r.message),
    onError: () => setTestMsg(null),
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const smtp: NonNullable<SettingsInput['smtp']> = {
      host: host.trim(),
      port: Number(port) || 587,
      security,
      username: username.trim() || null,
      from_address: fromAddress.trim(),
      from_name: fromName,
    };
    if (clearPw) smtp.password = '';
    else if (password) smtp.password = password;
    save.mutate({ smtp });
  };

  return (
    <Card title={t('settings.smtp')} anchor={cardAnchor('smtp')}>
      <form onSubmit={submit} className="space-y-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label={t('settings.smtpHost')} className="sm:col-span-2">
            <Input value={host} onChange={(e) => setHost(e.target.value)} dir="ltr" />
          </Field>
          <Field label={t('settings.smtpPort')}>
            <Input
              type="number"
              min={1}
              max={65535}
              value={port}
              onChange={(e) => setPort(e.target.value)}
              dir="ltr"
            />
          </Field>
          <Field label={t('settings.smtpSecurity')}>
            <Select value={security} onChange={(e) => setSecurity(e.target.value as SmtpSecurity)}>
              <option value="none">{t('settings.security.none')}</option>
              <option value="starttls">STARTTLS</option>
              <option value="ssl">SSL/TLS</option>
            </Select>
          </Field>
          <Field label={t('settings.smtpUsername')}>
            <Input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              dir="ltr"
              autoComplete="off"
            />
          </Field>
          <SecretField
            label={t('settings.smtpPassword')}
            value={password}
            isSet={s.password_set}
            clear={clearPw}
            onValue={setPassword}
            onClear={setClearPw}
          />
          <Field label={t('settings.fromAddress')} className="sm:col-span-2">
            <Input
              type="email"
              value={fromAddress}
              onChange={(e) => setFromAddress(e.target.value)}
              dir="ltr"
            />
          </Field>
          <Field label={t('settings.fromName')}>
            <Input value={fromName} onChange={(e) => setFromName(e.target.value)} />
          </Field>
        </div>
        <ErrorBox error={save.error} />
        <div className="flex justify-end">
          <Button type="submit" variant="primary" busy={save.isPending}>
            {t('common.save')}
          </Button>
        </div>
      </form>
      <div className="mt-4 border-t border-slate-200 pt-4 dark:border-slate-800">
        <div className="flex flex-wrap items-end gap-2">
          <Field label={t('settings.smtpTestTo')} className="min-w-64 flex-1">
            <Input
              type="email"
              value={testTo}
              onChange={(e) => setTestTo(e.target.value)}
              dir="ltr"
            />
          </Field>
          <Button
            disabled={!testTo.trim()}
            busy={test.isPending}
            onClick={() => {
              setTestMsg(null);
              test.mutate();
            }}
          >
            {t('settings.smtpTest')}
          </Button>
        </div>
        <p className="mt-1 text-xs text-slate-500">{t('settings.smtpTestHint')}</p>
        {testMsg && (
          <div className="mt-2">
            <Notice tone="success">{testMsg}</Notice>
          </div>
        )}
        {test.error && <ErrorBox className="mt-2" error={new Error(errorMessage(t, test.error))} />}
      </div>
    </Card>
  );
}

function GeneralCard({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const [retention, setRetention] = useState(String(settings.retention_days));
  const [eventRetention, setEventRetention] = useState(String(settings.event_retention_days));
  const [interval, setInterval] = useState(String(settings.default_interval_s));
  const [lang, setLang] = useState<Language>(settings.default_language);
  const [baseUrl, setBaseUrl] = useState(settings.public_base_url ?? '');
  const [tz, setTz] = useState(settings.timezone ?? 'UTC');
  const [topoMin, setTopoMin] = useState(String(settings.topology_refresh_min ?? 30));
  const zones = timeZones(settings.timezone);
  const topoN = Number(topoMin);
  const topoOk = /^\d+$/.test(topoMin.trim()) && (topoN === 0 || (topoN >= 5 && topoN <= 1440));

  useEffect(() => {
    setTz(settings.timezone ?? 'UTC');
    setTopoMin(String(settings.topology_refresh_min ?? 30));
    setRetention(String(settings.retention_days));
    setEventRetention(String(settings.event_retention_days));
    setInterval(String(settings.default_interval_s));
    setLang(settings.default_language);
    setBaseUrl(settings.public_base_url ?? '');
  }, [settings]);

  const save = useMutation({
    mutationFn: (body: SettingsInput) => endpoints.updateSettings(body),
    onSuccess: (r) => {
      qc.setQueryData(qk.settings, r);
      void qc.invalidateQueries({ queryKey: qk.publicSettings });
      toast.push('success', t('common.saved'));
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate({
      retention_days: Number(retention),
      event_retention_days: Number(eventRetention),
      default_interval_s: Number(interval),
      default_language: lang,
      public_base_url: baseUrl.trim() || null,
      timezone: tz,
      topology_refresh_min: topoN,
    });
  };

  return (
    <Card title={t('settings.general')} anchor={cardAnchor('general')}>
      <form onSubmit={submit} className="space-y-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label={t('settings.retentionDays')} hint={t('settings.retentionHint')}>
            <Input
              type="number"
              min={1}
              max={3650}
              value={retention}
              onChange={(e) => setRetention(e.target.value)}
              dir="ltr"
              required
            />
          </Field>
          <Field label={t('settings.eventRetentionDays')}>
            <Input
              type="number"
              min={1}
              max={3650}
              value={eventRetention}
              onChange={(e) => setEventRetention(e.target.value)}
              dir="ltr"
              required
            />
          </Field>
          <Field label={t('settings.defaultInterval')} hint={t('settings.defaultIntervalHint')}>
            <Input
              type="number"
              min={10}
              max={86400}
              value={interval}
              onChange={(e) => setInterval(e.target.value)}
              dir="ltr"
              required
            />
          </Field>
          <Field label={t('settings.defaultLanguage')} hint={t('settings.defaultLanguageHint')}>
            <Select value={lang} onChange={(e) => setLang(e.target.value as Language)}>
              <option value="en">English</option>
              <option value="he">עברית</option>
            </Select>
          </Field>
          <Field
            label={t('settings.publicBaseUrl')}
            hint={t('settings.publicBaseUrlHint')}
            className="sm:col-span-2"
          >
            <Input
              type="url"
              value={baseUrl}
              placeholder="http://netmon.local:8080"
              onChange={(e) => setBaseUrl(e.target.value)}
              dir="ltr"
            />
          </Field>
          <Field label={t('settings.timezone')} hint={t('settings.timezoneHint')}>
            <Select value={tz} onChange={(e) => setTz(e.target.value)} dir="ltr">
              {zones.map((z) => (
                <option key={z} value={z}>
                  {z}
                </option>
              ))}
            </Select>
          </Field>
          <Field
            label={t('settings.topologyRefresh')}
            hint={
              topoOk ? (
                t('settings.topologyRefreshHint')
              ) : (
                <span className="text-red-600">{t('settings.topologyRefreshErr')}</span>
              )
            }
          >
            <Input
              type="number"
              min={0}
              max={1440}
              value={topoMin}
              onChange={(e) => setTopoMin(e.target.value)}
              dir="ltr"
              aria-invalid={!topoOk}
            />
          </Field>
        </div>
        <ErrorBox error={save.error} />
        <div className="flex justify-end">
          <Button type="submit" variant="primary" busy={save.isPending} disabled={!topoOk}>
            {t('common.save')}
          </Button>
        </div>
      </form>
    </Card>
  );
}

function PortsCard({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const [interval, setInterval] = useState(String(settings.port_inventory_min ?? 10));
  const [threshold, setThreshold] = useState(String(settings.uplink_mac_threshold ?? 5));
  const [retention, setRetention] = useState(String(settings.port_mac_retention_days ?? 30));
  const [rdns, setRdns] = useState(settings.reverse_dns ?? true);
  useEffect(() => {
    setInterval(String(settings.port_inventory_min ?? 10));
    setThreshold(String(settings.uplink_mac_threshold ?? 5));
    setRetention(String(settings.port_mac_retention_days ?? 30));
    setRdns(settings.reverse_dns ?? true);
  }, [settings]);
  const err = portSettingsErrors({ interval, threshold, retention });
  const invalid = err.interval || err.threshold || err.retention;
  const save = useMutation({
    mutationFn: (body: SettingsInput) => endpoints.updateSettings(body),
    onSuccess: (r) => {
      qc.setQueryData(qk.settings, r);
      void qc.invalidateQueries({ queryKey: ['device-ports'] });
      toast.push('success', t('common.saved'));
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (invalid) return;
    save.mutate({
      port_inventory_min: Number(interval),
      uplink_mac_threshold: Number(threshold),
      port_mac_retention_days: Number(retention),
      reverse_dns: rdns,
    });
  };
  const hint = (bad: boolean, ok: string, errKey: string) =>
    bad ? <span className="text-red-600">{t(errKey)}</span> : ok;
  return (
    <Card title={t('settings.ports.title')} anchor={cardAnchor('ports')}>
      <form onSubmit={submit} className="space-y-4" data-testid="ports-settings">
        <p className="text-xs text-slate-500">{t('settings.ports.intro')}</p>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field
            label={t('settings.ports.interval')}
            hint={hint(
              err.interval,
              t('settings.ports.intervalHint'),
              'settings.ports.intervalErr',
            )}
          >
            <Input
              type="number"
              min={0}
              max={1440}
              value={interval}
              onChange={(e) => setInterval(e.target.value)}
              dir="ltr"
              aria-invalid={err.interval}
            />
          </Field>
          <Field
            label={t('settings.ports.threshold')}
            hint={hint(
              err.threshold,
              t('settings.ports.thresholdHint'),
              'settings.ports.thresholdErr',
            )}
          >
            <Input
              type="number"
              min={1}
              max={1000}
              value={threshold}
              onChange={(e) => setThreshold(e.target.value)}
              dir="ltr"
              aria-invalid={err.threshold}
            />
          </Field>
          <Field
            label={t('settings.ports.retention')}
            hint={hint(
              err.retention,
              t('settings.ports.retentionHint'),
              'settings.ports.retentionErr',
            )}
          >
            <Input
              type="number"
              min={1}
              max={365}
              value={retention}
              onChange={(e) => setRetention(e.target.value)}
              dir="ltr"
              aria-invalid={err.retention}
            />
          </Field>
        </div>
        <div>
          <Checkbox label={t('settings.ports.reverseDns')} checked={rdns} onChange={setRdns} />
          <p className="ms-6 mt-0.5 text-xs text-slate-500">{t('settings.ports.reverseDnsHint')}</p>
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

export function SettingsPage() {
  const { t } = useTranslation();
  const q = useQuery({ queryKey: qk.settings, queryFn: endpoints.settings });
  const health = useQuery({ queryKey: ['health'], queryFn: endpoints.health });
  const [params, setParams] = useSearchParams();
  const p = params.get('tab') as SettingsTab | null;
  const tab: SettingsTab = p && SETTINGS_TABS.includes(p) ? p : 'general';
  const openTab = (k: SettingsTab) =>
    setParams(k === 'general' ? {} : { tab: k }, { replace: true });
  return (
    <div className="space-y-6">
      <PageHeader
        title={t('settings.title')}
        subtitle={
          health.data &&
          t('settings.healthLine', {
            version: health.data.version,
            db: health.data.database,
            mode: health.data.timescale ? 'TimescaleDB' : 'PostgreSQL',
            poller: health.data.poller,
            count: health.data.sensors_scheduled,
          })
        }
      />
      <SettingsSearch features={q.data ? featuresFromSettings(q.data) : ALL_OFF} onTab={openTab} />
      <TabBar
        tabs={[...SETTINGS_TABS]}
        value={tab}
        label={t('settings.title')}
        onChange={openTab}
        render={(k) => t(`settings.tab.${k}`)}
      />
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} />
      {q.data && tab === 'general' && (
        <>
          <GeneralCard settings={q.data} />
          <PortsCard settings={q.data} />
          <SmtpCard settings={q.data} />
          <SecurityCard settings={q.data} />
        </>
      )}
      {q.data && tab === 'features' && <FeaturesTab settings={q.data} />}
      {q.data && tab === 'ingest' && <IngestTab settings={q.data} />}
      {q.data && tab === 'siem' && <SiemTab settings={q.data} />}
      {q.data && tab === 'ldap' && <LdapTab settings={q.data} />}
      {q.data && tab === 'sso' && <SsoTab settings={q.data} />}
      {q.data && tab === 'backup' && <BackupSettingsTab settings={q.data} />}
      {q.data && tab === 'flows' && <FlowSettingsTab settings={q.data} />}
      {q.data && tab === 'newDevices' && <NewDeviceSettingsTab settings={q.data} />}
      {q.data && tab === 'reports' && <ReportSettingsTab settings={q.data} />}
      {q.data && tab === 'noc' && <NocTab settings={q.data} />}
      {q.data && tab === 'chatAck' && <ChatAckSettingsTab settings={q.data} />}
      {q.data && tab === 'aws' && <AwsTab settings={q.data} />}
      {q.data && tab === 'assistant' && <AssistantSettingsTab settings={q.data} />}
      {q.data && tab === 'audit' && (
        <>
          <FeatureToggleCard names={['audit']} settings={q.data} />
          <FeatureSection feature="audit" settings={q.data}>
            <AuditRetentionCard settings={q.data} />
          </FeatureSection>
        </>
      )}
    </div>
  );
}
