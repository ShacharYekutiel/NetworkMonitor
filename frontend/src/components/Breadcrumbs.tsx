import { Fragment } from 'react';
import { Link } from 'react-router-dom';
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
  return (
    <nav aria-label={t('nav.breadcrumbs')} className="flex flex-wrap items-center gap-1">
      {items.map((it, i) => (
        <Fragment key={i}>
          {i > 0 && <ChevronIcon size={10} className="text-slate-400 rtl:rotate-180" />}
          {it.to ? (
            <Link to={it.to} className="hover:underline">
              {it.label}
            </Link>
          ) : (
            <span>{it.label}</span>
          )}
        </Fragment>
      ))}
    </nav>
  );
}
