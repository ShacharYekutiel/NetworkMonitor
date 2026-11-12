import { useAiFeature } from '../assistant/common';
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints3, qk3 } from '../../api/endpoints3';
import type { ChatLinkCode } from '../../api/types3';
import { useFeatures } from '../../hooks/features';
import { formatDateTime } from '../../lib/format';
import { useToast } from '../toast';
import { Button, Card, Empty, ErrorBox, Modal } from '../ui';

function CodeDialog({ code, onClose }: { code: ChatLinkCode | null; onClose: () => void }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  if (!code) return null;
  return (
    <Modal
      open
      onClose={onClose}
      title={t('chatAck.codeTitle')}
      footer={
        <Button variant="primary" onClick={onClose}>
          {t('common.close')}
        </Button>
      }
    >
      <div className="space-y-3 text-sm" data-testid="link-code-dialog">
        <p>
          {code.bot_usernames.length
            ? t('chatAck.codeStep1', { bot: code.bot_usernames.map((b) => `@${b}`).join(' / ') })
            : t('chatAck.codeNoBot')}
        </p>
        <div className="flex items-center gap-2">
          <code
            dir="ltr"
            className="flex-1 rounded bg-slate-100 px-3 py-2 text-center font-mono text-lg dark:bg-slate-800"
          >
            {code.instructions}
          </code>
          <Button
            onClick={() =>
              void navigator.clipboard?.writeText(code.instructions).then(() => setCopied(true))
            }
          >
            {copied ? t('kiosk.copied') : t('kiosk.copy')}
          </Button>
        </div>
        <p className="text-xs text-slate-500">
          {t('chatAck.codeExpires', { time: formatDateTime(code.expires_at) })}
        </p>
        <p className="text-xs text-slate-500">{t('chatAck.codeGroupHint')}</p>
      </div>
    </Modal>
  );
}

/** [3] My profile → Chat accounts: link a Telegram account with a one-time code. */
export function ChatAccountsCard() {
  const { t } = useTranslation();
  const { features } = useFeatures();
  const qc = useQueryClient();
  const toast = useToast();
  const [code, setCode] = useState<ChatLinkCode | null>(null);
  const askOn = useAiFeature('ai_chat_ask');
  const q = useQuery({
    queryKey: qk3.chatLinks(false),
    queryFn: () => endpoints3.chatLinks(false),
    enabled: features.chat_ack,
  });
  const create = useMutation({
    mutationFn: endpoints3.createLinkCode,
    onSuccess: setCode,
  });
  const unlink = useMutation({
    mutationFn: (id: number) => endpoints3.deleteChatLink(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['chat-links'] });
      toast.push('success', t('chatAck.unlinked'));
    },
  });
  if (!features.chat_ack) return null;
  return (
    <Card
      title={t('chatAck.accounts')}
      actions={
        <Button size="sm" variant="primary" busy={create.isPending} onClick={() => create.mutate()}>
          {t('chatAck.linkTelegram')}
        </Button>
      }
    >
      <p className="mb-3 text-xs text-slate-500">{t('chatAck.accountsIntro')}</p>
      {/* [3b] Telegram /ask (linked admins, private chat) */}
      {askOn && (
        <p className="mb-3 text-xs text-violet-700 dark:text-violet-300" data-testid="ask-info">
          {t('assistant.chatAsk.accountInfo')}
        </p>
      )}
      <ErrorBox error={q.error ?? create.error ?? unlink.error} />
      {q.data && !q.data.items.length && <Empty>{t('chatAck.noLinks')}</Empty>}
      <ul className="divide-y divide-slate-100 text-sm dark:divide-slate-800">
        {q.data?.items.map((l) => (
          <li key={l.id} className="flex flex-wrap items-center gap-2 py-1.5">
            <span className="font-medium">Telegram</span>
            <bdi dir="ltr">{l.display}</bdi>
            <span className="text-xs text-slate-500">
              {t('chatAck.linkedAt', { date: formatDateTime(l.linked_at) })}
              {l.last_used_at &&
                ` · ${t('chatAck.lastUsed', { date: formatDateTime(l.last_used_at) })}`}
            </span>
            <Button
              size="sm"
              variant="ghost"
              className="ms-auto"
              onClick={() => unlink.mutate(l.id)}
            >
              {t('chatAck.unlink')}
            </Button>
          </li>
        ))}
      </ul>
      <CodeDialog
        code={code}
        onClose={() => {
          setCode(null);
          void qc.invalidateQueries({ queryKey: ['chat-links'] });
        }}
      />
    </Card>
  );
}
