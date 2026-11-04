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
  ErrorBox,
  Field,
  Input,
  Loading,
  Notice,
  PageHeader,
  Select,
} from '../components/ui';
import { errorMessage } from '../lib/errors';

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
    <Card title={t('settings.smtp')}>
      <form onSubmit={submit} className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-3">
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

  useEffect(() => {
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
    });
  };

  return (
    <Card title={t('settings.general')}>
      <form onSubmit={submit} className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-3">
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
        </div>
        <ErrorBox error={save.error} />
        <div className="flex justify-end">
          <Button type="submit" variant="primary" busy={save.isPending}>
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
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} />
      {q.data && (
        <>
          <GeneralCard settings={q.data} />
          <SmtpCard settings={q.data} />
        </>
      )}
    </div>
  );
}
