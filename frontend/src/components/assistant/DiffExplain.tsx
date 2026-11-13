import { useEffect, useRef } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../../api/endpoints';
import { endpoints3b, qk3b } from '../../api/endpoints3b';
import type { DiffFlag, RiskSeverity } from '../../api/types';
import { formatCost } from '../../lib/assistant';
import { SparkIcon } from '../icons';
import { Button, cx, ErrorBox, Spinner, TAG, TAG_TONE } from '../ui';
import { AiBadge, AiNotice, useAiFeature, useAssistantErrorText } from './common';

const SEV_CLS: Record<RiskSeverity, string> = {
  high: TAG_TONE.down,
  medium: TAG_TONE.warning,
  info: TAG_TONE.neutral,
};

export function SeverityChip({ severity }: { severity: RiskSeverity }) {
  const { t } = useTranslation();
  return <span className={cx(TAG, SEV_CLS[severity])}>{t(`diffRisk.severity.${severity}`)}</span>;
}

const SEV_ORDER: Record<RiskSeverity, number> = { high: 0, medium: 1, info: 2 };

/**
 * [3b] "Explain changes" panel on the configuration diff (§ 14.8.4): deterministic risk flags
 * (always, no model call) + the AI explanation on request (or automatically with
 * `config_diff_auto`). `onLines` scrolls the diff to the flagged lines.
 */
export function DiffExplain({
  deviceId,
  from,
  to,
  onLines,
}: {
  deviceId: number;
  from: number;
  to: number;
  onLines?: (lines: number[]) => void;
}) {
  const { t } = useTranslation();
  const on = useAiFeature('ai_config_diff');
  const errText = useAssistantErrorText();
  const settings = useQuery({ queryKey: qk.settings, queryFn: endpoints.settings, enabled: on });
  const flags = useQuery({
    queryKey: qk3b.diffFlags(deviceId, from, to),
    queryFn: () => endpoints3b.diffFlags(deviceId, from, to),
    enabled: on,
  });
  const explain = useMutation({
    mutationFn: (refresh: boolean) =>
      endpoints3b.explainDiff({ device_id: deviceId, from, to, refresh }),
  });
  const autoKey = useRef('');
  const { mutate, reset } = explain;
  useEffect(() => {
    reset();
    const k = `${deviceId}/${from}/${to}`;
    if (on && settings.data?.config_diff_auto && autoKey.current !== k) {
      autoKey.current = k;
      mutate(false);
    }
  }, [on, settings.data?.config_diff_auto, deviceId, from, to, mutate, reset]);
  if (!on) return null;
  const list: DiffFlag[] = [...(explain.data?.flags ?? flags.data?.flags ?? [])].sort(
    (a, b) => SEV_ORDER[a.severity] - SEV_ORDER[b.severity],
  );
  const ai = explain.data?.ai;
  return (
    <div className="space-y-2 border-b border-line p-3" data-testid="diff-explain">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold">{t('diffRisk.title')}</h3>
        {flags.isLoading && <Spinner className="h-3.5 w-3.5" />}
        <span className="flex-1" />
        <Button
          size="sm"
          onClick={() => explain.mutate(!!explain.data)}
          busy={explain.isPending}
          data-testid="diff-explain-button"
        >
          <SparkIcon size={12} className="text-accent-ink" />
          {explain.data ? t('diffRisk.refresh') : t('diffRisk.explain')}
        </Button>
      </div>
      <ErrorBox error={flags.error} />
      {flags.data && !list.length && (
        <p className="text-xs text-ink-muted">{t('diffRisk.noFlags')}</p>
      )}
      {list.length > 0 && (
        <ul className="space-y-1" data-testid="diff-flags">
          {list.map((f, i) => (
            <li key={`${f.rule_id}-${i}`} className="flex flex-wrap items-center gap-2 text-sm">
              <SeverityChip severity={f.severity} />
              <button
                type="button"
                className="text-start hover:underline"
                onClick={() => onLines?.(f.lines)}
                data-rule={f.rule_id}
              >
                {t(f.title_key, {
                  defaultValue: t(`diffRisk.${f.rule_id}`, { defaultValue: f.rule_id }),
                })}
              </button>
              <span className="text-xs text-ink-muted">
                {t(`diffRisk.side.${f.side}`)}
                {f.lines.length > 0 && (
                  <>
                    {' · '}
                    <bdi dir="ltr">
                      {t('diffRisk.lines', { lines: f.lines.slice(0, 6).join(', ') })}
                    </bdi>
                  </>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
      <ErrorBox error={explain.error} />
      {explain.data?.status === 'failed' && explain.data.error && (
        <p role="alert" className="text-sm text-status-down">
          {errText(explain.data.error)}
        </p>
      )}
      {ai && (
        <div className="space-y-2 rounded-md bg-accent-soft p-2" data-testid="diff-ai">
          <div className="flex items-center gap-2 text-xs text-ink-muted">
            <AiBadge />
            {t('diffRisk.confidence', { level: t(`confidence3.${ai.confidence}`) })}
            {explain.data?.cost_estimate != null &&
              ` · ≈ ${formatCost(explain.data.cost_estimate, null)}`}
          </div>
          <ul className="list-disc space-y-0.5 ps-5 text-sm">
            {ai.summary.map((s, i) => (
              <li key={i} dir="auto">
                {s}
              </li>
            ))}
          </ul>
          {ai.risks.length > 0 && (
            <ul className="space-y-1">
              {ai.risks.map((r, i) => (
                <li key={i} className="text-sm">
                  <span className="inline-flex flex-wrap items-center gap-2">
                    <SeverityChip severity={r.severity} />
                    <button
                      type="button"
                      className="text-start font-medium hover:underline"
                      onClick={() => onLines?.(r.lines)}
                      dir="auto"
                    >
                      {r.title}
                    </button>
                  </span>
                  <p className="text-xs text-ink-muted" dir="auto">
                    {r.explanation}
                  </p>
                </li>
              ))}
            </ul>
          )}
          {ai.truncated && <p className="text-xs text-status-warning">{t('diffRisk.truncated')}</p>}
          <AiNotice />
        </div>
      )}
    </div>
  );
}
