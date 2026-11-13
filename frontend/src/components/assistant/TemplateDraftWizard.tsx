import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { isApiError } from '../../api/client';
import { qk } from '../../api/endpoints';
import { endpoints3b, qk3b } from '../../api/endpoints3b';
import {
  DEFAULT_WALK_PRESETS,
  WALK_PRESETS,
  type CannotDraft,
  type SnmpWalk,
  type TemplateDraft,
  type WalkPreset,
} from '../../api/types';
import { useLiveMessages } from '../../hooks/live';
import { DevicePicker } from '../Dependency';
import { SparkIcon } from '../icons';
import { useToast } from '../toast';
import {
  Button,
  Checkbox,
  ErrorBox,
  Field,
  FieldGroup,
  Input,
  Modal,
  Notice,
  Spinner,
  Textarea,
} from '../ui';
import { AiNotice, CannotDraftNotice, useAiFeature } from './common';

const OID_RE = /^\d+(\.\d+){3,}$/;
type Step = 'setup' | 'walk' | 'draft';

function parseOids(text: string): { oids: string[]; bad: string[] } {
  const oids: string[] = [];
  const bad: string[] = [];
  for (const raw of text.split(/[\s,]+/)) {
    const v = raw.trim().replace(/^\./, '');
    if (!v) continue;
    (OID_RE.test(v) ? oids : bad).push(v);
  }
  return { oids, bad };
}

/** [3b] Templates page → "Draft a template from a device" (§ 14.8.7). */
export function TemplateDraftButton() {
  const { t } = useTranslation();
  const on = useAiFeature('ai_template_drafts');
  const [open, setOpen] = useState(false);
  if (!on) return null;
  return (
    <>
      <Button onClick={() => setOpen(true)} data-testid="template-draft-open">
        <SparkIcon size={14} className="text-accent-ink" /> {t('tdraft.open')}
      </Button>
      {open && <TemplateDraftWizard onClose={() => setOpen(false)} />}
    </>
  );
}

function TemplateDraftWizard({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const [step, setStep] = useState<Step>('setup');
  const [deviceId, setDeviceId] = useState<number | null>(null);
  const [presets, setPresets] = useState<WalkPreset[]>(DEFAULT_WALK_PRESETS);
  const [oidText, setOidText] = useState('');
  const [walkId, setWalkId] = useState<number | null>(null);
  const [templateId, setTemplateId] = useState('');
  const [notes, setNotes] = useState('');
  const [yaml, setYaml] = useState('');
  const [draft, setDraft] = useState<TemplateDraft | null>(null);
  const [cannot, setCannot] = useState<CannotDraft | null>(null);
  const [overwrite, setOverwrite] = useState(false);
  const { oids, bad } = parseOids(oidText);
  const tooMany = presets.length + oids.length > 8;

  const start = useMutation({
    mutationFn: () => endpoints3b.startWalk({ device_id: deviceId!, presets, oids }),
    onSuccess: (w) => {
      setWalkId(w.id);
      qc.setQueryData(qk3b.walk(w.id), w);
      setStep('walk');
    },
  });
  const walk = useQuery({
    queryKey: qk3b.walk(walkId ?? 0),
    queryFn: () => endpoints3b.walk(walkId!),
    enabled: walkId !== null,
    refetchInterval: (q) => (q.state.data?.status === 'running' ? 2000 : false),
  });
  useLiveMessages(['walk.update'], (m) => {
    if (m.type !== 'walk.update' || m.data.walk.id !== walkId) return;
    if (m.data.walk.status === 'running')
      qc.setQueryData<SnmpWalk>(qk3b.walk(m.data.walk.id), (old) => ({ ...old, ...m.data.walk }));
    else void qc.invalidateQueries({ queryKey: qk3b.walk(m.data.walk.id) });
  });
  const cancel = useMutation({
    mutationFn: () => endpoints3b.cancelWalk(walkId!),
    onSuccess: (w) => qc.setQueryData(qk3b.walk(w.id), w),
  });
  const makeDraft = useMutation({
    mutationFn: () =>
      endpoints3b.templateDraft({
        walk_id: walkId!,
        template_id: templateId.trim() || null,
        notes: notes.trim() || null,
      }),
    onMutate: () => setCannot(null),
    onSuccess: (d) => {
      // no template possible: stay on the walk step with the reason, no editor
      if (d.cannot_draft) {
        setCannot(d.cannot_draft);
        return;
      }
      setDraft(d);
      setYaml(d.yaml);
      setStep('draft');
    },
  });
  const save = useMutation({
    mutationFn: () => endpoints3b.saveUserTemplate(yaml, overwrite),
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: qk.deviceTemplates });
      toast.push('success', t('tdraft.saved', { file: r.file }));
      onClose();
    },
  });
  useEffect(() => {
    if (isApiError(save.error) && save.error.code === 'template_exists') setOverwrite(false);
  }, [save.error]);

  const w = walk.data;
  const finished = w && w.status !== 'running';
  const saveDetails =
    isApiError(save.error) && save.error.code === 'invalid_template'
      ? ((save.error.details ?? []) as { path?: string; error?: string }[])
      : [];

  return (
    <Modal open onClose={onClose} wide title={t('tdraft.title')} testId="template-draft">
      <div className="space-y-4">
        <ol className="flex flex-wrap gap-3 text-xs text-ink-muted">
          {(['setup', 'walk', 'draft'] as Step[]).map((s, i) => (
            <li key={s} className={s === step ? 'font-semibold text-accent-ink' : ''}>
              {i + 1}. {t(`tdraft.step.${s}`)}
            </li>
          ))}
        </ol>

        {step === 'setup' && (
          <div className="space-y-3">
            <p className="text-sm text-ink-muted">{t('tdraft.intro')}</p>
            <Field label={t('tdraft.device')} required>
              <DevicePicker value={deviceId} onChange={setDeviceId} label={t('tdraft.device')} />
            </Field>
            <FieldGroup label={t('tdraft.subtrees')} hint={t('tdraft.subtreesHint')}>
              <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
                {WALK_PRESETS.map((p) => (
                  <Checkbox
                    key={p}
                    label={t(`tdraft.preset.${p}`)}
                    checked={presets.includes(p)}
                    onChange={(v) => setPresets((x) => (v ? [...x, p] : x.filter((y) => y !== p)))}
                  />
                ))}
              </div>
            </FieldGroup>
            <Field
              label={t('tdraft.customOids')}
              hint={
                bad.length ? (
                  <span className="text-status-down">
                    {t('tdraft.badOids', { oids: bad.join(', ') })}
                  </span>
                ) : tooMany ? (
                  <span className="text-status-down">{t('tdraft.tooMany')}</span>
                ) : (
                  t('tdraft.customOidsHint')
                )
              }
            >
              <Input
                value={oidText}
                onChange={(e) => setOidText(e.target.value)}
                dir="ltr"
                className="font-mono"
                placeholder="1.3.6.1.4.1.25506.2.6"
              />
            </Field>
            <p className="text-xs text-ink-muted">{t('tdraft.neverWalked')}</p>
            <ErrorBox error={start.error} />
            <div className="flex justify-end gap-2">
              <Button onClick={onClose}>{t('common.cancel')}</Button>
              <Button
                variant="primary"
                onClick={() => start.mutate()}
                busy={start.isPending}
                disabled={
                  !deviceId || bad.length > 0 || tooMany || presets.length + oids.length === 0
                }
                data-testid="walk-start"
              >
                {t('tdraft.startWalk')}
              </Button>
            </div>
          </div>
        )}

        {step === 'walk' && (
          <div className="space-y-3">
            {w ? (
              <div className="space-y-1 text-sm" data-testid="walk-status" data-status={w.status}>
                <p className="flex items-center gap-2">
                  {w.status === 'running' && <Spinner className="h-4 w-4" />}
                  {t(`tdraft.walkStatus.${w.status}`)} · {t('tdraft.varbinds', { n: w.total })}
                </p>
                <ul className="text-xs text-ink-muted" dir="ltr">
                  {Object.entries(w.counts).map(([oid, n]) => (
                    <li key={oid}>
                      {oid}: {n}
                    </li>
                  ))}
                </ul>
                {w.error && <p className="text-xs text-status-down">{w.error}</p>}
              </div>
            ) : (
              <Spinner />
            )}
            <ErrorBox error={walk.error ?? cancel.error} />
            {finished && w.total > 0 && (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <Field label={t('tdraft.templateId')} hint={t('tdraft.templateIdHint')}>
                  <Input
                    value={templateId}
                    onChange={(e) => setTemplateId(e.target.value)}
                    dir="ltr"
                    placeholder="acme_x100"
                    pattern="[a-z0-9_]+"
                  />
                </Field>
                <Field label={t('tdraft.notes')} hint={t('tdraft.notesHint')}>
                  <Input value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={500} />
                </Field>
              </div>
            )}
            {cannot && (
              <CannotDraftNotice
                title={t('tdraft.cannotTitle')}
                reason={cannot.reason}
                hint={t('tdraft.cannotHint')}
                example={t('tdraft.cannotExample')}
              />
            )}
            <ErrorBox error={makeDraft.error} />
            {makeDraft.isPending && (
              <p className="text-xs text-ink-muted">{t('tdraft.drafting')}</p>
            )}
            <div className="flex justify-end gap-2">
              {w?.status === 'running' && (
                <Button onClick={() => cancel.mutate()} busy={cancel.isPending}>
                  {t('tdraft.cancelWalk')}
                </Button>
              )}
              <Button onClick={() => setStep('setup')} disabled={w?.status === 'running'}>
                {t('tdraft.back')}
              </Button>
              <Button
                variant="primary"
                onClick={() => makeDraft.mutate()}
                busy={makeDraft.isPending}
                disabled={!finished || !w?.total}
                data-testid="draft-template"
              >
                <SparkIcon size={14} /> {t('tdraft.makeDraft')}
              </Button>
            </div>
          </div>
        )}

        {step === 'draft' && draft && (
          <div className="space-y-3">
            <Notice>{t('tdraft.reviewBanner')}</Notice>
            {draft.valid ? (
              <p className="text-sm text-status-up" data-testid="draft-valid">
                {t('tdraft.valid')}
              </p>
            ) : (
              <div className="text-sm text-status-down" data-testid="draft-invalid">
                {t('tdraft.invalid')}
                <ul className="list-disc ps-5 text-xs">
                  {draft.errors.map((e, i) => (
                    <li key={i}>
                      <bdi dir="ltr">{e.path}</bdi>: {e.error}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {draft.warnings.length > 0 && (
              <ul className="list-disc ps-5 text-xs text-status-warning">
                {draft.warnings.map((x, i) => (
                  <li key={i}>{x}</li>
                ))}
              </ul>
            )}
            {draft.notes && (
              <p className="whitespace-pre-wrap text-xs text-ink-muted" dir="auto">
                {draft.notes}
              </p>
            )}
            <Textarea
              value={yaml}
              onChange={(e) => setYaml(e.target.value)}
              rows={18}
              dir="ltr"
              spellCheck={false}
              className="font-mono text-xs"
              aria-label={t('tdraft.yaml')}
              data-testid="draft-yaml"
            />
            <AiNotice />
            {saveDetails.length > 0 && (
              <ul className="list-disc ps-5 text-xs text-status-down">
                {saveDetails.map((e, i) => (
                  <li key={i}>
                    <bdi dir="ltr">{e.path}</bdi>: {e.error}
                  </li>
                ))}
              </ul>
            )}
            <ErrorBox error={save.error} />
            {isApiError(save.error) && save.error.code === 'template_exists' && (
              <Checkbox label={t('tdraft.overwrite')} checked={overwrite} onChange={setOverwrite} />
            )}
            <div className="flex justify-end gap-2">
              <Button onClick={() => setStep('walk')}>{t('tdraft.back')}</Button>
              <Button
                variant="primary"
                onClick={() => save.mutate()}
                busy={save.isPending}
                disabled={!yaml.trim()}
                data-testid="save-user-template"
              >
                {t('tdraft.save')}
              </Button>
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}
