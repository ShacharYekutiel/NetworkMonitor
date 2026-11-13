import { useEffect, useState, type FormEvent } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints3b, qk3b } from '../../api/endpoints3b';
import type { AssistantSettings, ChatModel, ModelListItem } from '../../api/types';
import { chatModelOptions, friendlyModelName } from '../../lib/assistant';
import { cardAnchor } from '../../lib/settingsIndex';
import { PlusIcon, TrashIcon } from '../icons';
import { useToast } from '../toast';
import { Button, Card, ErrorBox, Input } from '../ui';
import { useAssistantErrorText } from './common';

export const CHAT_MODELS_MAX = 8;
const LABEL_MAX = 60;
const MODEL_RE = /^[A-Za-z0-9.:/_-]{1,256}$/;

type Row = { id: string; label: string };

/** Client-side check of the `chat_models` list (the server validates again). */
export function chatModelsErrors(rows: Row[]): Record<number, 'invalid' | 'duplicate' | 'label'> {
  const out: Record<number, 'invalid' | 'duplicate' | 'label'> = {};
  const seen = new Set<string>();
  rows.forEach((r, i) => {
    const id = r.id.trim();
    if (!MODEL_RE.test(id)) out[i] = 'invalid';
    else if (seen.has(id)) out[i] = 'duplicate';
    else if (r.label.trim().length > LABEL_MAX) out[i] = 'label';
    seen.add(id);
  });
  return out;
}

const rowsOf = (s: AssistantSettings): Row[] =>
  (s.chat_models ?? []).map((m) => ({ id: m.id, label: m.label ?? '' }));

/**
 * [3b owner] Settings → Assistant → Chat models: the models / inference profiles an admin may
 * pick in the chat composer. Not a connection field: saving needs no new connection test.
 */
export function ChatModelsCard({ s }: { s: AssistantSettings }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const errText = useAssistantErrorText();
  const [rows, setRows] = useState<Row[]>(() => rowsOf(s));
  const [models, setModels] = useState<ModelListItem[] | null>(null);
  useEffect(() => {
    setRows(rowsOf(s));
  }, [s]);
  const errs = chatModelsErrors(rows);
  const invalid = Object.keys(errs).length > 0;
  const full = rows.length >= CHAT_MODELS_MAX;
  const save = useMutation({
    mutationFn: () =>
      endpoints3b.updateSettings({
        chat_models: rows.map((r): ChatModel => ({
          id: r.id.trim(),
          label: r.label.trim() || null,
        })),
      }),
    onSuccess: (r) => {
      qc.setQueryData(qk3b.settings, r);
      void qc.invalidateQueries({ queryKey: qk3b.status });
      toast.push('success', t('common.saved'));
    },
  });
  const listModels = useMutation({
    mutationFn: () => endpoints3b.models(),
    onSuccess: (r) => setModels(r.items),
  });
  const setRow = (i: number, patch: Partial<Row>) =>
    setRows((x) => x.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const add = (id = '') => {
    if (full || (id && rows.some((r) => r.id.trim() === id))) return;
    setRows((x) => [...x, { id, label: '' }]);
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!invalid) save.mutate();
  };
  const effective = chatModelOptions(s.chat_models_effective ?? []);

  return (
    <Card title={t('assistant.chatModels.title')} anchor={cardAnchor('assistantChatModels')}>
      <form onSubmit={submit} className="space-y-3" data-testid="assistant-chat-models">
        <p className="text-xs text-ink-muted">{t('assistant.chatModels.hint')}</p>
        {rows.length === 0 && (
          <p className="text-xs text-ink-muted" data-testid="chat-models-default">
            {t('assistant.chatModels.defaultList')}
          </p>
        )}
        <ul className="space-y-2">
          {rows.map((r, i) => (
            <li key={i} className="flex flex-wrap items-start gap-2">
              <div className="min-w-0 flex-[2_1_16rem]">
                <Input
                  value={r.id}
                  onChange={(e) => setRow(i, { id: e.target.value })}
                  dir="ltr"
                  placeholder="us.anthropic.claude-…"
                  aria-label={t('assistant.chatModels.id')}
                  aria-invalid={!!errs[i]}
                  className="font-mono text-xs"
                />
                {errs[i] ? (
                  <p className="mt-0.5 text-xs text-status-down">
                    {t(`assistant.chatModels.error.${errs[i]}`)}
                  </p>
                ) : (
                  r.id.trim() && (
                    <p className="mt-0.5 text-xs text-ink-muted">
                      {friendlyModelName(r.id.trim())}
                    </p>
                  )
                )}
              </div>
              <Input
                value={r.label}
                maxLength={LABEL_MAX}
                onChange={(e) => setRow(i, { label: e.target.value })}
                placeholder={t('assistant.chatModels.labelPlaceholder')}
                aria-label={t('assistant.chatModels.label')}
                className="min-w-0 flex-[1_1_10rem]"
              />
              <Button
                size="sm"
                variant="ghost"
                aria-label={t('assistant.chatModels.remove')}
                title={t('assistant.chatModels.remove')}
                onClick={() => setRows((x) => x.filter((_, j) => j !== i))}
              >
                <TrashIcon size={13} />
              </Button>
            </li>
          ))}
        </ul>
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" onClick={() => add()} disabled={full}>
            <PlusIcon size={12} /> {t('assistant.chatModels.add')}
          </Button>
          <Button size="sm" onClick={() => listModels.mutate()} busy={listModels.isPending}>
            {t('assistant.settings.listModels')}
          </Button>
          {full && (
            <span className="text-xs text-ink-muted">
              {t('assistant.chatModels.max', { n: CHAT_MODELS_MAX })}
            </span>
          )}
          <span className="flex-1" />
          <Button
            size="sm"
            type="submit"
            variant="primary"
            busy={save.isPending}
            disabled={invalid}
          >
            {t('common.save')}
          </Button>
        </div>
        {listModels.data?.error && (
          <p className="text-xs text-status-down">{errText(listModels.data.error)}</p>
        )}
        <ErrorBox error={listModels.error} />
        {models && (
          <div className="max-h-48 overflow-y-auto rounded-md border border-line bg-surface">
            {models.length === 0 && (
              <p className="p-2 text-xs text-ink-muted">{t('assistant.settings.noModels')}</p>
            )}
            {models.map((m) => (
              <button
                key={m.id}
                type="button"
                disabled={full || rows.some((r) => r.id.trim() === m.id)}
                onClick={() => add(m.id)}
                className="flex w-full flex-wrap items-center gap-2 px-2 py-1 text-start text-xs hover:bg-accent-soft disabled:opacity-50"
              >
                <span dir="ltr" className="font-mono">
                  {m.id}
                </span>
                <span className="text-ink-muted">{m.name}</span>
              </button>
            ))}
          </div>
        )}
        <ErrorBox error={save.error} />
        {effective.length > 0 && (
          <p className="text-xs text-ink-muted" data-testid="chat-models-effective">
            {t('assistant.chatModels.effective')}{' '}
            {effective.map((m, i) => (
              <span key={m.id}>
                {i > 0 && ', '}
                <span title={m.id}>{m.name}</span>
                {i === 0 && ` (${t('assistant.chatModels.default')})`}
              </span>
            ))}
          </p>
        )}
      </form>
    </Card>
  );
}
