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
import { AuditIcon, CheckIcon, SparkIcon } from '../icons';
import { Banner, ToggleChip, WizardSteps } from '../kit';
import { useToast } from '../toast';
import {
  Button,
  Checkbox,
  cx,
  ErrorBox,
  Field,
  FieldGroup,
  Input,
  Modal,
  Spinner,
  TAG,
  TAG_TONE,
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

  const STEPS: Step[] = ['setup', 'walk', 'draft'];
  return (
    <Modal open onClose={onClose} wide title={t('tdraft.title')} testId="template-draft">
      <div className="grid gap-4">
        <WizardSteps
          label={t('tdraft.title')}
          steps={STEPS.map((s) => t(`tdraft.step.${s}`))}
          current={STEPS.indexOf(step)}
          onGo={w?.status === 'running' ? undefined : (i) => setStep(STEPS[i])}
        />

        {step === 'setup' && (
          <div className="grid gap-4">
            <p className="m-0 text-ink-muted">{t('tdraft.intro')}</p>
            <Field label={t('tdraft.device')} required>
              <DevicePicker value={deviceId} onChange={setDeviceId} label={t('tdraft.device')} />
            </Field>
            <FieldGroup label={t('tdraft.subtrees')} hint={t('tdraft.subtreesHint')}>
              <div className="flex flex-wrap gap-2">
                {WALK_PRESETS.map((p) => (
                  <ToggleChip
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
            <Banner icon={AuditIcon} title={t('tdraft.neverWalked')} />
            <ErrorBox error={start.error} />
            <div className="flex flex-wrap gap-2.5">
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
              <Button onClick={onClose}>{t('common.cancel')}</Button>
            </div>
          </div>
        )}

        {step === 'walk' && (
          <div className="grid gap-4">
            {w ? <WalkResult walk={w} /> : <Spinner />}
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
              <p className="m-0 flex items-center gap-2 text-sm text-ink-muted">
                <Spinner className="h-4 w-4" />
                {t('tdraft.drafting')}
              </p>
            )}
            <div className="flex flex-wrap gap-2.5">
              <Button
                variant="primary"
                onClick={() => makeDraft.mutate()}
                busy={makeDraft.isPending}
                disabled={!finished || !w?.total}
                data-testid="draft-template"
              >
                <SparkIcon size={14} /> {t('tdraft.makeDraft')}
              </Button>
              {w?.status === 'running' && (
                <Button onClick={() => cancel.mutate()} busy={cancel.isPending}>
                  {t('tdraft.cancelWalk')}
                </Button>
              )}
              <Button onClick={() => setStep('setup')} disabled={w?.status === 'running'}>
                {t('tdraft.back')}
              </Button>
            </div>
          </div>
        )}

        {step === 'draft' && draft && (
          <div className="grid gap-4">
            <Banner tone="info" icon={SparkIcon} title={t('tdraft.reviewBanner')} />
            {draft.valid ? (
              <div
                className="flex items-center gap-2.5 rounded-[14px] bg-status-up-soft px-3.5 py-3 text-sm"
                data-testid="draft-valid"
              >
                <CheckIcon size={18} className="shrink-0 text-status-up" />
                {t('tdraft.valid')}
              </div>
            ) : (
              <Banner tone="down" title={t('tdraft.invalid')}>
                <span data-testid="draft-invalid">
                  <ul className="m-0 list-disc ps-5">
                    {draft.errors.map((e, i) => (
                      <li key={i}>
                        <bdi dir="ltr">{e.path}</bdi>: {e.error}
                      </li>
                    ))}
                  </ul>
                </span>
              </Banner>
            )}
            {draft.warnings.length > 0 && (
              <Banner tone="warn" title={t('tdraft.warnings')}>
                <ul className="m-0 list-disc ps-5">
                  {draft.warnings.map((x, i) => (
                    <li key={i}>{x}</li>
                  ))}
                </ul>
              </Banner>
            )}
            {draft.notes && (
              <p className="m-0 whitespace-pre-wrap text-sm text-ink-muted" dir="auto">
                {draft.notes}
              </p>
            )}
            <div className="grid gap-1.5">
              <span className="text-[13px] text-ink-muted">{t('tdraft.yaml')}</span>
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
            </div>
            <AiNotice />
            {saveDetails.length > 0 && (
              <ul className="m-0 list-disc ps-5 text-xs text-status-down">
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
            <div className="flex flex-wrap gap-2.5">
              <Button
                variant="primary"
                onClick={() => save.mutate()}
                busy={save.isPending}
                disabled={!yaml.trim()}
                data-testid="save-user-template"
              >
                {t('tdraft.save')}
              </Button>
              <Button onClick={() => setStep('walk')}>{t('tdraft.back')}</Button>
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}

const OUTCOME_TAG: Record<string, string> = {
  ok: TAG_TONE.up,
  truncated: TAG_TONE.warning,
  timeout: TAG_TONE.warning,
  error: TAG_TONE.down,
  skipped: TAG_TONE.neutral,
};

/** [maint-1.4] Walk status, the per-subtree outcome (each subtree has its own time budget) and the
 * cause when the walk did not finish (rejected credentials / no answer / too slow / SNMP error).
 * Premium: a status line, the subtrees as rows with a tag, the cause as an error box. */
export function WalkResult({ walk: w }: { walk: SnmpWalk }) {
  const { t } = useTranslation();
  const outcomes = w.outcomes ?? [];
  const failed = w.status === 'failed';
  return (
    <div className="grid gap-3" data-testid="walk-status" data-status={w.status}>
      <p
        className={cx(
          'm-0 flex items-center gap-2 text-[17px] font-semibold',
          failed && 'text-status-down',
        )}
      >
        {w.status === 'running' && <Spinner className="h-4 w-4" />}
        {t(`tdraft.walkStatus.${w.status}`)}
        <span className="text-sm font-normal text-ink-muted">
          · {t('tdraft.varbinds', { n: w.total })}
        </span>
      </p>
      <ul
        className="m-0 list-none overflow-hidden rounded-[18px] bg-chip p-0 text-sm [&>li+li]:border-t [&>li+li]:border-line"
        data-testid="walk-subtrees"
      >
        {outcomes.length > 0
          ? outcomes.map((o) => (
              <li
                key={o.subtree}
                data-outcome={o.status}
                className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5"
              >
                <bdi dir="ltr" className="num min-w-0 flex-1 truncate">
                  {o.subtree}
                </bdi>
                <span className="num text-[13px] text-ink-muted">
                  {t('tdraft.varbinds', { n: o.count })}
                </span>
                <span className={cx(TAG, OUTCOME_TAG[o.status] ?? TAG_TONE.neutral)}>
                  {t(`tdraft.outcome.${o.status}`)}
                </span>
              </li>
            ))
          : Object.entries(w.counts).map(([oid, n]) => (
              <li key={oid} className="px-4 py-2.5">
                <bdi dir="ltr">{oid}</bdi>: {n}
              </li>
            ))}
      </ul>
      {w.status !== 'running' && (w.error_code || w.error) && (
        <div
          className="grid gap-1 rounded-[14px] bg-status-down-soft px-4 py-3 text-sm"
          data-testid="walk-error"
          data-code={w.error_code ?? ''}
        >
          {w.error_code && (
            <p className="m-0 font-medium">{t(`tdraft.walkError.${w.error_code}`)}</p>
          )}
          {w.error && (
            <p className="m-0 text-[13px] text-ink-muted">
              {t('tdraft.walkErrorDetail')}{' '}
              <code dir="ltr" className="font-mono">
                {w.error}
              </code>
            </p>
          )}
        </div>
      )}
    </div>
  );
}
