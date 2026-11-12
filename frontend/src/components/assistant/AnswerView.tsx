import { useMemo, useState, type MouseEvent } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { endpoints3b, qk3b } from '../../api/endpoints3b';
import type { AssistantMessage, AssistantSource, AssistantStep } from '../../api/types';
import {
  argsSummary,
  compactTokens,
  copyableMarkdown,
  formatCost,
  isRetryable,
  kbSourceUrl,
  nmRoute,
  type LiveAnswer,
} from '../../lib/assistant';
import { renderAnswer } from '../../lib/markdown';
import { CheckIcon, ChevronIcon, StopIcon, ThumbIcon, XIcon } from '../icons';
import { useToast } from '../toast';
import { Button, cx, Spinner, Textarea } from '../ui';
import { AiBadge, useAssistantErrorText } from './common';

/** Safe Markdown of an AI text; internal links navigate inside the SPA. */
export function Markdown({
  text,
  allowedRefs,
  sources,
  className,
}: {
  text: string;
  allowedRefs?: ReadonlySet<string> | null;
  sources?: AssistantSource[];
  className?: string;
}) {
  const navigate = useNavigate();
  const html = useMemo(
    () => renderAnswer(text, { allowedRefs, sources }),
    [text, allowedRefs, sources],
  );
  const onClick = (e: MouseEvent<HTMLDivElement>) => {
    const a = (e.target as HTMLElement).closest('a[data-nm]');
    if (!a || e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
    const href = a.getAttribute('href');
    if (href?.startsWith('/')) {
      e.preventDefault();
      navigate(href);
    }
  };
  return (
    <div
      className={cx('nm-md text-sm leading-relaxed', className)}
      onClick={onClick}
      // rendered by markdown-it with HTML disabled, no images, nm: links only (lib/markdown.ts)
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}

function StepRow({ s }: { s: AssistantStep }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const label = t(s.label_key, { defaultValue: t('assistant.tool.generic', { tool: s.tool }) });
  const hasArgs = Object.keys(s.args ?? {}).length > 0;
  return (
    <li className="text-xs" data-testid="tool-step" data-tool={s.tool} data-state={s.state}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        disabled={!hasArgs}
        aria-expanded={hasArgs ? open : undefined}
        className="flex w-full items-center gap-1.5 text-start text-slate-600 disabled:cursor-default dark:text-slate-400"
      >
        {s.state === 'running' ? (
          <Spinner className="h-3 w-3" />
        ) : s.ok ? (
          <CheckIcon size={12} className="text-green-600" />
        ) : (
          <XIcon size={12} className="text-red-600" />
        )}
        <span>{label}</span>
        {s.state === 'done' && (
          <span className="text-slate-400">
            {s.ok
              ? t('assistant.stepDone', { rows: s.rows ?? 0, ms: s.ms ?? 0 })
              : t('assistant.stepFailed', { error: s.error ?? '' })}
            {s.truncated && ` · ${t('assistant.truncated')}`}
          </span>
        )}
        {hasArgs && (
          <ChevronIcon
            size={11}
            className={cx('ms-auto transition-transform', open ? 'rotate-90' : 'rtl:rotate-180')}
          />
        )}
      </button>
      {open && hasArgs && (
        <code
          dir="ltr"
          className="mt-0.5 block whitespace-pre-wrap break-all rounded bg-slate-100 px-2 py-1 text-[11px] dark:bg-slate-800"
        >
          {argsSummary(s.args, 2000)}
        </code>
      )}
    </li>
  );
}

/** Tool steps of an answer ("Searching syslog… 37 messages · 84 ms"), expandable arguments. */
export function ToolSteps({ steps }: { steps: AssistantStep[] }) {
  if (!steps.length) return null;
  return (
    <ul className="space-y-0.5 border-s-2 border-slate-200 ps-2 dark:border-slate-700">
      {steps.map((s) => (
        <StepRow key={s.call_id} s={s} />
      ))}
    </ul>
  );
}

function Sources({ sources }: { sources: AssistantSource[] }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  if (!sources.length) return null;
  return (
    <div className="text-xs" data-testid="answer-sources">
      <span className="font-medium text-slate-500">{t('assistant.sources')}:</span>{' '}
      {sources.map((s, i) => {
        const route = s.ref.startsWith('kb/') ? null : nmRoute(s.ref);
        const ext = s.ref.startsWith('kb/') ? kbSourceUrl(s.ref, sources) : null;
        return (
          <span key={s.ref}>
            {i > 0 && ' · '}
            {route ? (
              <a
                href={route}
                onClick={(e) => {
                  e.preventDefault();
                  navigate(route);
                }}
                className="text-sky-700 hover:underline dark:text-sky-400"
              >
                {s.label}
              </a>
            ) : ext ? (
              <a
                href={ext}
                target="_blank"
                rel="noopener noreferrer nofollow"
                className="text-sky-700 hover:underline dark:text-sky-400"
              >
                {s.label}
              </a>
            ) : (
              <span dir="auto" className="text-slate-600 dark:text-slate-300">
                {s.label}
                {s.url && <bdi dir="ltr"> ({s.url.split('/').pop()})</bdi>}
              </span>
            )}
          </span>
        );
      })}
    </div>
  );
}

function Feedback({ m }: { m: AssistantMessage }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [comment, setComment] = useState('');
  const [asking, setAsking] = useState(false);
  const mut = useMutation({
    mutationFn: (v: { rating: 1 | -1 | 0; comment: string | null }) =>
      endpoints3b.feedback(m.id, v.rating, v.comment),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk3b.conversation(m.conversation_id) });
      setAsking(false);
    },
  });
  const rate = (r: 1 | -1) => {
    const next = m.feedback === r ? 0 : r;
    mut.mutate({ rating: next, comment: null });
    setAsking(next === -1);
  };
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <Button
        size="sm"
        variant="ghost"
        aria-pressed={m.feedback === 1}
        aria-label={t('assistant.thumbsUp')}
        title={t('assistant.thumbsUp')}
        onClick={() => rate(1)}
        className={m.feedback === 1 ? 'text-green-700 dark:text-green-400' : ''}
        disabled={mut.isPending}
      >
        <ThumbIcon size={14} />
      </Button>
      <Button
        size="sm"
        variant="ghost"
        aria-pressed={m.feedback === -1}
        aria-label={t('assistant.thumbsDown')}
        title={t('assistant.thumbsDown')}
        onClick={() => rate(-1)}
        className={m.feedback === -1 ? 'text-red-700 dark:text-red-400' : ''}
        disabled={mut.isPending}
      >
        <ThumbIcon size={14} className="rotate-180" />
      </Button>
      {asking && (
        <span className="flex w-full items-end gap-1">
          <Textarea
            rows={2}
            maxLength={1000}
            value={comment}
            placeholder={t('assistant.feedbackComment')}
            onChange={(e) => setComment(e.target.value)}
            className="text-xs"
          />
          <Button
            size="sm"
            onClick={() => mut.mutate({ rating: -1, comment: comment.trim() || null })}
            busy={mut.isPending}
          >
            {t('common.save')}
          </Button>
        </span>
      )}
    </span>
  );
}

/** Footer: model, tokens, ≈ cost, duration. */
function MetaLine({ m }: { m: AssistantMessage }) {
  const { t } = useTranslation();
  const tokens = m.usage.input_tokens + m.usage.output_tokens;
  return (
    <span className="text-[11px] text-slate-400" data-testid="answer-meta">
      {m.model_id && <bdi dir="ltr">{m.model_id}</bdi>}
      {' · '}
      {t('assistant.tokens', { n: compactTokens(tokens) })}
      {m.cost_estimate !== null && ` · ≈ ${formatCost(m.cost_estimate, m.currency)}`}
      {m.duration_ms !== null && ` · ${(m.duration_ms / 1000).toFixed(1)} s`}
    </span>
  );
}

/**
 * One assistant answer: dimmed notes of tool rounds, tool steps, the Markdown answer (live while
 * streaming), notices, error with Retry, sources, Copy, feedback, meta line.
 */
export function AnswerBlock({
  live,
  message,
  running,
  onStop,
  onRetry,
  compact,
}: {
  live?: LiveAnswer | null;
  message?: AssistantMessage | null;
  running?: boolean;
  onStop?: () => void;
  onRetry?: () => void;
  compact?: boolean;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const errText = useAssistantErrorText();
  const final = message ?? live?.final ?? null;
  const notes = final?.notes ?? live?.notes ?? [];
  const steps = final?.steps?.length ? final.steps : (live?.steps ?? []);
  const text = final ? final.content : (live?.text ?? '');
  const allowed = useMemo(() => (final ? null : new Set(live?.refs ?? [])), [final, live?.refs]);
  const error = final?.error ?? live?.error ?? null;
  const notices = final
    ? final.notices.map((c) => ({ code: c, message: '' }))
    : (live?.notices ?? []);
  const isRunning = !!running && !final;
  const copy = async () => {
    if (!final) return;
    try {
      await navigator.clipboard.writeText(
        copyableMarkdown(final.content, window.location.origin, final.sources),
      );
      toast.push('success', t('assistant.copied'));
    } catch {
      toast.push('error', t('assistant.copyFailed'));
    }
  };
  return (
    <div className="space-y-2" data-testid="answer" data-status={final?.status ?? 'running'}>
      {notes.map((n) => (
        <div key={n.round} className="opacity-60" data-testid="round-note">
          <Markdown text={n.text} allowedRefs={allowed} sources={final?.sources} />
        </div>
      ))}
      <ToolSteps steps={steps} />
      {text ? (
        <Markdown text={text} allowedRefs={allowed} sources={final?.sources} />
      ) : (
        isRunning && (
          <p className="flex items-center gap-2 text-sm text-slate-500">
            <Spinner className="h-4 w-4" /> {t('assistant.thinking')}
          </p>
        )
      )}
      {notices.map((n, i) => (
        <p
          key={i}
          className="rounded bg-amber-50 px-2 py-1 text-xs text-amber-800 dark:bg-amber-950/40 dark:text-amber-300"
          data-testid="answer-notice"
        >
          {t(`assistant.notice.${n.code}`, { defaultValue: n.message || n.code })}
        </p>
      ))}
      {final?.status === 'cancelled' && (
        <p className="text-xs text-slate-500">{t('assistant.cancelled')}</p>
      )}
      {error && (
        <div
          role="alert"
          className="flex flex-wrap items-center gap-2 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 ring-1 ring-inset ring-red-200 dark:bg-red-950/40 dark:text-red-300 dark:ring-red-900"
          data-testid="answer-error"
          data-code={error.code}
        >
          <span className="flex-1">{errText(error)}</span>
          {onRetry && isRetryable(error.code) && !isRunning && (
            <Button size="sm" onClick={onRetry}>
              {t('common.retry')}
            </Button>
          )}
        </div>
      )}
      {final && <Sources sources={final.sources} />}
      <div className="flex flex-wrap items-center gap-2">
        {isRunning && onStop && (
          <Button size="sm" onClick={onStop} data-testid="stop">
            <StopIcon size={12} /> {t('assistant.stop')}
          </Button>
        )}
        {final && final.content && !compact && (
          <Button size="sm" variant="ghost" onClick={() => void copy()}>
            {t('assistant.copy')}
          </Button>
        )}
        {final && !compact && <Feedback m={final} />}
        {final && (
          <span className="ms-auto inline-flex items-center gap-1">
            <AiBadge />
            <MetaLine m={final} />
          </span>
        )}
      </div>
    </div>
  );
}

/** Errors before the stream started (busy, caps, conversation full, …). */
export function PreStreamErrorBox({
  error,
  onAttach,
}: {
  error: {
    code: string;
    message: string;
    status: number;
    details: Record<string, unknown>[];
  } | null;
  onAttach?: (messageId: number) => void;
}) {
  const { t } = useTranslation();
  if (!error) return null;
  const d = error.details[0] ?? {};
  let extra = '';
  if (error.code === 'assistant_cap_reached' && typeof d.cap === 'string')
    extra = t('assistant.capDetail', {
      cap: t(`assistant.cap.${d.cap}`, { defaultValue: d.cap }),
      used: d.used ?? '',
      limit: d.limit ?? '',
    });
  const runningId =
    error.code === 'assistant_busy' && typeof d.message_id === 'number' ? d.message_id : null;
  return (
    <div className="space-y-1" data-testid="prestream-error" data-code={error.code}>
      <div
        role="alert"
        className="flex flex-wrap items-center gap-2 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 ring-1 ring-inset ring-red-200 dark:bg-red-950/40 dark:text-red-300 dark:ring-red-900"
      >
        <span className="flex-1">
          {error.code === 'assistant_busy' && error.status === 429
            ? t('errors.assistant_busy_global')
            : t(`errors.${error.code}`, { defaultValue: error.message || error.code })}
          {extra && ` ${extra}`}
        </span>
        {runningId !== null && onAttach && (
          <Button size="sm" onClick={() => onAttach(runningId)}>
            {t('assistant.showRunning')}
          </Button>
        )}
      </div>
    </div>
  );
}
