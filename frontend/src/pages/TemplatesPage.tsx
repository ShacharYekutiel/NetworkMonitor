import { useTranslation } from 'react-i18next';
import { useDeviceTemplates } from '../components/Templates';
import { Card, Empty, ErrorBox, Loading, PageHeader } from '../components/ui';
import { describeEntry, describeMatch, l10n } from '../lib/templates';

export function TemplatesPage() {
  const { t, i18n } = useTranslation();
  const q = useDeviceTemplates();
  return (
    <div className="space-y-4">
      <PageHeader title={t('templates.title')} subtitle={t('templates.subtitle')} />
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} />
      {q.data && !q.data.length && <Empty>{t('templates.none')}</Empty>}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {q.data?.map((tpl) => (
          <Card
            key={tpl.id}
            title={
              <span className="flex flex-wrap items-center gap-2">
                <bdi dir="auto">{l10n(tpl.name, i18n.language)}</bdi>
                {tpl.requires_snmp && (
                  <span className="rounded bg-sky-50 px-1.5 py-0.5 text-[11px] font-medium text-sky-800 ring-1 ring-inset ring-sky-200 dark:bg-sky-950 dark:text-sky-300 dark:ring-sky-900">
                    SNMP
                  </span>
                )}
              </span>
            }
            actions={
              <code dir="ltr" className="text-xs text-slate-500">
                {tpl.id}
              </code>
            }
          >
            <p className="text-sm text-slate-700 dark:text-slate-200">
              {l10n(tpl.description, i18n.language)}
            </p>
            <dl className="mt-3 space-y-2 text-sm">
              <div>
                <dt className="text-xs font-medium text-slate-500">{t('templates.matches')}</dt>
                <dd className="text-slate-700 dark:text-slate-300">
                  {describeMatch(tpl.match, t)}{' '}
                  <span className="text-xs text-slate-500">
                    ({t('templates.priority', { priority: tpl.priority })})
                  </span>
                </dd>
              </div>
              <div>
                <dt className="text-xs font-medium text-slate-500">{t('templates.creates')}</dt>
                <dd>
                  <ul className="ms-4 list-disc text-slate-700 dark:text-slate-300">
                    {tpl.entries.map((e, i) => (
                      <li key={i}>
                        <bdi dir="auto">{describeEntry(e, i18n.language, t)}</bdi>
                      </li>
                    ))}
                  </ul>
                </dd>
              </div>
            </dl>
          </Card>
        ))}
      </div>
    </div>
  );
}
