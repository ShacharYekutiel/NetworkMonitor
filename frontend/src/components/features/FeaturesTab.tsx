import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints } from '../../api/endpoints';
import type { EventRule, FeatureName, Settings, SettingsInput } from '../../api/types';
import { FEATURE_NAMES } from '../../api/types';
import {
  featureHints,
  featuresFromSettings,
  featureSettingKey,
  featureStatus,
} from '../../lib/features';
import { useToast } from '../toast';
import { Button, Card, Checkbox, cx, ErrorBox, Modal } from '../ui';
import { useSaveSettings, Toggle } from './common';
import { useIngestStatus } from './IngestStatus';

/** Dialog after turning Event alerts on: enable the (seeded, disabled) built-in rules in one click. */
function BuiltinRulesDialog({ rules, onClose }: { rules: EventRule[]; onClose: () => void }) {
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
      title={t('features.builtinTitle')}
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
        <p className="text-sm">{t('features.builtinText')}</p>
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

function FeatureRow({
  name,
  settings,
  onToggle,
  busy,
  error,
}: {
  name: FeatureName;
  settings: Settings;
  onToggle: (name: FeatureName, v: boolean) => void;
  busy: boolean;
  error: unknown;
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
        />
      </div>
      <div className="min-w-0 flex-1 space-y-0.5">
        <div className="flex flex-wrap items-baseline gap-2">
          <span className="font-medium">{t(`features.name.${name}`)}</span>
          <span
            className={cx(
              'text-xs',
              f[name] ? 'text-green-700 dark:text-green-400' : 'text-slate-500',
            )}
          >
            {f[name] ? t('features.on') : t('features.off')}
          </span>
        </div>
        <p className="text-sm text-slate-600 dark:text-slate-300">{t(`features.desc.${name}`)}</p>
        {lines.map((l, i) => (
          <p
            key={i}
            className={cx(
              'text-xs',
              l.tone === 'ok' && 'text-green-700 dark:text-green-400',
              l.tone === 'error' && 'text-red-600 dark:text-red-400',
              l.tone === 'muted' && 'text-slate-500',
            )}
          >
            {t(l.key, l.params)}
          </p>
        ))}
        {hints.map((h) => (
          <p
            key={h.key}
            className={cx(
              'text-xs',
              h.tone === 'warn' ? 'text-amber-700 dark:text-amber-300' : 'text-slate-500',
            )}
          >
            {t(h.key)}
          </p>
        ))}
        {!!error && <ErrorBox error={error} className="mt-1" />}
      </div>
    </li>
  );
}

/** [2b-3] Settings → Features: one row per feature (all OFF by default). */
export function FeaturesTab({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const [pending, setPending] = useState<FeatureName | null>(null);
  const [errors, setErrors] = useState<Partial<Record<FeatureName, unknown>>>({});
  const [builtins, setBuiltins] = useState<EventRule[]>([]);
  const save = useSaveSettings({ toast: false });
  const toggle = (name: FeatureName, v: boolean) => {
    setPending(name);
    setErrors((e) => ({ ...e, [name]: null }));
    save.mutate({ [featureSettingKey(name)]: v } as SettingsInput, {
      onSuccess: async (s) => {
        if (name === 'event_alerts' && v && (s.feature_traps_enabled || s.feature_syslog_enabled)) {
          try {
            const r = await endpoints.eventRules();
            const b = r.items.filter((x) => x.builtin);
            if (b.length && b.every((x) => !x.enabled)) setBuiltins(b);
          } catch {
            /* the dialog is a convenience */
          }
        }
      },
      onError: (e) => setErrors((x) => ({ ...x, [name]: e })),
      onSettled: () => setPending(null),
    });
  };
  return (
    <Card title={t('features.title')} bodyClassName="p-0">
      <p className="border-b border-slate-200 px-4 py-2 text-xs text-slate-500 dark:border-slate-800">
        {t('features.intro')}
      </p>
      <ul className="divide-y divide-slate-100 dark:divide-slate-800" data-testid="features-list">
        {FEATURE_NAMES.map((n) => (
          <FeatureRow
            key={n}
            name={n}
            settings={settings}
            onToggle={toggle}
            busy={pending === n}
            error={errors[n]}
          />
        ))}
      </ul>
      <BuiltinRulesDialog rules={builtins} onClose={() => setBuiltins([])} />
    </Card>
  );
}
