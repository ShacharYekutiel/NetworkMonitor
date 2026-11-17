import { useId, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints3b } from '../../api/endpoints3b';
import type { CannotDraft, RuleDraft, RuleDraftKind } from '../../api/types';
import { SparkIcon, XIcon } from '../icons';
import { Banner, WizardSteps } from '../kit';
import { Button, ErrorBox, Spinner, Textarea } from '../ui';
import { AiBadge, CannotDraftNotice, useAiFeature } from './common';

/**
 * [3b] "Describe the rule…" + Draft (§ 14.8.6) at the top of a NEW rule form. The draft only
 * prefills the form (`onDraft`); the admin reviews every field and saves through the normal API.
 */
export function RuleDraftBox({
  kind,
  onDraft,
}: {
  kind: RuleDraftKind;
  onDraft: (d: RuleDraft) => void;
}) {
  const { t } = useTranslation();
  const on = useAiFeature('ai_rule_drafts');
  const [open, setOpen] = useState(false);
  const [text, setText] = useState('');
  const [cannot, setCannot] = useState<CannotDraft | null>(null);
  const fieldId = useId();
  const m = useMutation({
    mutationFn: () => endpoints3b.ruleDraft(kind, text.trim()),
    onMutate: () => setCannot(null),
    onSuccess: (d) => {
      // no draft possible: keep the box open with the reason, never prefill the form
      if (d.cannot_draft || !d.draft) {
        setCannot(d.cannot_draft ?? { reason: '', source: 'placeholder' });
        return;
      }
      onDraft(d);
      setOpen(false);
    },
  });
  if (!on) return null;
  if (!open)
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex h-9 items-center gap-2 rounded-full bg-chip pe-4 ps-3 text-[13.5px] font-medium text-ink transition-colors hover:text-accent-ink"
        data-testid="describe-rule"
      >
        <SparkIcon size={15} className="text-accent-ink" /> {t('draft.describe')}
      </button>
    );
  const len = text.trim().length;
  const examples = [1, 2, 3].map((i) =>
    t(`draft.${kind === 'alert_rule' ? 'exAlert' : 'exEvent'}${i}`),
  );
  return (
    // the template draft wizard's look: steps, a card with the field, example chips, actions
    <div className="grid gap-3.5 rounded-[20px] bg-surface-sunken p-4" data-testid="rule-draft-box">
      <WizardSteps
        label={t('draft.stepsLabel')}
        current={m.isPending ? 1 : 0}
        steps={[t('draft.step.describe'), t('draft.step.draft'), t('draft.step.review')]}
      />
      <div className="-mt-[22px] grid gap-1.5">
        <label className="text-[13.5px] text-ink-muted" htmlFor={fieldId}>
          {t('draft.describeLabel')}
        </label>
        <Textarea
          id={fieldId}
          rows={3}
          maxLength={1000}
          value={text}
          dir="auto"
          placeholder={
            kind === 'alert_rule' ? t('draft.alertPlaceholder') : t('draft.eventPlaceholder')
          }
          onChange={(e) => {
            setText(e.target.value);
            setCannot(null);
          }}
          disabled={m.isPending}
          data-testid="describe-text"
        />
      </div>
      <div className="flex flex-wrap gap-1.5" aria-label={t('draft.examples')} role="group">
        {examples.map((x) => (
          <button
            key={x}
            type="button"
            onClick={() => {
              setText(x);
              setCannot(null);
            }}
            disabled={m.isPending}
            className="inline-flex min-h-8 items-center rounded-full bg-surface px-3 py-1 text-start text-[13px] text-ink-muted transition-colors hover:text-ink"
          >
            {x}
          </button>
        ))}
      </div>
      {m.isPending ? (
        <p className="m-0 flex items-center gap-2.5 text-sm text-ink-muted" role="status">
          <Spinner className="h-4 w-4" />
          {t('draft.working')}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center gap-2.5">
        <Button
          variant="primary"
          onClick={() => m.mutate()}
          busy={m.isPending}
          disabled={len < 5}
          data-testid="draft-button"
        >
          <SparkIcon size={14} /> {t('draft.draft')}
        </Button>
        <Button onClick={() => setOpen(false)} disabled={m.isPending}>
          {t('draft.manual')}
        </Button>
        <span className="ms-auto">
          <AiBadge />
        </span>
      </div>
      {cannot && (
        <CannotDraftNotice
          title={t('draft.cannotTitle')}
          reason={cannot.reason}
          hint={t('draft.cannotHint')}
          example={
            kind === 'alert_rule' ? t('draft.cannotExampleAlert') : t('draft.cannotExampleEvent')
          }
        />
      )}
      <ErrorBox error={m.error} />
    </div>
  );
}

/** Banner over a form prefilled by a draft: review notice, explanation, assumptions, validation. */
export function DraftBanner({ draft, onDismiss }: { draft: RuleDraft; onDismiss?: () => void }) {
  const { t } = useTranslation();
  const errors = !draft.validation.ok ? draft.validation.errors : [];
  return (
    <div
      className="grid gap-3 rounded-[20px] bg-surface-sunken p-4"
      data-testid="draft-banner"
      role="note"
    >
      <WizardSteps
        label={t('draft.stepsLabel')}
        current={2}
        steps={[t('draft.step.describe'), t('draft.step.draft'), t('draft.step.review')]}
      />
      <div className="-mt-[22px] flex items-start gap-3 rounded-lg bg-accent-soft px-4 py-3.5">
        <SparkIcon size={20} className="mt-px shrink-0 text-accent-ink" />
        <div className="grid flex-1 gap-1">
          <b className="flex flex-wrap items-center gap-2 font-semibold">
            {t('draft.banner')}
            <AiBadge />
          </b>
          {draft.explanation && (
            <span dir="auto" className="text-[13.5px] text-ink-muted">
              {draft.explanation}
            </span>
          )}
        </div>
        {onDismiss && (
          <button
            type="button"
            aria-label={t('common.close')}
            className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-ink-muted hover:bg-surface"
            onClick={onDismiss}
          >
            <XIcon size={16} />
          </button>
        )}
      </div>
      {draft.assumptions.length > 0 && (
        <div className="grid gap-1.5 px-1">
          <span className="text-[13.5px] font-semibold">{t('draft.assumptions')}</span>
          <ul className="m-0 grid list-disc gap-1 ps-5 text-[13.5px] text-ink-muted">
            {draft.assumptions.map((a, i) => (
              <li key={i} dir="auto">
                {a}
              </li>
            ))}
          </ul>
        </div>
      )}
      {errors.length > 0 && (
        <Banner tone="down" title={t('draft.validationErrors')}>
          <ul className="m-0 list-disc ps-5">
            {errors.map((e, i) => (
              <li key={i}>
                <bdi dir="ltr">{e.loc.join('.')}</bdi>: {e.msg}
              </li>
            ))}
          </ul>
        </Banner>
      )}
      <p className="m-0 px-1 text-xs text-ink-muted">{t('assistant.mayBeWrong')}</p>
    </div>
  );
}
