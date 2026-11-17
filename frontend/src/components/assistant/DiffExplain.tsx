import { useEffect, useRef } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../../api/endpoints';
import { endpoints3b, qk3b } from '../../api/endpoints3b';
import type { DiffFlag, RiskSeverity } from '../../api/types';
import { formatCost } from '../../lib/assistant';
import { CheckIcon, RefreshIcon, SparkIcon } from '../icons';
import { AiTag } from '../kit';
import { Button, cx, ErrorBox, Spinner, TAG, TAG_TONE } from '../ui';
import { AiNotice, useAiFeature, useAssistantErrorText } from './common';

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
    // prototype "סקירת השינוי": a card with the flags as rows (tag, what, where, "to line"),
    // the AI text under them, "explain again" at the end
    <section
      className="grid gap-1 rounded-xl bg-surface px-[22px] pb-5 pt-4 shadow-card"
      data-testid="diff-explain"
      aria-labelledby="diff-explain-title"
    >
      <header className="flex min-h-[40px] flex-wrap items-center gap-2.5">
        <h2 id="diff-explain-title" className="m-0 text-[17px] font-semibold">
          {t('diffRisk.title')}
        </h2>
        {ai && <AiTag />}
        {ai && (
          <span className="text-[13px] text-ink-muted">
            {t('diffRisk.confidence', { level: t(`confidence3.${ai.confidence}`) })}
            {explain.data?.cost_estimate != null &&
              ` · ≈ ${formatCost(explain.data.cost_estimate, null)}`}
          </span>
        )}
        {flags.isLoading && <Spinner className="h-3.5 w-3.5" />}
        <span className="flex-1" />
        <Button
          size="sm"
          onClick={() => explain.mutate(!!explain.data)}
          busy={explain.isPending}
          data-testid="diff-explain-button"
        >
          {explain.data ? <RefreshIcon size={13} /> : <SparkIcon size={13} />}
          {explain.data ? t('diffRisk.refresh') : t('diffRisk.explain')}
        </Button>
      </header>
      <ErrorBox error={flags.error} />
      {flags.data && !list.length && (
        <div className="flex items-center gap-2.5 rounded-[14px] bg-status-up-soft px-3.5 py-3 text-sm">
          <CheckIcon size={16} className="text-status-up" />
          {t('diffRisk.noFlags')}
        </div>
      )}
      {list.length > 0 && (
        <ul
          className="m-0 list-none p-0 [&>li+li]:border-t [&>li+li]:border-line"
          data-testid="diff-flags"
        >
          {list.map((f, i) => (
            <li
              key={`${f.rule_id}-${i}`}
              className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-3 py-3"
            >
              <SeverityChip severity={f.severity} />
              <span className="grid gap-0.5">
                <b className="font-medium">
                  {t(f.title_key, {
                    defaultValue: t(`diffRisk.${f.rule_id}`, { defaultValue: f.rule_id }),
                  })}
                </b>
                <span className="text-[13px] text-ink-muted">
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
              </span>
              {f.lines.length > 0 && onLines && (
                <Button size="sm" onClick={() => onLines(f.lines)} data-rule={f.rule_id}>
                  {t('diffRisk.toLine')}
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      <ErrorBox error={explain.error} />
      {explain.data?.status === 'failed' && explain.data.error && (
        <p role="alert" className="m-0 text-sm text-status-down">
          {errText(explain.data.error)}
        </p>
      )}
      {ai && (
        <div className="grid gap-3 border-t border-line pt-3" data-testid="diff-ai">
          <ul className="m-0 grid gap-1.5 ps-5 leading-relaxed">
            {ai.summary.map((s, i) => (
              <li key={i} dir="auto">
                {s}
              </li>
            ))}
          </ul>
          {ai.risks.length > 0 && (
            <ul className="m-0 grid list-none gap-2.5 p-0">
              {ai.risks.map((r, i) => (
                <li key={i} className="grid gap-1">
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
                  <p className="m-0 text-[13.5px] text-ink-muted" dir="auto">
                    {r.explanation}
                  </p>
                </li>
              ))}
            </ul>
          )}
          {ai.truncated && (
            <p className="m-0 text-xs text-status-warning">{t('diffRisk.truncated')}</p>
          )}
          <AiNotice />
        </div>
      )}
    </section>
  );
}
