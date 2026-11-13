import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { endpoints3b, qk3b } from '../api/endpoints3b';
import type { AssistantConversation, AssistantMessage } from '../api/types';
import { AnswerBlock, PreStreamErrorBox } from '../components/assistant/AnswerView';
import { AiNotice, AssistantStateBadge, useAssistantStatus } from '../components/assistant/common';
import { EditIcon, PlusIcon, SparkIcon, TrashIcon } from '../components/icons';
import {
  Button,
  buttonClass,
  CHIP,
  CHIP_TONE,
  ConfirmDialog,
  cx,
  ErrorBox,
  Input,
  Loading,
  PageHeader,
  Select,
  Textarea,
} from '../components/ui';
import { useAuth } from '../hooks/auth';
import { FeatureOffNotice, useFeatures } from '../hooks/features';
import { useLiveMessages } from '../hooks/live';
import { useAnswerStream } from '../hooks/useAnswerStream';
import {
  chatModelOptions,
  pickChatModel,
  readChatModel,
  rememberChatModel,
  remainingQuestions,
  visibleExamples,
} from '../lib/assistant';
import { formatRelative } from '../lib/format';

const MAX_Q = 2000;

function ConversationList({ activeId, onNew }: { activeId: number | null; onNew: () => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [q, setQ] = useState('');
  const [editing, setEditing] = useState<AssistantConversation | null>(null);
  const [title, setTitle] = useState('');
  const [deleting, setDeleting] = useState<AssistantConversation | 'all' | null>(null);
  const list = useQuery({
    queryKey: qk3b.conversations({ q }),
    queryFn: () => endpoints3b.conversations({ q: q || undefined, page_size: 100 }),
    placeholderData: keepPreviousData,
  });
  const rename = useMutation({
    mutationFn: () => endpoints3b.renameConversation(editing!.id, title.trim()),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['assistant-conversations'] });
      setEditing(null);
    },
  });
  const del = useMutation({
    mutationFn: async () => {
      if (deleting === 'all') await endpoints3b.deleteAllConversations();
      else if (deleting) await endpoints3b.deleteConversation(deleting.id);
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['assistant-conversations'] });
      const wasActive = deleting === 'all' || (deleting && deleting.id === activeId);
      setDeleting(null);
      if (wasActive) navigate('/assistant');
    },
  });
  return (
    <aside
      className="flex min-h-0 flex-col rounded-lg border border-line bg-surface lg:w-72 lg:shrink-0"
      aria-label={t('assistant.history')}
    >
      <div className="flex items-center gap-2 border-b border-line p-2">
        <Button size="sm" variant="primary" onClick={onNew}>
          <PlusIcon size={12} /> {t('assistant.newConversation')}
        </Button>
        <Link to="/assistant/usage" className="ms-auto text-xs text-accent-ink hover:underline">
          {t('assistant.usageLink')}
        </Link>
      </div>
      <div className="p-2">
        <Input
          type="search"
          value={q}
          placeholder={t('assistant.searchHistory')}
          aria-label={t('assistant.searchHistory')}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>
      <ul
        className="max-h-64 min-h-0 flex-1 overflow-y-auto lg:max-h-none"
        data-testid="conversation-list"
      >
        {list.data?.items.map((c) =>
          editing?.id === c.id ? (
            <li key={c.id} className="flex items-center gap-1 px-2 py-1">
              <Input
                value={title}
                maxLength={200}
                autoFocus
                aria-label={t('assistant.rename')}
                onChange={(e) => setTitle(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && title.trim()) rename.mutate();
                  if (e.key === 'Escape') setEditing(null);
                }}
              />
              <Button
                size="sm"
                busy={rename.isPending}
                disabled={!title.trim()}
                onClick={() => rename.mutate()}
              >
                {t('common.save')}
              </Button>
            </li>
          ) : (
            <li
              key={c.id}
              className={cx(
                'group flex items-center gap-1 px-2 py-1.5 text-sm',
                c.id === activeId ? 'bg-accent-soft' : 'hover:bg-surface-sunken',
              )}
            >
              <Link to={`/assistant/c/${c.id}`} className="min-w-0 flex-1">
                <span dir="auto" className="block truncate">
                  {c.title || t('assistant.untitled')}
                </span>
                <span className="block text-[11px] text-ink-subtle">
                  {c.kind !== 'chat' && `${t(`assistant.kind.${c.kind}`)} · `}
                  {c.channel === 'telegram' && 'Telegram · '}
                  {formatRelative(c.last_message_at)}
                </span>
              </Link>
              <button
                type="button"
                className="rounded p-1 text-ink-subtle hover:text-ink-muted"
                aria-label={t('assistant.rename')}
                title={t('assistant.rename')}
                onClick={() => {
                  setEditing(c);
                  setTitle(c.title);
                }}
              >
                <EditIcon size={13} />
              </button>
              <button
                type="button"
                className="rounded p-1 text-ink-subtle hover:text-status-down"
                aria-label={t('common.delete')}
                title={t('common.delete')}
                onClick={() => setDeleting(c)}
              >
                <TrashIcon size={13} />
              </button>
            </li>
          ),
        )}
        {list.data && !list.data.items.length && (
          <li className="p-3 text-center text-xs text-ink-muted">
            {t('assistant.noConversations')}
          </li>
        )}
      </ul>
      <ErrorBox error={list.error} className="m-2" />
      {!!list.data?.items.length && (
        <div className="border-t border-line p-2 text-end">
          <Button size="sm" variant="ghost" onClick={() => setDeleting('all')}>
            {t('assistant.deleteAll')}
          </Button>
        </div>
      )}
      <ConfirmDialog
        open={!!deleting}
        title={deleting === 'all' ? t('assistant.deleteAll') : t('assistant.deleteConversation')}
        message={
          deleting === 'all'
            ? t('assistant.deleteAllConfirm')
            : t('common.deleteConfirm', { name: deleting?.title ?? '' })
        }
        onConfirm={() => del.mutate()}
        onClose={() => {
          setDeleting(null);
          del.reset();
        }}
        busy={del.isPending}
        error={del.error}
      />
    </aside>
  );
}

function UserBubble({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div
        dir="auto"
        className="max-w-[85%] whitespace-pre-wrap rounded-lg border border-line bg-surface-sunken px-3.5 py-2 text-sm text-ink"
        data-testid="user-question"
      >
        {text}
      </div>
    </div>
  );
}

/** /assistant and /assistant/c/:id — "Ask the network" (admins, feature `assistant`). */
export function AssistantPage() {
  const { t } = useTranslation();
  const { features, loaded } = useFeatures();
  const params = useParams();
  const conversationId = params.id ? Number(params.id) : null;
  const navigate = useNavigate();
  const qc = useQueryClient();
  const status = useAssistantStatus();
  const { user } = useAuth();
  const [question, setQuestion] = useState('');
  // [3b owner] model choice: remembered per user (localStorage), default = the main model
  const [chosenModel, setChosenModel] = useState<string | null>(() => readChatModel(user?.id));
  useEffect(() => {
    setChosenModel(readChatModel(user?.id));
  }, [user?.id]);
  const [pending, setPending] = useState<string | null>(null);
  const lastQuestion = useRef('');
  const bottom = useRef<HTMLDivElement>(null);
  const stream = useAnswerStream({
    onStart: (cid) => {
      if (cid !== conversationId) navigate(`/assistant/c/${cid}`, { replace: true });
      void qc.invalidateQueries({ queryKey: ['assistant-conversations'] });
    },
  });
  const detail = useQuery({
    queryKey: qk3b.conversation(conversationId ?? 0),
    queryFn: () => endpoints3b.conversation(conversationId!),
    enabled: conversationId !== null && features.assistant,
  });

  // finished stream → reload the stored conversation (authoritative content)
  const finalId = stream.live.final?.id;
  useEffect(() => {
    if (!finalId || !stream.live.conversationId) return;
    void qc.invalidateQueries({ queryKey: qk3b.conversation(stream.live.conversationId) });
    void qc.invalidateQueries({ queryKey: ['assistant-conversations'] });
    void qc.invalidateQueries({ queryKey: qk3b.status });
  }, [finalId, stream.live.conversationId, qc]);

  // other tabs: a message of mine finished
  useLiveMessages(['assistant.message'], (m) => {
    if (m.type !== 'assistant.message') return;
    void qc.invalidateQueries({ queryKey: ['assistant-conversations'] });
    void qc.invalidateQueries({ queryKey: qk3b.conversation(m.data.conversation_id) });
  });

  // re-attach to a running answer of this conversation (page reload, other tab, back navigation)
  const msgs = detail.data?.messages ?? [];
  const runningMsg = [...msgs]
    .reverse()
    .find((m) => m.role === 'assistant' && m.status === 'running');
  const { attach } = stream;
  useEffect(() => {
    if (runningMsg && stream.live.messageId !== runningMsg.id && !stream.running)
      void attach(runningMsg.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runningMsg?.id]);

  // new conversation: forget the live state when the user navigates elsewhere
  const { reset } = stream;
  useEffect(() => {
    if (stream.live.conversationId && conversationId !== stream.live.conversationId) {
      reset();
      setPending(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' });
  }, [msgs.length, stream.live.text.length, stream.live.steps.length]);

  if (!loaded) return <Loading />;
  if (!features.assistant) return <FeatureOffNotice feature="assistant" />;

  const modelOptions = chatModelOptions(status.data?.chat_models ?? []);
  const model = pickChatModel(modelOptions, chosenModel, status.data?.default_model_id);
  const send = (text: string) => {
    const q = text.trim();
    if (!q || q.length > MAX_Q || stream.running) return;
    lastQuestion.current = q;
    setPending(q);
    setQuestion('');
    void stream.ask(conversationId, q, model);
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    send(question);
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send(question);
    }
  };

  const liveId = stream.live.messageId;
  // stored messages; the one being streamed is rendered from the live state
  const shown: AssistantMessage[] = msgs;
  const liveInList = liveId !== null && shown.some((m) => m.id === liveId);
  const showPending = pending !== null && !liveInList;
  const remaining = remainingQuestions(status.data?.me);
  const examples = visibleExamples(features);
  const empty = !conversationId && !pending && !stream.live.messageId;

  return (
    <div className="flex h-full flex-col gap-4">
      <PageHeader
        title={
          <span className="inline-flex items-center gap-2">
            <SparkIcon className="text-accent-ink" /> {t('assistant.title')}
          </span>
        }
        subtitle={status.data && <AssistantStateBadge state={status.data.state} />}
      />
      <div className="flex min-h-0 flex-1 flex-col gap-4 lg:flex-row">
        <ConversationList
          activeId={conversationId}
          onNew={() => {
            stream.reset();
            setPending(null);
            navigate('/assistant');
          }}
        />
        <section className="flex min-h-[60vh] min-w-0 flex-1 flex-col rounded-lg border border-line bg-surface">
          <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4" data-testid="conversation">
            {empty && (
              <div className="space-y-3">
                <p className="text-sm text-ink-muted">{t('assistant.intro')}</p>
                <div className="flex flex-wrap gap-2" data-testid="examples">
                  {examples.map((ex) => (
                    <button
                      key={ex.key}
                      type="button"
                      onClick={() => send(t(`assistant.example.${ex.key}`))}
                      className={cx(
                        CHIP,
                        CHIP_TONE.neutral,
                        'h-auto min-h-[26px] whitespace-normal py-1 text-start hover:border-accent-ink hover:text-ink',
                      )}
                    >
                      {t(`assistant.example.${ex.key}`)}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {detail.isLoading && <Loading />}
            <ErrorBox error={detail.error} />
            {shown.map((m) =>
              m.role === 'user' ? (
                <UserBubble key={m.id} text={m.content} />
              ) : (
                <AnswerBlock
                  key={m.id}
                  message={m.id === liveId ? stream.live.final : m}
                  live={m.id === liveId ? stream.live : null}
                  running={m.id === liveId && stream.running}
                  onStop={() => void stream.stop()}
                  onRetry={() => send(lastQuestion.current || previousQuestion(shown, m))}
                />
              ),
            )}
            {showPending && <UserBubble text={pending} />}
            {(stream.running || stream.live.messageId) && !liveInList && (
              <AnswerBlock
                live={stream.live}
                running={stream.running}
                onStop={() => void stream.stop()}
                onRetry={() => send(lastQuestion.current)}
              />
            )}
            <PreStreamErrorBox error={stream.preError} onAttach={(id) => void stream.attach(id)} />
            <div ref={bottom} />
          </div>
          <form onSubmit={submit} className="space-y-1 border-t border-line p-3">
            <Textarea
              rows={3}
              value={question}
              maxLength={MAX_Q}
              dir="auto"
              placeholder={t('assistant.placeholder')}
              aria-label={t('assistant.question')}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={onKey}
              data-testid="question"
            />
            <div className="flex flex-wrap items-center gap-2 text-xs text-ink-muted">
              <span className="tabular-nums">
                {question.length} / {MAX_Q}
              </span>
              {remaining !== null && (
                <span data-testid="remaining">
                  {t('assistant.remaining', { count: remaining })}
                </span>
              )}
              <span className="hidden sm:inline">{t('assistant.enterHint')}</span>
              <span className="flex-1" />
              {model && (
                <label className="inline-flex items-center gap-1">
                  <span>{t('assistant.model')}</span>
                  <Select
                    value={model}
                    onChange={(e) => {
                      setChosenModel(e.target.value);
                      rememberChatModel(user?.id, e.target.value);
                    }}
                    disabled={stream.running || modelOptions.length < 2}
                    className="w-auto max-w-[14rem] py-1 text-xs"
                    data-testid="chat-model"
                    title={model}
                  >
                    {modelOptions.map((m) => (
                      <option key={m.id} value={m.id} title={m.id}>
                        {m.name}
                      </option>
                    ))}
                  </Select>
                </label>
              )}
              {stream.running ? (
                <Button size="sm" onClick={() => void stream.stop()}>
                  {t('assistant.stop')}
                </Button>
              ) : (
                <Button
                  size="sm"
                  type="submit"
                  variant="primary"
                  disabled={!question.trim() || remaining === 0}
                >
                  {t('assistant.send')}
                </Button>
              )}
            </div>
            <AiNotice />
          </form>
        </section>
      </div>
    </div>
  );
}

function previousQuestion(list: AssistantMessage[], m: AssistantMessage): string {
  const i = list.findIndex((x) => x.id === m.id);
  for (let j = i - 1; j >= 0; j--) if (list[j].role === 'user') return list[j].content;
  return '';
}

/** Link to the Ask page, styled as a button (used by drawers and incident cards). */
export function ContinueInAskLink({ conversationId }: { conversationId: number }) {
  const { t } = useTranslation();
  return (
    <Link to={`/assistant/c/${conversationId}`} className={buttonClass('secondary', 'sm')}>
      {t('assistant.continueInAsk')}
    </Link>
  );
}
