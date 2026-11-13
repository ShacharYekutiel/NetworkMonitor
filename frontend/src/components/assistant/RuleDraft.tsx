import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints3b } from '../../api/endpoints3b';
import type { CannotDraft, RuleDraft, RuleDraftKind } from '../../api/types';
import { SparkIcon } from '../icons';
import { Button, ErrorBox, Textarea } from '../ui';
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
        className="inline-flex items-center gap-1.5 text-xs font-medium text-violet-700 hover:underline dark:text-violet-300"
        data-testid="describe-rule"
      >
        <SparkIcon size={12} /> {t('draft.describe')}
      </button>
    );
  const len = text.trim().length;
  return (
    <div className="space-y-2 rounded-md bg-violet-50/70 p-3 ring-1 ring-inset ring-violet-200 dark:bg-violet-950/20 dark:ring-violet-900">
      <label className="block text-xs font-medium text-slate-600 dark:text-slate-400">
        {t('draft.describeLabel')}
        <Textarea
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
          className="mt-1"
          data-testid="describe-text"
        />
      </label>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant="primary"
          onClick={() => m.mutate()}
          busy={m.isPending}
          disabled={len < 5}
          data-testid="draft-button"
        >
          <SparkIcon size={12} /> {t('draft.draft')}
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
          {t('common.cancel')}
        </Button>
        {m.isPending && <span className="text-xs text-slate-500">{t('draft.working')}</span>}
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
  return (
    <div
      className="space-y-1 rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-900 ring-1 ring-inset ring-amber-200 dark:bg-amber-950/40 dark:text-amber-200 dark:ring-amber-900"
      data-testid="draft-banner"
      role="note"
    >
      <p className="flex items-center gap-2 font-medium">
        <AiBadge />
        {t('draft.banner')}
        {onDismiss && (
          <button type="button" className="ms-auto text-xs underline" onClick={onDismiss}>
            {t('common.close')}
          </button>
        )}
      </p>
      {draft.explanation && (
        <p dir="auto" className="text-xs">
          {draft.explanation}
        </p>
      )}
      {draft.assumptions.length > 0 && (
        <div className="text-xs">
          <span className="font-medium">{t('draft.assumptions')}:</span>
          <ul className="list-disc ps-5">
            {draft.assumptions.map((a, i) => (
              <li key={i} dir="auto">
                {a}
              </li>
            ))}
          </ul>
        </div>
      )}
      {!draft.validation.ok && draft.validation.errors.length > 0 && (
        <div className="text-xs text-red-700 dark:text-red-300">
          <span className="font-medium">{t('draft.validationErrors')}:</span>
          <ul className="list-disc ps-5">
            {draft.validation.errors.map((e, i) => (
              <li key={i}>
                <bdi dir="ltr">{e.loc.join('.')}</bdi>: {e.msg}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
