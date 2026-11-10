import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../../api/endpoints';
import { endpoints3, qk3 } from '../../api/endpoints3';
import type { NotificationChannel, NotificationType } from '../../api/types';
import { useFeatures } from '../../hooks/features';
import { useToast } from '../toast';
import { Button, Checkbox, ErrorBox } from '../ui';

/** [3] Channel form: "Acknowledge" buttons/links (feature chat_ack), Telegram poller state, take over. */
export function ChatAckFields({
  type,
  channel,
  ackButtons,
  ackLink,
  onAckButtons,
  onAckLink,
}: {
  type: NotificationType;
  channel?: NotificationChannel;
  ackButtons: boolean;
  ackLink: boolean;
  onAckButtons: (v: boolean) => void;
  onAckLink: (v: boolean) => void;
}) {
  const { t } = useTranslation();
  const { features } = useFeatures();
  const qc = useQueryClient();
  const toast = useToast();
  const isTelegram = type === 'telegram' && channel?.type === 'telegram';
  const poll = useQuery({
    queryKey: qk3.telegramStatus,
    queryFn: endpoints3.telegramStatus,
    enabled: features.chat_ack && isTelegram,
  });
  const settings = useQuery({
    queryKey: qk.settings,
    queryFn: endpoints.settings,
    enabled: features.chat_ack && (type === 'teams' || type === 'email'),
  });
  const noPublicUrl = settings.data !== undefined && !settings.data.public_base_url;
  const poller = poll.data?.pollers.find((p) => channel && p.channel_ids.includes(channel.id));
  const take = useMutation({
    mutationFn: () => endpoints3.takeOverBot((channel as NotificationChannel).id),
    onSuccess: () => {
      toast.push('success', t('chatAck.takenOver'));
      void qc.invalidateQueries({ queryKey: qk3.telegramStatus });
    },
  });
  if (!features.chat_ack) return null;
  return (
    <fieldset
      className="space-y-2 rounded-md border border-slate-200 p-3 dark:border-slate-700"
      data-testid="chat-ack-fields"
    >
      <legend className="px-1 text-xs font-semibold text-slate-600 dark:text-slate-400">
        {t('chatAck.channelTitle')}
      </legend>
      {type === 'telegram' && (
        <>
          <Checkbox label={t('chatAck.ackButtons')} checked={ackButtons} onChange={onAckButtons} />
          <p className="text-xs text-slate-500">{t('chatAck.ackButtonsHint')}</p>
          <Checkbox
            label={t('chatAck.ackLinkTelegram')}
            checked={ackLink}
            disabled={ackButtons}
            onChange={onAckLink}
          />
          {isTelegram && poller && (
            <p
              className={
                poller.state === 'error' ? 'text-xs text-red-600' : 'text-xs text-slate-500'
              }
            >
              {t('chatAck.pollerLine', {
                bot: poller.bot_username,
                state: t(`chatAck.pollerState.${poller.state}`),
              })}
              {poller.last_error && (
                <bdi dir="auto" className="ms-1">
                  — {poller.last_error}
                </bdi>
              )}
            </p>
          )}
          {isTelegram && (
            <div className="space-y-1">
              <Button size="sm" busy={take.isPending} onClick={() => take.mutate()}>
                {t('chatAck.takeOver')}
              </Button>
              <p className="text-xs text-slate-500">{t('chatAck.takeOverHint')}</p>
              <ErrorBox error={take.error} />
            </div>
          )}
        </>
      )}
      {(type === 'teams' || type === 'email') && (
        <>
          <Checkbox label={t('chatAck.ackLink')} checked={ackLink} onChange={onAckLink} />
          <p className="text-xs text-slate-500">
            {type === 'teams' ? t('chatAck.teamsLimit') : t('chatAck.emailHint')}
          </p>
          {ackLink && noPublicUrl && (
            <p
              className="text-xs text-amber-700 dark:text-amber-300"
              data-testid="ack-needs-public-url"
            >
              {t('chatAck.needsPublicUrl')}
            </p>
          )}
        </>
      )}
    </fieldset>
  );
}
