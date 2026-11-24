import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints } from '../../api/endpoints';
import type { EventRule, FeatureName, Settings, SettingsInput } from '../../api/types';
import { endpoints3, qk3 } from '../../api/endpoints3';
import {
  PHASE3_FEATURES,
  type StatusLine,
  featureHints,
  featuresFromSettings,
  featureSettingKey,
  featureStatus,
} from '../../lib/features';
import { useToast } from '../toast';
import { Button, Card, Checkbox, cx, ErrorBox, Modal } from '../ui';
import { FeatureSection, useSaveSettings, Toggle } from './common';
import { useIngestStatus } from './IngestStatus';
import {
  backupStatusLines,
  flowStatusLines,
  kerberosStatusLines,
  newDeviceStatusLines,
  pollerStatusLines,
} from '../../lib/features3';
import { formatDateTime } from '../../lib/format';

function Lines({ lines }: { lines: StatusLine[] }) {
  const { t } = useTranslation();
  return (
    <>
      {lines.map((l, i) => (
        <p
          key={i}
          className={cx(
            'text-xs',
            l.tone === 'ok' && 'text-status-up',
            l.tone === 'error' && 'text-status-down',
            l.tone === 'muted' && 'text-ink-muted',
          )}
        >
          {t(l.key, l.params)}
        </p>
      ))}
    </>
  );
}

const fmtDate = (iso: string) =>
  new Intl.DateTimeFormat(undefined, { dateStyle: 'short' }).format(new Date(iso));
const fmtTime = (iso: string) => {
  const d = new Date(iso);
  const sameDay = new Date().toDateString() === d.toDateString();
  return sameDay
    ? new Intl.DateTimeFormat(undefined, { timeStyle: 'short' }).format(d)
    : formatDateTime(iso);
};

/** [3] Status lines of a phase-3 feature, from its own status endpoint (only while ON). */
function Phase3Lines({ name, on }: { name: FeatureName; on: boolean }) {
  const { t } = useTranslation();
  const flows = useQuery({
    queryKey: qk3.flowStatus,
    queryFn: endpoints3.flowStatus,
    enabled: on && name === 'flows',
    refetchInterval: 15_000,
  });
  const backup = useQuery({
    queryKey: qk3.backupRunner,
    queryFn: endpoints3.backupRunner,
    enabled: on && name === 'config_backup',
    refetchInterval: 30_000,
  });
  const newdev = useQuery({
    queryKey: qk3.newDeviceStatus,
    queryFn: endpoints3.newDeviceStatus,
    enabled: on && name === 'new_device_alerts',
  });
  const krb = useQuery({
    queryKey: qk3.kerberos,
    queryFn: endpoints3.kerberos,
    enabled: on && name === 'sso_kerberos',
  });
  const poll = useQuery({
    queryKey: qk3.telegramStatus,
    queryFn: endpoints3.telegramStatus,
    enabled: on && name === 'chat_ack',
    refetchInterval: 30_000,
  });
  if (!on) return null;
  let lines: StatusLine[];
  switch (name) {
    case 'flows':
      lines = flowStatusLines(flows.data);
      break;
    case 'config_backup':
      lines = backupStatusLines(backup.data, fmtTime);
      break;
    case 'new_device_alerts':
      lines = newDeviceStatusLines(newdev.data, fmtDate);
      break;
    case 'sso_kerberos':
      lines = kerberosStatusLines(krb.data);
      break;
    case 'chat_ack':
      lines = pollerStatusLines(poll.data?.pollers);
      break;
    default:
      lines = [{ key: 'features.status.on', tone: 'ok' }];
  }
  const err = flows.error ?? backup.error ?? newdev.error ?? krb.error ?? poll.error;
  return (
    <>
      <Lines lines={lines} />
      {!!err && <p className="text-xs text-ink-muted">{t('features.status.unknown')}</p>}
    </>
  );
}

/** Dialog after turning Event alerts on: enable the (seeded, disabled) built-in rules in one click. */
function BuiltinRulesDialog({
  rules,
  onClose,
  backup,
}: {
  rules: EventRule[];
  onClose: () => void;
  /** [3] the config-change trigger rules (feature config_backup) */
  backup?: boolean;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const [sel, setSel] = useState<number[]>(() => rules.map((r) => r.id));
  const m = useMutation({
    mutationFn: async () => {
      for (const id of sel) await endpoints.updateEventRule(id, { enabled: true });
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['event-rules'] });
      toast.push('success', t('features.builtinEnabled'));
      onClose();
    },
  });
  return (
    <Modal
      open={rules.length > 0}
      onClose={onClose}
      title={backup ? t('features.backupBuiltinTitle') : t('features.builtinTitle')}
      footer={
        <>
          <Button onClick={onClose}>{t('features.builtinLater')}</Button>
          <Button
            variant="primary"
            busy={m.isPending}
            disabled={!sel.length}
            onClick={() => m.mutate()}
          >
            {t('features.builtinEnable')}
          </Button>
        </>
      }
    >
      <div className="space-y-3" data-testid="builtin-rules-dialog">
        <p className="text-sm">
          {backup ? t('features.backupBuiltinText') : t('features.builtinText')}
        </p>
        <div className="flex flex-col gap-1.5">
          {rules.map((r) => (
            <Checkbox
              key={r.id}
              label={r.name}
              checked={sel.includes(r.id)}
              onChange={(v) => setSel((s) => (v ? [...s, r.id] : s.filter((x) => x !== r.id)))}
            />
          ))}
        </div>
        <ErrorBox error={m.error} />
      </div>
    </Modal>
  );
}

export function FeatureRow({
  name,
  settings,
  onToggle,
  busy,
  error,
  disabled,
}: {
  name: FeatureName;
  settings: Settings;
  onToggle: (name: FeatureName, v: boolean) => void;
  busy: boolean;
  error: unknown;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  const f = featuresFromSettings(settings);
  const ingest = useIngestStatus(f.traps || f.syslog ? 15_000 : 0);
  const lines = featureStatus(name, f, ingest.data, settings);
  const hints = featureHints(name, f, settings);
  return (
    <li className="flex items-start gap-3 px-4 py-3" data-feature={name}>
      <div className="pt-0.5">
        <Toggle
          checked={f[name]}
          onChange={(v) => onToggle(name, v)}
          label={t(`features.name.${name}`)}
          busy={busy}
          disabled={disabled}
        />
      </div>
      <div className="min-w-0 flex-1 space-y-0.5">
        <div className="flex flex-wrap items-baseline gap-2">
          <span className="font-medium">{t(`features.name.${name}`)}</span>
          <span className={cx('text-xs', f[name] ? 'text-status-up' : 'text-ink-muted')}>
            {f[name] ? t('features.on') : t('features.off')}
          </span>
        </div>
        <p className="text-sm text-ink-muted">{t(`features.desc.${name}`)}</p>
        {PHASE3_FEATURES.includes(name) ? (
          <Phase3Lines name={name} on={f[name]} />
        ) : (
          <Lines lines={lines} />
        )}
        {hints.map((h) => (
          <p
            key={h.key}
            className={cx('text-xs', h.tone === 'warn' ? 'text-status-warning' : 'text-ink-muted')}
          >
            {t(h.key)}
          </p>
        ))}
        {!!error && <ErrorBox error={error} className="mt-1" />}
      </div>
    </li>
  );
}

/**
 * Toggling features: PUT /api/settings with one feature_*_enabled key; per-feature busy and error
 * (e.g. the 422 of turning AD on with incomplete settings, shown in place) and the follow-up dialogs
 * ("enable the built-in rules?" for Event alerts, "…config-change triggers?" for Config backup).
 */
export function useFeatureToggle() {
  const [pending, setPending] = useState<FeatureName | null>(null);
  const [errors, setErrors] = useState<Partial<Record<FeatureName, unknown>>>({});
  const [builtins, setBuiltins] = useState<EventRule[]>([]);
  const [backupRules, setBackupRules] = useState<EventRule[]>([]);
  const save = useSaveSettings({ toast: false });
  const toggle = (name: FeatureName, v: boolean) => {
    setPending(name);
    setErrors((e) => ({ ...e, [name]: null }));
    save.mutate({ [featureSettingKey(name)]: v } as SettingsInput, {
      onSuccess: async (s) => {
        if (name === 'event_alerts' && v && (s.feature_traps_enabled || s.feature_syslog_enabled)) {
          try {
            const r = await endpoints.eventRules();
            const b = r.items.filter((x) => x.builtin && x.action !== 'config_backup');
            if (b.length && b.every((x) => !x.enabled)) setBuiltins(b);
          } catch {
            /* the dialog is a convenience */
          }
        }
        // [3] "Enable the built-in config-change triggers?" (seeded disabled)
        if (
          name === 'config_backup' &&
          v &&
          (s.feature_traps_enabled || s.feature_syslog_enabled)
        ) {
          try {
            const r = await endpoints.eventRules();
            const b = r.items.filter((x) => x.builtin && x.action === 'config_backup');
            if (b.length && b.every((x) => !x.enabled)) setBackupRules(b);
          } catch {
            /* the dialog is a convenience */
          }
        }
      },
      onError: (e) => setErrors((x) => ({ ...x, [name]: e })),
      onSettled: () => setPending(null),
    });
  };
  const dialogs = (
    <>
      <BuiltinRulesDialog rules={builtins} onClose={() => setBuiltins([])} />
      <BuiltinRulesDialog rules={backupRules} onClose={() => setBackupRules([])} backup />
    </>
  );
  return { toggle, pending, errors, dialogs };
}

/** The toggle rows of the given features (with their dialogs), without a card. */
export function FeatureToggleList({
  names,
  settings,
}: {
  names: FeatureName[];
  settings: Settings;
}) {
  const ft = useFeatureToggle();
  return (
    <>
      <ul className="divide-y divide-line">
        {names.map((n) => (
          <FeatureRow
            key={n}
            name={n}
            settings={settings}
            onToggle={ft.toggle}
            busy={ft.pending === n}
            error={ft.errors[n]}
          />
        ))}
      </ul>
      {ft.dialogs}
    </>
  );
}

/**
 * The on/off header card at the TOP of a feature's own settings tab (owner decision): the feature
 * switch(es) with description and live status line ("Listening on UDP 162" / "Off"). The settings
 * below are shown only while the feature is on.
 */
export function FeatureToggleCard({
  names,
  settings,
}: {
  names: FeatureName[];
  settings: Settings;
}) {
  const { t } = useTranslation();
  return (
    <Card
      title={names.length > 1 ? t('features.toggleMany') : t('features.toggleOne')}
      bodyClassName="p-0"
    >
      <div data-testid="feature-toggles">
        <FeatureToggleList names={names} settings={settings} />
      </div>
    </Card>
  );
}

/**
 * The switch of a configure-first feature (Active Directory, …) inside its settings form, next to
 * "Test connection": `blocked` keeps it disabled (with `hint`) until the stored settings passed a
 * test. Turning it off is always possible.
 */
export function InlineFeatureSwitch({
  name,
  settings,
  blocked,
  hint,
}: {
  name: FeatureName;
  settings: Settings;
  blocked: boolean;
  hint?: string;
}) {
  const { t } = useTranslation();
  const ft = useFeatureToggle();
  const f = featuresFromSettings(settings);
  const on = f[name];
  const lines = featureStatus(name, f, null, settings);
  return (
    <div className="space-y-0.5" data-testid="feature-toggles" data-feature={name}>
      <div className="flex flex-wrap items-center gap-2">
        <Toggle
          checked={on}
          onChange={(v) => ft.toggle(name, v)}
          label={t(`features.name.${name}`)}
          busy={ft.pending === name}
          disabled={!on && blocked}
        />
        <span className="text-sm font-medium">{t(`features.name.${name}`)}</span>
        <span className={cx('text-xs', on ? 'text-status-up' : 'text-ink-muted')}>
          {on ? t('features.on') : t('features.off')}
        </span>
      </div>
      {PHASE3_FEATURES.includes(name) ? (
        <Phase3Lines name={name} on={on} />
      ) : (
        on && <Lines lines={lines} />
      )}
      {!on && blocked && hint && (
        <p className="text-xs text-status-warning" data-testid="enable-hint">
          {hint}
        </p>
      )}
      {!!ft.errors[name] && <ErrorBox error={ft.errors[name]} className="mt-1" />}
      {ft.dialogs}
    </div>
  );
}

/** A feature's own settings tab: the switch card at the top, its settings below only while ON. */
export function FeatureTab({
  feature,
  settings,
  children,
}: {
  feature: FeatureName;
  settings: Settings;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-6">
      <FeatureToggleCard names={[feature]} settings={settings} />
      <FeatureSection feature={feature} settings={settings}>
        {children}
      </FeatureSection>
    </div>
  );
}
