import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { endpoints, qk } from '../../api/endpoints';
import { useLocalizeMessage } from '../../hooks/localize';
import { Drawer } from '../Drawer';
import { ChatIcon, RefreshIcon, SparkIcon } from '../icons';
import { AiTag } from '../kit';
import { Button, buttonClass, cx } from '../ui';
import { useAnswerStream } from '../../hooks/useAnswerStream';
import { formatDateTime } from '../../lib/format';
import { AnswerBlock, PreStreamErrorBox } from './AnswerView';
import { AiNotice, useAiFeature } from './common';

/**
 * [3b] "Explain" (AI) on a problem (§ 14.8.1): opens a side drawer with the streamed explanation
 * and a "Continue in Ask" link. Hidden unless `ai_explain_alert` (and `assistant`) are ON, for admins.
 */
export function ExplainButton({
  sensorId,
  eventId = null,
  size = 'sm',
  label,
}: {
  sensorId: number;
  eventId?: number | null;
  size?: 'sm' | 'md';
  label?: string;
}) {
  const { t } = useTranslation();
  const on = useAiFeature('ai_explain_alert');
  const [open, setOpen] = useState(false);
  const stream = useAnswerStream();
  if (!on) return null;
  const start = (refresh: boolean) => {
    setOpen(true);
    void stream.explain(sensorId, eventId, refresh);
  };
  return (
    <>
      <Button
        size={size}
        onClick={() => (stream.live.messageId || stream.running ? setOpen(true) : start(false))}
        title={t('explain.title')}
        data-testid="explain-button"
      >
        <SparkIcon size={size === 'sm' ? 12 : 14} className="text-accent-ink" />
        {label ?? t('explain.button')}
      </Button>
      <ExplainDrawerView
        open={open}
        onClose={() => setOpen(false)}
        stream={stream}
        onRefresh={() => start(true)}
        sensorId={sensorId}
      />
    </>
  );
}

/** What is explained (prototype .subj): the AI tag, the sensor and its device, the status line. */
function Subject({ sensorId }: { sensorId: number }) {
  const { t } = useTranslation();
  const localize = useLocalizeMessage();
  const q = useQuery({ queryKey: qk.sensor(sensorId), queryFn: () => endpoints.sensor(sensorId) });
  const s = q.data;
  if (!s) return null;
  return (
    <div className="mb-3.5 flex items-center gap-2.5 rounded-[14px] bg-chip px-3 py-2.5">
      <AiTag />
      <span className="grid min-w-0">
        <b dir="auto" className="truncate font-display text-[14.5px] font-semibold">
          {s.device_name ? `${s.device_name} · ${s.name}` : s.name}
        </b>
        <span className="truncate text-[13px] text-ink-muted">
          {t(`status.${s.status}`)}
          {s.status_message && (
            <>
              {' · '}
              <bdi dir="auto">{localize(s.status_message)}</bdi>
            </>
          )}
        </span>
      </span>
    </div>
  );
}

function ExplainDrawerView({
  open,
  onClose,
  stream,
  onRefresh,
  sensorId,
}: {
  open: boolean;
  onClose: () => void;
  stream: ReturnType<typeof useAnswerStream>;
  onRefresh: () => void;
  sensorId: number;
}) {
  const { t } = useTranslation();
  const final = stream.live.final;
  const cid = stream.live.conversationId;
  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={t('explain.title')}
      testId="explain-drawer"
      narrow
      footer={
        <div className="grid w-full gap-2.5">
          <AiNotice />
          <div className="flex flex-wrap items-center gap-2">
            {final && (
              <span className="text-[12.5px] text-ink-subtle">
                {t('explain.explainedAt', { time: formatDateTime(final.created_at) })}
              </span>
            )}
            {final && (
              <Button size="sm" onClick={onRefresh}>
                <RefreshIcon size={13} />
                {t('explain.refresh')}
              </Button>
            )}
          </div>
          {cid !== null && (
            <Link
              to={`/assistant/c/${cid}`}
              className={cx(buttonClass('secondary', 'sm'), 'justify-self-start')}
            >
              <ChatIcon size={14} />
              {t('assistant.continueInAsk')}
            </Link>
          )}
        </div>
      }
    >
      <Subject sensorId={sensorId} />
      <div className="grid gap-3">
        <PreStreamErrorBox error={stream.preError} onRetry={onRefresh} />
        {(stream.running || stream.live.messageId !== null || stream.live.error) && (
          <AnswerBlock
            live={stream.live}
            running={stream.running}
            onStop={() => void stream.stop()}
            onRetry={onRefresh}
            compact
            feedback
          />
        )}
      </div>
    </Drawer>
  );
}

/**
 * [3b] The Explain drawer without its button (tree context menu): starts the explanation of
 * `sensorId` when mounted; unmount it to close. The caller checks the feature / role.
 */
export function ExplainDrawer({ sensorId, onClose }: { sensorId: number; onClose: () => void }) {
  const stream = useAnswerStream();
  const { explain } = stream;
  useEffect(() => {
    void explain(sensorId, null, false);
  }, [explain, sensorId]);
  return (
    <ExplainDrawerView
      open
      onClose={onClose}
      stream={stream}
      onRefresh={() => void explain(sensorId, null, true)}
      sensorId={sensorId}
    />
  );
}
