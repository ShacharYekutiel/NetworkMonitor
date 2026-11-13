import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { HEALTH_CATEGORIES, type HealthItem } from '../../api/types';
import { healthText } from '../../lib/ports';
import { STATUS_DOT, statusClass } from '../../lib/status';
import { cx, Value } from '../ui';

/** One chip per health item in the fixed order; click opens the worst sensor. Hidden when empty. */
export function HealthStrip({ items }: { items: HealthItem[] }) {
  const { t } = useTranslation();
  if (!items.length) return null;
  const sorted = [...items].sort(
    (a, b) => HEALTH_CATEGORIES.indexOf(a.category) - HEALTH_CATEGORIES.indexOf(b.category),
  );
  return (
    <div className="flex flex-wrap gap-2" data-testid="health-strip">
      {sorted.map((h) => (
        <Link
          key={h.category}
          to={`/sensors/${h.primary_sensor_id}`}
          title={t(`status.${h.status}`)}
          className="inline-flex min-w-0 items-center gap-2 rounded-md bg-surface px-2.5 py-1.5 text-sm ring-1 ring-line hover:bg-surface-hover"
        >
          <span
            className={cx('h-2.5 w-2.5 shrink-0 rounded-full', statusClass(h.status, STATUS_DOT))}
            aria-hidden="true"
          />
          <span className="text-xs text-ink-muted">{t(`health.${h.category}`)}</span>
          <Value text={healthText(h)} className="font-semibold tabular-nums" />
          <span className="sr-only">{t(`status.${h.status}`)}</span>
        </Link>
      ))}
    </div>
  );
}
