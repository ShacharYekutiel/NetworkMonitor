import type { TFunction } from 'i18next';
import { useTranslation } from 'react-i18next';
import type { AwsHint } from '../../api/types';
import { isolateValues } from '../../lib/aws';
import { CheckIcon, XIcon } from '../icons';
import { cx } from '../ui';
import { useAssistantErrorText } from '../assistant/common';

/** One step of an AWS chain Test (`TestStep`; the assistant's steps share the shape). */
export type ChainStep = {
  step: string;
  hop?: 0 | 1 | null;
  ok: boolean;
  detail: string;
  ms: number;
  error_code: string | null;
  hints?: AwsHint[];
};

const HINT_PLACEHOLDERS = Object.fromEntries(
  [
    'trust_anchor_arn',
    'profile_arn',
    'role_arn',
    'cert_cn',
    'issuer_cn',
    'from_arn',
    'base_role_arn',
    'source_identity',
    'region',
    'account',
    'host',
    'service',
  ].map((k) => [k, '?']),
);

/** A hint of the backend hint table (§ 15.1.7) in plain words, with the values to compare. */
export function HintItem({ h }: { h: AwsHint }) {
  const { t } = useTranslation();
  // a value the backend did not know stays a readable "?" instead of "{{x}}"
  const text = t(`aws.hint.${h.code}`, {
    ...HINT_PLACEHOLDERS,
    ...isolateValues(h.values),
    defaultValue: h.text,
  });
  const values = Object.entries(h.values);
  return (
    <li className="space-y-1" data-hint={h.code}>
      <p dir="auto">{text}</p>
      {values.length > 0 && (
        <dl className="space-y-0.5 text-[11px]" aria-label={t('aws.test.values')}>
          {values.map(([k, v]) => (
            <div key={k} className="flex flex-wrap items-baseline gap-x-2">
              <dt className="font-mono text-slate-500" dir="ltr">
                {k}
              </dt>
              <dd dir="ltr" className="min-w-0 break-all font-mono" data-value={k}>
                {v}
              </dd>
            </div>
          ))}
        </dl>
      )}
    </li>
  );
}

function stepLabel(t: TFunction, s: ChainStep): string {
  const aws = t(`aws.test.step.${s.step}`, { defaultValue: '' });
  return aws || t(`assistant.test.step.${s.step}`, { defaultValue: s.step });
}

/**
 * The step list of a chain Test: each step with its hop (base / hop), the failing step
 * highlighted with its hints (the exact values to compare) — architecture § 15.1.7.
 */
export function AwsTestSteps({ steps }: { steps: ChainStep[] }) {
  const { t } = useTranslation();
  const errText = useAssistantErrorText();
  return (
    <ol className="space-y-1" data-testid="test-steps">
      {steps.map((s, i) => (
        <li
          key={`${s.step}-${i}`}
          className={cx(
            'rounded-md px-2 py-1',
            !s.ok &&
              'bg-red-50 ring-1 ring-inset ring-red-200 dark:bg-red-950/40 dark:ring-red-900',
            s.ok && (s.hints?.length ?? 0) > 0 && 'bg-amber-50 dark:bg-amber-950/30',
          )}
          data-step={s.step}
          data-ok={s.ok}
          data-hop={s.hop ?? ''}
        >
          <div className="flex flex-wrap items-start gap-1.5">
            {s.ok ? (
              <CheckIcon size={13} className="mt-0.5 shrink-0 text-green-600" />
            ) : (
              <XIcon size={13} className="mt-0.5 shrink-0 text-red-600" />
            )}
            <span className="font-medium">{stepLabel(t, s)}</span>
            {s.hop !== undefined && s.hop !== null && (
              <span
                className={cx(
                  'rounded px-1 text-[10px] font-medium',
                  s.hop === 0
                    ? 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300'
                    : 'bg-sky-100 text-sky-800 dark:bg-sky-900 dark:text-sky-200',
                )}
              >
                {t(s.hop === 0 ? 'aws.test.hop0' : 'aws.test.hop1')}
              </span>
            )}
            <span className="text-slate-400">{s.ms} ms</span>
          </div>
          <p dir="auto" className="ps-5 text-slate-600 dark:text-slate-400">
            {s.error_code ? errText({ code: s.error_code, message: s.detail }) : s.detail}
          </p>
          {s.error_code && s.detail && (
            <bdi
              dir="ltr"
              className="block ps-5 font-mono text-[11px] text-slate-500"
              data-testid="test-step-raw"
            >
              {s.detail}
            </bdi>
          )}
          {(s.hints?.length ?? 0) > 0 && (
            <div className="mt-1 ps-5" data-testid="test-hints">
              <p className="font-medium">{t('aws.test.hints')}</p>
              <ul className="list-disc space-y-1.5 ps-4">
                {s.hints!.map((h, j) => (
                  <HintItem key={`${h.code}-${j}`} h={h} />
                ))}
              </ul>
            </div>
          )}
        </li>
      ))}
    </ol>
  );
}

/** Test warnings (`TestResult.warnings`), AWS ones first, then the assistant's. */
export function TestWarnings({ warnings }: { warnings: string[] }) {
  const { t } = useTranslation();
  return (
    <>
      {warnings.map((w) => (
        <p key={w} className="text-amber-700 dark:text-amber-300" data-warning={w}>
          {t(`aws.warning.${w}`, {
            defaultValue: t(`assistant.warning.${w}`, { defaultValue: w }),
          })}
        </p>
      ))}
    </>
  );
}
