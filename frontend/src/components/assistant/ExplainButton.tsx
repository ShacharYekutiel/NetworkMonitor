import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { Drawer } from '../Drawer';
import { SparkIcon } from '../icons';
import { Button, buttonClass } from '../ui';
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
        <SparkIcon size={size === 'sm' ? 12 : 14} className="text-violet-600" />
        {label ?? t('explain.button')}
      </Button>
      <ExplainDrawerView
        open={open}
        onClose={() => setOpen(false)}
        stream={stream}
        onRefresh={() => start(true)}
      />
    </>
  );
}

function ExplainDrawerView({
  open,
  onClose,
  stream,
  onRefresh,
}: {
  open: boolean;
  onClose: () => void;
  stream: ReturnType<typeof useAnswerStream>;
  onRefresh: () => void;
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
      footer={
        <>
          {final && (
            <span className="me-auto self-center text-xs text-slate-500">
              {t('explain.explainedAt', { time: formatDateTime(final.created_at) })}
            </span>
          )}
          {final && (
            <Button size="sm" onClick={onRefresh}>
              {t('explain.refresh')}
            </Button>
          )}
          {cid !== null && (
            <Link to={`/assistant/c/${cid}`} className={buttonClass('secondary', 'sm')}>
              {t('assistant.continueInAsk')}
            </Link>
          )}
        </>
      }
    >
      <div className="space-y-3">
        <AiNotice />
        <PreStreamErrorBox error={stream.preError} />
        {(stream.running || stream.live.messageId !== null || stream.live.error) && (
          <AnswerBlock
            live={stream.live}
            running={stream.running}
            onStop={() => void stream.stop()}
            onRetry={onRefresh}
            compact
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
    />
  );
}
