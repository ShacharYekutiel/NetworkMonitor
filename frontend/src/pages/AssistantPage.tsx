import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link, useNavigate, useParams } from 'react-router';
import { endpoints3b, qk3b } from '../api/endpoints3b';
import type { AssistantConversation, AssistantMessage } from '../api/types';
import { AnswerBlock, PreStreamErrorBox } from '../components/assistant/AnswerView';
import { AssistantStateBadge, useAssistantStatus } from '../components/assistant/common';
import {
  ChatIcon,
  ChevronDownIcon,
  EditIcon,
  GaugeIcon,
  PlusIcon,
  SearchIcon,
  SendIcon,
  SparkIcon,
  StopIcon,
  TrashIcon,
} from '../components/icons';
import { Banner } from '../components/kit';
import {
  BackLink,
  Button,
  buttonClass,
  ConfirmDialog,
  cx,
  ErrorBox,
  Input,
  Loading,
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

/** Day buckets of the history list (prototype .hist h5): today, yesterday, this week, earlier. */
function dayBucket(iso: string, now = new Date()): 'today' | 'yesterday' | 'week' | 'earlier' {
  const d = new Date(iso);
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const t = d.getTime();
  if (t >= start) return 'today';
  if (t >= start - 86_400_000) return 'yesterday';
  if (t >= start - 6 * 86_400_000) return 'week';
  return 'earlier';
}

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
  const items = list.data?.items ?? [];
  const buckets: { key: string; items: AssistantConversation[] }[] = [];
  for (const c of items) {
    const k = dayBucket(c.last_message_at);
    const last = buckets[buckets.length - 1];
    if (last?.key === k) last.items.push(c);
    else buckets.push({ key: k, items: [c] });
  }
  return (
    // prototype .hist: search + "New", the conversations by day in one card, "Delete all" under it
    <aside
      className="grid min-w-0 content-start gap-2.5 lg:sticky lg:top-[80px]"
      aria-label={t('assistant.history')}
    >
      <div className="flex gap-2">
        <label className="flex h-[42px] min-w-0 flex-1 items-center gap-2 rounded-[14px] bg-surface px-3.5 shadow-card">
          <SearchIcon size={16} className="shrink-0 text-ink-subtle" />
          <input
            type="search"
            value={q}
            placeholder={t('assistant.searchHistory')}
            aria-label={t('assistant.searchHistory')}
            onChange={(e) => setQ(e.target.value)}
            className="h-full w-0 min-w-0 flex-1 bg-transparent text-sm text-ink outline-none placeholder:text-ink-subtle"
          />
        </label>
        <Button variant="primary" onClick={onNew} className="!h-[42px] !px-3.5">
          <PlusIcon size={16} /> {t('assistant.newConversation')}
        </Button>
      </div>
      <div
        className="max-h-[calc(100vh-230px)] overflow-y-auto rounded-xl bg-surface shadow-card max-lg:max-h-64"
        data-testid="conversation-list"
      >
        {buckets.map((bk) => (
          <section key={bk.key}>
            <h5 className="m-0 mx-2 mb-1 mt-3 px-1.5 text-[12.5px] font-semibold text-ink-subtle">
              {t(`assistant.day.${bk.key}`)}
            </h5>
            <ul className="m-0 list-none p-0">
              {bk.items.map((c) =>
                editing?.id === c.id ? (
                  <li key={c.id} className="flex items-center gap-1.5 px-2.5 py-2">
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
                      'group relative flex items-center gap-1 pe-2 transition-colors',
                      c.id === activeId ? 'bg-chip' : 'hover:bg-surface-hover',
                    )}
                  >
                    <Link
                      to={`/assistant/c/${c.id}`}
                      aria-current={c.id === activeId ? 'page' : undefined}
                      className="grid min-w-0 flex-1 py-[11px] ps-3.5 text-ink no-underline"
                    >
                      <b dir="auto" className="truncate text-[14.5px] font-medium">
                        {c.title || t('assistant.untitled')}
                      </b>
                      <span className="truncate text-[13px] text-ink-muted">
                        {t(`assistant.kind.${c.kind}`)}
                        {c.channel === 'telegram' && ' · Telegram'}
                        {' · '}
                        {formatRelative(c.last_message_at)}
                      </span>
                    </Link>
                    <span className="flex shrink-0 gap-0.5 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
                      <button
                        type="button"
                        className="inline-flex h-7 w-7 items-center justify-center rounded-full text-ink-subtle hover:bg-chip hover:text-ink"
                        aria-label={t('assistant.rename')}
                        title={t('assistant.rename')}
                        onClick={() => {
                          setEditing(c);
                          setTitle(c.title);
                        }}
                      >
                        <EditIcon size={14} />
                      </button>
                      <button
                        type="button"
                        className="inline-flex h-7 w-7 items-center justify-center rounded-full text-ink-subtle hover:bg-status-down-soft hover:text-status-down"
                        aria-label={t('common.delete')}
                        title={t('common.delete')}
                        onClick={() => setDeleting(c)}
                      >
                        <TrashIcon size={14} />
                      </button>
                    </span>
                  </li>
                ),
              )}
            </ul>
          </section>
        ))}
        {list.data && !items.length && (
          <p className="m-0 p-5 text-center text-sm text-ink-muted">
            {t('assistant.noConversations')}
          </p>
        )}
        <ErrorBox error={list.error} className="m-2" />
      </div>
      {!!items.length && (
        <Button
          size="sm"
          variant="danger"
          className="justify-self-start"
          onClick={() => setDeleting('all')}
        >
          {t('assistant.deleteAll')}
        </Button>
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

/** The question (prototype .msg.mine): an ink pill at the end side. */
function UserBubble({ text }: { text: string }) {
  return (
    <div
      dir="auto"
      className="max-w-[76%] justify-self-end whitespace-pre-wrap rounded-[22px] rounded-es-lg bg-accent px-[18px] py-3 text-[15.5px] text-accent-on"
      data-testid="user-question"
    >
      {text}
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

  const modelName = modelOptions.find((m) => m.id === model)?.name ?? null;
  const capped = remaining === 0;
  const me = status.data?.me;
  return (
    <div>
      <BackLink />
      <div className="mt-2 grid items-start gap-6 lg:grid-cols-[290px_minmax(0,1fr)]">
        <ConversationList
          activeId={conversationId}
          onNew={() => {
            stream.reset();
            setPending(null);
            navigate('/assistant');
          }}
        />
        {/* prototype .convo: head, messages in a centred 820px column, the sticky composer */}
        <section className="grid min-h-[calc(100vh-180px)] min-w-0 grid-rows-[auto_1fr_auto] gap-[22px]">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
              <h1 className="m-0 text-[28px] font-semibold tracking-[-0.02em]">
                {t('assistant.title')}
              </h1>
              {status.data && <AssistantStateBadge state={status.data.state} />}
            </div>
            <span className="flex items-center gap-2">
              {modelName && (
                <span className="text-[13px] text-ink-muted">
                  <bdi dir="ltr">{modelName} · Amazon Bedrock</bdi>
                </span>
              )}
              <Link to="/assistant/usage" className={buttonClass('secondary', 'sm')}>
                <GaugeIcon size={15} />
                {t('assistant.usageLink')}
              </Link>
            </span>
          </div>
          <div
            className="grid w-full max-w-[820px] content-start gap-[22px] justify-self-center"
            data-testid="conversation"
          >
            {empty && (
              <div className="grid justify-items-center gap-2.5 pb-4 pt-[6vh] text-center">
                <span className="grid h-[68px] w-[68px] place-items-center rounded-[22px] bg-accent text-accent-on">
                  <SparkIcon size={30} />
                </span>
                <h2 className="m-0 mt-1.5 text-[40px] font-semibold leading-[1.1] tracking-[-0.025em]">
                  {t('assistant.title')}
                </h2>
                <p className="m-0 max-w-[560px] text-ink-muted">{t('assistant.intro')}</p>
                <div
                  className="mt-[18px] grid w-full max-w-[720px] gap-2.5 sm:grid-cols-2"
                  data-testid="examples"
                >
                  {examples.map((ex) => (
                    <button
                      key={ex.key}
                      type="button"
                      onClick={() => send(t(`assistant.example.${ex.key}`))}
                      disabled={capped}
                      className="flex items-start gap-2.5 rounded-[18px] bg-surface px-4 py-3.5 text-start text-[14.5px] text-ink shadow-card transition-transform duration-200 hover:-translate-y-0.5 disabled:opacity-50 motion-reduce:hover:translate-y-0"
                    >
                      <ChatIcon size={16} className="mt-[3px] shrink-0 text-ink-subtle" />
                      <span dir="auto">{t(`assistant.example.${ex.key}`)}</span>
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
                  modelName={modelName}
                  onRetry={() => send(lastQuestion.current || previousQuestion(shown, m))}
                />
              ),
            )}
            {showPending && <UserBubble text={pending} />}
            {(stream.running || stream.live.messageId) && !liveInList && (
              <AnswerBlock
                live={stream.live}
                running={stream.running}
                modelName={modelName}
                onRetry={() => send(lastQuestion.current)}
              />
            )}
            <PreStreamErrorBox
              error={stream.preError}
              onAttach={(id) => void stream.attach(id)}
              onRetry={() => send(lastQuestion.current)}
            />
            <div ref={bottom} />
          </div>
          <div className="sticky bottom-[18px] grid w-full max-w-[820px] gap-2.5 justify-self-center">
            {capped && (
              <Banner
                tone="warn"
                title={t('assistant.capTitle')}
                actions={
                  <Link to="/assistant/usage" className={buttonClass('secondary', 'sm')}>
                    {t('assistant.usageLink')}
                  </Link>
                }
              >
                {me && t('assistant.capBody', { used: me.questions_today, limit: me.daily_limit })}
              </Banner>
            )}
            <form
              onSubmit={submit}
              className={cx(
                'grid gap-2.5 rounded-[26px] bg-surface pb-3 pe-3.5 ps-[18px] pt-3.5 shadow-[0_10px_40px_rgba(0,0,0,0.10),0_0_0_1px_var(--border)]',
                capped && 'opacity-60',
              )}
            >
              <textarea
                rows={1}
                value={question}
                maxLength={MAX_Q}
                dir="auto"
                placeholder={t('assistant.placeholder')}
                aria-label={t('assistant.question')}
                onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={onKey}
                disabled={capped}
                data-testid="question"
                className="max-h-40 min-h-[26px] w-full resize-none border-0 bg-transparent px-1 text-base leading-normal text-ink outline-none [field-sizing:content] placeholder:text-ink-subtle disabled:cursor-not-allowed"
              />
              <div className="flex flex-wrap items-center gap-2.5">
                {model && (
                  <label className="relative inline-flex">
                    <span className="sr-only">{t('assistant.model')}</span>
                    <select
                      value={model}
                      onChange={(e) => {
                        setChosenModel(e.target.value);
                        rememberChatModel(user?.id, e.target.value);
                      }}
                      disabled={stream.running || modelOptions.length < 2}
                      className="h-8 max-w-[14rem] cursor-pointer appearance-none truncate rounded-full border-0 bg-chip pe-8 ps-3 text-[13px] font-medium text-ink outline-none focus-visible:shadow-[0_0_0_2px_var(--focus)] disabled:cursor-default"
                      data-testid="chat-model"
                      title={model}
                    >
                      {modelOptions.map((m) => (
                        <option key={m.id} value={m.id} title={m.id}>
                          {m.name}
                        </option>
                      ))}
                    </select>
                    {modelOptions.length > 1 && (
                      <ChevronDownIcon
                        size={14}
                        className="pointer-events-none absolute end-2.5 top-1/2 -translate-y-1/2 text-ink-muted"
                      />
                    )}
                  </label>
                )}
                <span className="min-w-0 flex-1 text-[12.5px] text-ink-subtle">
                  <span className="max-sm:hidden">{t('assistant.enterHint')}</span>
                  {remaining !== null && (
                    <>
                      <span className="max-sm:hidden"> · </span>
                      <span data-testid="remaining">
                        {t('assistant.remaining', { count: remaining })}
                      </span>
                    </>
                  )}
                  {question.length > MAX_Q - 200 && (
                    <span className="num">
                      {' · '}
                      {question.length} / {MAX_Q}
                    </span>
                  )}
                </span>
                {stream.running ? (
                  <button
                    type="button"
                    onClick={() => void stream.stop()}
                    data-testid="stop"
                    aria-label={t('assistant.stop')}
                    title={t('assistant.stop')}
                    className="grid h-[38px] w-[38px] shrink-0 place-items-center rounded-full bg-accent text-accent-on"
                  >
                    <StopIcon size={14} className="fill-current" />
                  </button>
                ) : (
                  <button
                    type="submit"
                    disabled={!question.trim() || capped}
                    title={t('assistant.send')}
                    className="grid h-[38px] w-[38px] shrink-0 place-items-center rounded-full bg-accent text-accent-on transition-opacity disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    <SendIcon size={18} />
                    <span className="sr-only">{t('assistant.send')}</span>
                  </button>
                )}
              </div>
            </form>
          </div>
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
