import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints3b, qk3b } from '../../api/endpoints3b';
import type { AiFeatureName, AssistantState } from '../../api/types';
import { useAuth } from '../../hooks/auth';
import { useFeatures } from '../../hooks/features';
import { cx } from '../ui';

/** An AI feature's entry points are shown to admins while the feature AND the assistant are ON. */
export function useAiFeature(name: AiFeatureName): boolean {
  const { isAdmin } = useAuth();
  const { features } = useFeatures();
  return isAdmin && features.assistant && features[name];
}

/** GET /api/assistant/status (admins, while the assistant is ON). */
export function useAssistantStatus(enabled = true) {
  const { isAdmin } = useAuth();
  const { features } = useFeatures();
  return useQuery({
    queryKey: qk3b.status,
    queryFn: endpoints3b.status,
    enabled: enabled && isAdmin && features.assistant,
    staleTime: 15_000,
    refetchInterval: 60_000,
  });
}

/** The small "AI" chip next to AI-written content and AI buttons. */
export function AiBadge({ className }: { className?: string }) {
  const { t } = useTranslation();
  return (
    <span
      className={cx(
        'inline-flex items-center rounded bg-violet-100 px-1 text-[10px] font-semibold uppercase tracking-wide text-violet-800 dark:bg-violet-900/60 dark:text-violet-200',
        className,
      )}
      title={t('assistant.aiGenerated')}
    >
      {t('assistant.ai')}
    </span>
  );
}

/** "AI-generated, may be wrong" — once per surface (owner rule "Labelled"). */
export function AiNotice({ className }: { className?: string }) {
  const { t } = useTranslation();
  return (
    <p className={cx('text-xs text-slate-500', className)} data-testid="ai-notice">
      <AiBadge className="me-1" />
      {t('assistant.mayBeWrong')}
    </p>
  );
}

const STATE_TONE: Record<AssistantState, string> = {
  disabled: 'bg-slate-400',
  unconfigured: 'bg-slate-500',
  ready: 'bg-green-600',
  degraded: 'bg-amber-500',
  error: 'bg-red-600',
};

export function AssistantStateBadge({ state }: { state: AssistantState }) {
  const { t } = useTranslation();
  return (
    <span className="inline-flex items-center gap-1.5 text-xs" data-testid="assistant-state">
      <span aria-hidden className={cx('h-2 w-2 rounded-full', STATE_TONE[state])} />
      {t(`assistant.state.${state}`)}
    </span>
  );
}

/** Text of a `{code, message}` error of the assistant (translated when we know the code). */
export function useAssistantErrorText() {
  const { t } = useTranslation();
  return (e: { code: string; message?: string } | null | undefined): string => {
    if (!e) return '';
    const tr = t(`errors.${e.code}`, { defaultValue: '' });
    if (!tr) return e.message || e.code;
    // codes whose server text carries the useful part (AWS message, host, …)
    const withServer = new Set([
      'bedrock_access_denied',
      'bedrock_validation',
      'aws_auth_failed',
      'rolesanywhere_failed',
      'aws_network',
      'kb_unavailable',
    ]);
    return withServer.has(e.code) && e.message && e.message !== tr ? `${tr} — ${e.message}` : tr;
  };
}

/**
 * [3b owner] "No draft possible" (rule / template drafts): the reason (in the description's
 * language, from the server) and an example of a usable description; no form is opened.
 */
export function CannotDraftNotice({
  title,
  reason,
  hint,
  example,
  testId = 'cannot-draft',
}: {
  title: string;
  reason: string;
  hint: string;
  example: string;
  testId?: string;
}) {
  return (
    <div
      className="space-y-1 rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-900 ring-1 ring-inset ring-amber-200 dark:bg-amber-950/40 dark:text-amber-200 dark:ring-amber-900"
      role="status"
      data-testid={testId}
    >
      <p className="flex items-center gap-2 font-medium">
        <AiBadge />
        {title}
      </p>
      {reason && (
        <p dir="auto" className="text-xs" data-testid={`${testId}-reason`}>
          {reason}
        </p>
      )}
      <p className="text-xs">{hint}</p>
      <p dir="auto" className="text-xs italic">
        “{example}”
      </p>
    </div>
  );
}
