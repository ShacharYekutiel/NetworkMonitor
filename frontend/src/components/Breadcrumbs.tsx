import { Fragment } from 'react';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useTree } from '../hooks/queries';
import { groupPath } from '../lib/tree';
import { ChevronIcon } from './icons';

export function Breadcrumbs({
  groupId,
  extra,
}: {
  groupId: number | null | undefined;
  extra?: { to?: string; label: string }[];
}) {
  const { t } = useTranslation();
  const { data } = useTree();
  const path = groupId && data ? groupPath(data.groups, groupId) : [];
  const items: { to?: string; label: string }[] = [
    { to: '/', label: t('nav.dashboard') },
    ...path.map((g) => ({ to: `/groups/${g.id}`, label: g.name })),
    ...(extra ?? []),
  ];
  // iOS-style back row: "‹ Overview / HQ / Core"; the chevron goes to the nearest parent.
  const parent = [...items].reverse().find((it) => it.to);
  return (
    <nav
      aria-label={t('nav.breadcrumbs')}
      className="-ms-2 flex min-h-9 flex-wrap items-center gap-x-1.5 gap-y-1 text-[15px]"
    >
      {parent?.to && (
        <Link
          to={parent.to}
          aria-hidden="true"
          tabIndex={-1}
          className="inline-flex h-9 items-center rounded-[10px] ps-1 text-ink hover:bg-chip"
        >
          <ChevronIcon size={18} strokeWidth={2} className="rotate-180 rtl:rotate-0" />
        </Link>
      )}
      {items.map((it, i) => (
        <Fragment key={i}>
          {i > 0 && <span className="text-ink-subtle">/</span>}
          {it.to ? (
            <Link
              to={it.to}
              className={
                it === parent
                  ? 'font-medium text-ink no-underline hover:underline'
                  : 'text-ink-muted no-underline hover:text-ink hover:underline'
              }
            >
              {it.label}
            </Link>
          ) : (
            <span className="text-ink-muted">{it.label}</span>
          )}
        </Fragment>
      ))}
    </nav>
  );
}
