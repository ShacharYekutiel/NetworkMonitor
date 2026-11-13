import { useTranslation } from 'react-i18next';
import type { CheckResult } from '../api/types';
import { formatNumber, formatValue } from '../lib/format';
import { StatusBadge, StatusDot } from './Status';
import { Ltr, Value } from './ui';
import { useLocalizeMessage } from '../hooks/localize';

export function CheckResultView({ result }: { result: CheckResult }) {
  const { t } = useTranslation();
  const localize = useLocalizeMessage();
  return (
    <div className="space-y-2 rounded-md bg-surface-sunken p-3 text-sm ring-1 ring-line">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={result.status} />
        <span className="font-medium">{result.ok ? t('check.ok') : t('check.failed')}</span>
        <span className="text-ink-muted">
          <Ltr>{formatNumber(result.duration_ms, 0)} ms</Ltr>
        </span>
      </div>
      {result.message && (
        <div dir="auto" className="text-ink-muted">
          {localize(result.message)}
        </div>
      )}
      {result.channels.length > 0 && (
        <ul className="grid grid-cols-1 gap-1 sm:grid-cols-2">
          {result.channels.map((c) => (
            <li key={c.key} className="flex items-center gap-2">
              <StatusDot status={c.status} />
              <span className="text-ink-muted">
                {t(`channel.${c.key}`, { defaultValue: c.key })}:
              </span>
              <span className="tabular-nums">
                <Value text={formatValue(c.value, c.unit)} />
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
