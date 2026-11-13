import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints3b, qk3b } from '../../api/endpoints3b';
import type { AiFeatureName, AssistantState } from '../../api/types';
import { useAuth } from '../../hooks/auth';
import { useFeatures } from '../../hooks/features';
import { BANNER, BANNER_TONE, cx, TAG, TAG_TONE } from '../ui';

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
    <span className={cx(TAG, TAG_TONE.accent, className)} title={t('assistant.aiGenerated')}>
      {t('assistant.ai')}
    </span>
  );
}

/** "AI-generated, may be wrong" — once per surface (owner rule "Labelled"). */
export function AiNotice({ className }: { className?: string }) {
  const { t } = useTranslation();
  return (
    <p className={cx('text-xs text-ink-muted', className)} data-testid="ai-notice">
      <AiBadge className="me-1" />
      {t('assistant.mayBeWrong')}
    </p>
  );
}

const STATE_TONE: Record<AssistantState, string> = {
  disabled: 'bg-status-paused',
  unconfigured: 'bg-status-paused',
  ready: 'bg-status-up',
  degraded: 'bg-status-warning',
  error: 'bg-status-down',
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
      className={cx(BANNER, BANNER_TONE.warning, 'space-y-1')}
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
