import { useEffect, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints } from '../../api/endpoints';
import type { EventRuleKind, Settings, SiemProtocol, SiemTarget } from '../../api/types';
import { useFeatures } from '../../hooks/features';
import { DEFAULT_PORT, MAX_TARGETS, newTarget, validateTarget } from '../../lib/siem';
import { EditIcon, PlusIcon, TrashIcon } from '../icons';
import { SeveritySelect } from '../logs/common';
import {
  Button,
  Card,
  Checkbox,
  ConfirmDialog,
  cx,
  Empty,
  ErrorBox,
  Field,
  FieldGroup,
  Input,
  Modal,
  Notice,
  Select,
  Table,
  Textarea,
  td,
  th,
} from '../ui';
import { FeatureSection, useSaveSettings } from './common';
import { useIngestStatus } from './IngestStatus';

function TestButton({
  body,
  disabled,
}: {
  body: () => { target_id: string } | { target: SiemTarget };
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  const m = useMutation({ mutationFn: () => endpoints.testForwarder(body()) });
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <Button size="sm" busy={m.isPending} disabled={disabled} onClick={() => m.mutate()}>
        {t('siem.test')}
      </Button>
      {m.data && (
        <span className="text-xs text-green-700 dark:text-green-400">{t('siem.testOk')}</span>
      )}
      {!!m.error && <ErrorBox error={m.error} className="!py-1 text-xs" />}
    </span>
  );
}

function TargetModal({
  target,
  onClose,
  onSave,
}: {
  target: SiemTarget | null;
  onClose: () => void;
  onSave: (t: SiemTarget) => void;
}) {
  const { t } = useTranslation();
  const { features } = useFeatures();
  const [v, setV] = useState<SiemTarget>(newTarget());
  const [port, setPort] = useState('6514');
  const [sources, setSources] = useState('');
  const [touched, setTouched] = useState(false);
  useEffect(() => {
    if (!target) return;
    setV(target);
    setPort(String(target.port));
    setSources(target.sources.join('\n'));
    setTouched(false);
  }, [target]);
  const set = (p: Partial<SiemTarget>) => setV((x) => ({ ...x, ...p }));
  const full = (): SiemTarget => ({
    ...v,
    name: v.name.trim(),
    host: v.host.trim(),
    port: Number(port),
    sources: sources.split(/[\s,;]+/).filter(Boolean),
  });
  const errs = validateTarget(v, sources, port);
  const shown = touched ? errs : {};
  const invalid = Object.keys(errs).length > 0;
  const kinds: EventRuleKind[] = ['syslog', 'trap'];
  const hint = (k: keyof typeof shown, ok?: string) =>
    shown[k] ? <span className="text-red-600">{t(shown[k] as string)}</span> : ok;
  return (
    <Modal
      open={!!target}
      onClose={onClose}
      wide
      title={target?.id ? t('siem.edit') : t('siem.add')}
    >
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          setTouched(true);
          if (!invalid) onSave(full());
        }}
      >
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-4">
          <Field label={t('common.name')} required className="sm:col-span-2" hint={hint('name')}>
            <Input value={v.name} onChange={(e) => set({ name: e.target.value })} maxLength={64} />
          </Field>
          <Field label={t('siem.protocol')}>
            <Select
              value={v.protocol}
              onChange={(e) => {
                const p = e.target.value as SiemProtocol;
                if (port === String(DEFAULT_PORT[v.protocol])) setPort(String(DEFAULT_PORT[p]));
                set({ protocol: p });
              }}
            >
              <option value="udp">UDP</option>
              <option value="tcp">TCP</option>
              <option value="tls">TLS</option>
            </Select>
          </Field>
          <Field label={t('siem.port')} hint={hint('port')}>
            <Input
              type="number"
              min={1}
              max={65535}
              value={port}
              onChange={(e) => setPort(e.target.value)}
              dir="ltr"
            />
          </Field>
          <Field label={t('siem.host')} required className="sm:col-span-2" hint={hint('host')}>
            <Input
              value={v.host}
              onChange={(e) => set({ host: e.target.value })}
              dir="ltr"
              placeholder="siem.example.com"
            />
          </Field>
          <FieldGroup label={t('siem.kinds')} hint={hint('kinds')} className="sm:col-span-2">
            <div className="flex gap-4 pt-1">
              {kinds.map((k) => (
                <Checkbox
                  key={k}
                  label={t(`logs.tab.${k === 'trap' ? 'traps' : 'syslog'}`)}
                  checked={v.kinds.includes(k)}
                  onChange={(c) =>
                    set({ kinds: c ? [...v.kinds, k] : v.kinds.filter((x) => x !== k) })
                  }
                />
              ))}
            </div>
          </FieldGroup>
          <Field
            label={t('siem.minSeverity')}
            hint={t('siem.minSeverityHint')}
            className="sm:col-span-2"
          >
            <SeveritySelect
              value={v.min_severity}
              onChange={(s) => set({ min_severity: s ?? 7 })}
              label={t('siem.minSeverity')}
              anyLabel={t('logs.anySeverity')}
            />
          </Field>
          <Field
            label={t('siem.sources')}
            hint={hint('sources', t('siem.sourcesHint'))}
            className="sm:col-span-2"
          >
            <Textarea
              rows={2}
              value={sources}
              onChange={(e) => setSources(e.target.value)}
              dir="ltr"
              className="font-mono"
            />
          </Field>
        </div>
        <div className="flex flex-wrap gap-4">
          {v.protocol === 'tls' && (
            <Checkbox
              label={t('siem.tlsVerify')}
              checked={v.tls_verify}
              onChange={(c) => set({ tls_verify: c })}
            />
          )}
          <Checkbox
            label={t('siem.includeUnknown')}
            checked={v.include_unknown}
            onChange={(c) => set({ include_unknown: c })}
          />
          <Checkbox
            label={t('common.enabled')}
            checked={v.enabled}
            onChange={(c) => set({ enabled: c })}
          />
        </div>
        {v.protocol === 'tls' && !v.tls_verify && <Notice>{t('siem.tlsVerifyOff')}</Notice>}
        {v.protocol === 'udp' && <p className="text-xs text-slate-500">{t('siem.udpHint')}</p>}
        <div className="flex flex-wrap items-center justify-end gap-2">
          <span className="me-auto">
            <TestButton
              body={() => ({ target: full() })}
              disabled={invalid || !features.siem_forwarding}
            />
          </span>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="submit" variant="primary">
            {t('siem.apply')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

const FW_CLS: Record<string, string> = {
  connected: 'text-green-700 dark:text-green-400',
  connecting: 'text-amber-700 dark:text-amber-300',
  idle: 'text-slate-500',
  error: 'text-red-600 dark:text-red-400',
  disabled: 'text-slate-400',
};

export function SiemCard({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const { features } = useFeatures();
  const ingest = useIngestStatus(15_000);
  const targets = settings.siem_targets ?? [];
  const [editing, setEditing] = useState<{ t: SiemTarget; index: number } | null>(null);
  const [deleting, setDeleting] = useState<number | null>(null);
  const save = useSaveSettings();
  const put = (list: SiemTarget[], done?: () => void) =>
    save.mutate({ siem_targets: list }, { onSuccess: () => done?.() });
  const fw = (id?: string) => ingest.data?.forwarders.find((f) => f.id === id);
  return (
    <Card
      title={t('siem.title')}
      actions={
        <Button
          size="sm"
          variant="primary"
          disabled={targets.length >= MAX_TARGETS}
          onClick={() => setEditing({ t: newTarget(), index: -1 })}
        >
          <PlusIcon size={12} /> {t('siem.add')}
        </Button>
      }
      bodyClassName="p-0"
    >
      <p className="px-4 py-2 text-xs text-slate-500">{t('siem.intro')}</p>
      {!targets.length ? (
        <Empty>{t('siem.none')}</Empty>
      ) : (
        <Table>
          <thead>
            <tr>
              <th className={th}>{t('common.name')}</th>
              <th className={th}>{t('siem.destination')}</th>
              <th className={th}>{t('siem.kinds')}</th>
              <th className={th}>{t('siem.state')}</th>
              <th className={th}>{t('siem.counters')}</th>
              <th className={th} />
            </tr>
          </thead>
          <tbody>
            {targets.map((x, i) => {
              const s = fw(x.id);
              return (
                <tr key={x.id ?? i} className={x.enabled ? undefined : 'opacity-60'}>
                  <td className={`${td} font-medium`}>{x.name}</td>
                  <td className={td}>
                    <bdi dir="ltr">
                      {x.protocol.toUpperCase()} {x.host}:{x.port}
                    </bdi>
                    {x.protocol === 'tls' && !x.tls_verify && (
                      <span className="ms-1 text-xs text-amber-700 dark:text-amber-300">
                        ({t('siem.noVerify')})
                      </span>
                    )}
                  </td>
                  <td className={td}>
                    {x.kinds
                      .map((k) => t(`logs.tab.${k === 'trap' ? 'traps' : 'syslog'}`))
                      .join(', ')}
                  </td>
                  <td className={cx(td, s ? FW_CLS[s.state] : 'text-slate-400')}>
                    {s ? t(`siem.fw.${s.state}`) : '—'}
                    {s?.last_error && (
                      <div className="text-xs">
                        <bdi dir="ltr">{s.last_error}</bdi>
                      </div>
                    )}
                  </td>
                  <td className={`${td} whitespace-nowrap text-xs tabular-nums`}>
                    {s
                      ? t('siem.countersLine', {
                          sent: s.sent,
                          dropped: s.dropped,
                          errors: s.errors,
                          queue: s.queue,
                        })
                      : '—'}
                  </td>
                  <td className={`${td} whitespace-nowrap text-end`}>
                    {x.id && (
                      <TestButton
                        body={() => ({ target_id: x.id as string })}
                        disabled={!features.siem_forwarding}
                      />
                    )}
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label={t('common.edit')}
                      onClick={() => setEditing({ t: x, index: i })}
                    >
                      <EditIcon size={14} />
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label={t('common.delete')}
                      onClick={() => setDeleting(i)}
                    >
                      <TrashIcon size={14} />
                    </Button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      )}
      <ErrorBox error={save.error} className="m-3" />
      <TargetModal
        target={editing?.t ?? null}
        onClose={() => setEditing(null)}
        onSave={(v) => {
          const idx = editing?.index ?? -1;
          const list =
            idx < 0 ? [...targets, v] : targets.map((x, i) => (i === idx ? { ...v, id: x.id } : x));
          put(list, () => setEditing(null));
        }}
      />
      <ConfirmDialog
        open={deleting !== null}
        title={t('siem.deleteTitle')}
        message={t('common.deleteConfirm', {
          name: deleting !== null ? targets[deleting]?.name : '',
        })}
        onConfirm={() =>
          deleting !== null &&
          put(
            targets.filter((_, i) => i !== deleting),
            () => setDeleting(null),
          )
        }
        onClose={() => setDeleting(null)}
        busy={save.isPending}
      />
    </Card>
  );
}

export function SiemTab({ settings }: { settings: Settings }) {
  return (
    <FeatureSection feature="siem_forwarding">
      <SiemCard settings={settings} />
    </FeatureSection>
  );
}
