import { useMemo, useState, type MouseEvent, type ReactNode } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link, useNavigate } from 'react-router';
import { endpoints3b, qk3b } from '../../api/endpoints3b';
import type { AssistantMessage, AssistantSource, AssistantStep } from '../../api/types';
import {
  argsSummary,
  compactTokens,
  copyableMarkdown,
  formatCost,
  friendlyModelName,
  isRetryable,
  kbSourceUrl,
  nmRoute,
  type LiveAnswer,
} from '../../lib/assistant';
import { renderAnswer } from '../../lib/markdown';
import {
  AlertIcon,
  BackupIcon,
  CheckIcon,
  ChevronIcon,
  ClockIcon,
  CopyIcon,
  DeviceIcon,
  FlowIcon,
  FolderIcon,
  IncidentIcon,
  LayersIcon,
  LogsIcon,
  MapIcon,
  PortIcon,
  SensorIcon,
  SparkIcon,
  StopIcon,
  ThumbIcon,
  WrenchIcon,
  XIcon,
} from '../icons';
import { useAuth } from '../../hooks/auth';
import { AiTag, Banner } from '../kit';
import { useToast } from '../toast';
import { Button, buttonClass, cx, Spinner, Textarea } from '../ui';
import { useAssistantErrorText } from './common';

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
    <li className="text-[13px]" data-testid="tool-step" data-tool={s.tool} data-state={s.state}>
      {/* prototype .tstep: ✓ (or a spinner), the step, rows · ms at the end */}
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        disabled={!hasArgs}
        aria-expanded={hasArgs ? open : undefined}
        className="flex w-full items-center gap-2 text-start text-ink-muted disabled:cursor-default"
      >
        {s.state === 'running' ? (
          <Spinner className="h-[15px] w-[15px] text-ink-subtle" />
        ) : s.ok ? (
          <CheckIcon size={15} className="shrink-0 text-status-up" />
        ) : (
          <XIcon size={15} className="shrink-0 text-status-down" />
        )}
        <span className="min-w-0">{label}</span>
        {s.state === 'done' && (
          <span className="num ms-auto text-xs text-ink-subtle [unicode-bidi:plaintext]">
            {s.ok
              ? t('assistant.stepDone', { rows: s.rows ?? 0, ms: s.ms ?? 0 })
              : t('assistant.stepFailed', { error: s.error ?? '' })}
            {s.truncated && ` · ${t('assistant.truncated')}`}
          </span>
        )}
        {hasArgs && (
          <ChevronIcon
            size={12}
            className={cx(
              'shrink-0 text-ink-subtle transition-transform',
              s.state !== 'done' && 'ms-auto',
              open ? 'rotate-90' : 'rtl:rotate-180',
            )}
          />
        )}
      </button>
      {open && hasArgs && (
        <code
          dir="ltr"
          className="mt-1 block whitespace-pre-wrap break-all rounded-[10px] bg-chip px-2.5 py-1.5 font-mono text-[11.5px]"
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
    <ul className="m-0 grid list-none gap-1 p-0">
      {steps.map((s) => (
        <StepRow key={s.call_id} s={s} />
      ))}
    </ul>
  );
}

const SOURCE_ICON: Record<string, IconC> = {
  device: DeviceIcon,
  sensor: SensorIcon,
  group: FolderIcon,
  port: PortIcon,
  event: ClockIcon,
  syslog: LogsIcon,
  trap: LogsIcon,
  maintenance: WrenchIcon,
  config: BackupIcon,
  'config-diff': BackupIcon,
  topology: MapIcon,
  incident: IncidentIcon,
  flows: FlowIcon,
  kb: LayersIcon,
};
type IconC = (p: { size?: number; className?: string }) => ReactNode;

/** Source chips under an answer (prototype .srcs / .src): number, kind icon, label. */
function Sources({ sources }: { sources: AssistantSource[] }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  if (!sources.length) return null;
  const chip =
    'inline-flex h-[30px] max-w-full items-center gap-[7px] rounded-full bg-surface pe-3 ps-2.5 text-[13px] font-medium text-ink no-underline shadow-[0_1px_2px_rgba(0,0,0,0.06),0_0_0_1px_var(--border)]';
  return (
    <div className="flex flex-wrap gap-2" data-testid="answer-sources">
      <span className="sr-only">{t('assistant.sources')}:</span>
      {sources.map((s, i) => {
        const route = s.ref.startsWith('kb/') ? null : nmRoute(s.ref);
        const ext = s.ref.startsWith('kb/') ? kbSourceUrl(s.ref, sources) : null;
        const Icon = SOURCE_ICON[s.ref.slice(0, s.ref.indexOf('/'))] ?? SparkIcon;
        const inner = (
          <>
            <i className="num text-[11px] font-semibold not-italic text-ink-subtle">{i + 1}</i>
            <Icon size={15} className="shrink-0 text-ink-muted" />
            <bdi dir="auto" className="truncate">
              {s.label}
            </bdi>
          </>
        );
        return route ? (
          <a
            key={s.ref}
            href={route}
            onClick={(e) => {
              e.preventDefault();
              navigate(route);
            }}
            className={cx(chip, 'hover:bg-surface-hover')}
          >
            {inner}
          </a>
        ) : ext ? (
          <a
            key={s.ref}
            href={ext}
            target="_blank"
            rel="noopener noreferrer nofollow"
            className={cx(chip, 'hover:bg-surface-hover')}
          >
            {inner}
          </a>
        ) : (
          <span key={s.ref} className={cx(chip, 'text-ink-muted')}>
            {inner}
            {s.url && (
              <bdi dir="ltr" className="text-ink-subtle">
                ({s.url.split('/').pop()})
              </bdi>
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
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <button
        type="button"
        aria-pressed={m.feedback === 1}
        aria-label={t('assistant.thumbsUp')}
        title={t('assistant.thumbsUp')}
        onClick={() => rate(1)}
        className={cx(ROUND, m.feedback === 1 && ROUND_ON)}
        disabled={mut.isPending}
      >
        <ThumbIcon size={16} />
      </button>
      <button
        type="button"
        aria-pressed={m.feedback === -1}
        aria-label={t('assistant.thumbsDown')}
        title={t('assistant.thumbsDown')}
        onClick={() => rate(-1)}
        className={cx(ROUND, m.feedback === -1 && ROUND_ON)}
        disabled={mut.isPending}
      >
        <ThumbIcon size={16} className="rotate-180" />
      </button>
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

/** Round 30px icon buttons of the answer footer (prototype .ameta .round, .thumbs .round.on). */
const ROUND =
  'inline-flex h-[30px] w-[30px] items-center justify-center rounded-full text-ink-muted transition-colors hover:bg-chip hover:text-ink disabled:opacity-50';
const ROUND_ON = '!bg-accent !text-accent-on';

/** Footer: tokens, ≈ cost, duration (the model is named on the answer's first line). */
function MetaLine({ m }: { m: AssistantMessage }) {
  const { t } = useTranslation();
  const tokens = m.usage.input_tokens + m.usage.output_tokens;
  return (
    <span className="num text-[12.5px] text-ink-subtle" data-testid="answer-meta">
      {t('assistant.tokens', { n: compactTokens(tokens) })}
      {m.cost_estimate !== null && (
        <>
          {' · '}
          <bdi dir="ltr">≈ {formatCost(m.cost_estimate, m.currency)}</bdi>
        </>
      )}
      {m.duration_ms !== null && (
        <>
          {' · '}
          <bdi dir="ltr">{(m.duration_ms / 1000).toFixed(1)} s</bdi>
        </>
      )}
    </span>
  );
}

/**
 * One assistant answer (prototype .msg.ai): "AI ✦ model" line, dimmed notes of tool rounds, tool
 * steps, the Markdown answer (live while streaming), notices, error with Retry, source chips, and
 * the footer (fine print, tokens, copy, feedback).
 */
export function AnswerBlock({
  live,
  message,
  running,
  onStop,
  onRetry,
  compact,
  feedback,
  modelName,
}: {
  live?: LiveAnswer | null;
  message?: AssistantMessage | null;
  running?: boolean;
  onStop?: () => void;
  onRetry?: () => void;
  compact?: boolean;
  /** compact, but with 👍/👎 (the Explain panel) */
  feedback?: boolean;
  /** the model's readable name while no stored message names it yet */
  modelName?: string | null;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const { isAdmin } = useAuth();
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
  const model = final?.model_id ? friendlyModelName(final.model_id) : modelName;
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
    <div
      className={cx(
        'grid gap-3',
        compact ? 'text-[15px] leading-[1.6]' : 'text-[15.5px] leading-[1.7]',
      )}
      data-testid="answer"
      data-status={final?.status ?? 'running'}
    >
      {!compact && (
        <div className="flex items-center gap-2 text-[13px] text-ink-muted">
          <AiTag />
          {model && (
            <bdi dir="ltr" title={final?.model_id ?? undefined}>
              {model}
            </bdi>
          )}
        </div>
      )}
      {notes.map((n) => (
        <div key={n.round} className="opacity-60" data-testid="round-note">
          <Markdown
            text={n.text}
            allowedRefs={allowed}
            sources={final?.sources}
            className="!text-[14px]"
          />
        </div>
      ))}
      <ToolSteps steps={steps} />
      {text ? (
        <Markdown
          text={text}
          allowedRefs={allowed}
          sources={final?.sources}
          className="!text-[length:inherit] !leading-[inherit]"
        />
      ) : (
        isRunning && (
          <p className="m-0 flex items-center gap-2 text-sm text-ink-muted">
            <Spinner className="h-4 w-4" /> {t('assistant.thinking')}
          </p>
        )
      )}
      {notices.map((n, i) => (
        <p
          key={i}
          className={cx(
            'm-0 flex items-center gap-2.5 rounded-[14px] px-3.5 py-2.5 text-sm',
            n.code === 'retrying' ? 'bg-chip text-ink-muted' : 'bg-status-warning-soft text-ink',
          )}
          data-testid="answer-notice"
        >
          {n.code === 'retrying' && isRunning ? (
            <Spinner className="h-4 w-4 shrink-0" />
          ) : (
            <AlertIcon size={16} className="shrink-0 text-status-warning" />
          )}
          {t(`assistant.notice.${n.code}`, { defaultValue: n.message || n.code })}
        </p>
      ))}
      {final?.status === 'cancelled' && (
        <p className="m-0 flex items-center gap-2.5 rounded-[14px] bg-chip px-3.5 py-2.5 text-sm text-ink-muted">
          <StopIcon size={14} className="shrink-0" />
          {t('assistant.cancelled')}
        </p>
      )}
      {error && (
        <div data-testid="answer-error" data-code={error.code}>
          <Banner
            tone="down"
            title={t('assistant.answerFailed')}
            actions={
              <>
                {onRetry && isRetryable(error.code) && !isRunning && (
                  <Button size="sm" onClick={onRetry}>
                    {t('common.retry')}
                  </Button>
                )}
                {isAdmin && !isRetryable(error.code) && (
                  <Link to="/settings?tab=assistant" className={buttonClass('secondary', 'sm')}>
                    {t('assistant.toSettings')}
                  </Link>
                )}
              </>
            }
          >
            {errText(error)}
          </Banner>
        </div>
      )}
      {final && <Sources sources={final.sources} />}
      <div className="flex flex-wrap items-center gap-1.5 text-[12.5px] text-ink-subtle">
        {isRunning && onStop && (
          <Button size="sm" onClick={onStop} data-testid="stop">
            <StopIcon size={12} /> {t('assistant.stop')}
          </Button>
        )}
        {final && (
          <>
            {!compact && <span data-testid="ai-notice">{t('assistant.aiShort')}</span>}
            {!compact && <span aria-hidden="true">·</span>}
            <MetaLine m={final} />
          </>
        )}
        <span className="flex-1" />
        {final && final.content && !compact && (
          <button
            type="button"
            className={ROUND}
            onClick={() => void copy()}
            aria-label={t('assistant.copy')}
            title={t('assistant.copy')}
          >
            <CopyIcon size={16} />
          </button>
        )}
        {final && (!compact || feedback) && <Feedback m={final} />}
      </div>
    </div>
  );
}

/** Errors before the stream started (busy, caps, conversation full, …). */
export function PreStreamErrorBox({
  error,
  onAttach,
  onRetry,
}: {
  error: {
    code: string;
    message: string;
    status: number;
    details: Record<string, unknown>[];
  } | null;
  onAttach?: (messageId: number) => void;
  /** offered for the busy / throttled errors */
  onRetry?: () => void;
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
  const busy = error.code === 'assistant_busy' || error.code === 'bedrock_throttled';
  const cap = error.code === 'assistant_cap_reached';
  return (
    <div data-testid="prestream-error" data-code={error.code}>
      <Banner
        tone={busy || cap ? 'warn' : 'down'}
        title={
          error.code === 'assistant_busy' && error.status === 429
            ? t('errors.assistant_busy_global')
            : t(`errors.${error.code}`, { defaultValue: error.message || error.code })
        }
        actions={
          (runningId !== null && onAttach) || (busy && runningId === null && onRetry) ? (
            <>
              {runningId !== null && onAttach && (
                <Button size="sm" onClick={() => onAttach(runningId)}>
                  {t('assistant.showRunning')}
                </Button>
              )}
              {busy && runningId === null && onRetry && (
                <Button size="sm" onClick={onRetry}>
                  {t('common.retry')}
                </Button>
              )}
            </>
          ) : undefined
        }
      >
        {extra || undefined}
      </Banner>
    </div>
  );
}
