import { useTranslation } from 'react-i18next';
import { ChevronIcon } from './icons';
import { Button } from './ui';

export function Pagination({
  page,
  pageSize,
  total,
  onPage,
}: {
  page: number;
  pageSize: number;
  total: number;
  onPage: (p: number) => void;
}) {
  const { t } = useTranslation();
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const from = total ? (page - 1) * pageSize + 1 : 0;
  const to = Math.min(total, page * pageSize);
  return (
    <div className="flex items-center justify-between gap-2 border-t border-line px-3.5 py-2 text-xs text-ink-muted">
      <span>{t('pagination.range', { from, to, total })}</span>
      <div className="flex items-center gap-1">
        <Button
          size="sm"
          disabled={page <= 1}
          onClick={() => onPage(page - 1)}
          aria-label={t('pagination.prev')}
        >
          <ChevronIcon size={14} className="rotate-180 rtl:rotate-0" />
        </Button>
        <span className="px-2 tabular-nums text-ink">{t('pagination.page', { page, pages })}</span>
        <Button
          size="sm"
          disabled={page >= pages}
          onClick={() => onPage(page + 1)}
          aria-label={t('pagination.next')}
        >
          <ChevronIcon size={14} className="rtl:rotate-180" />
        </Button>
      </div>
    </div>
  );
}
