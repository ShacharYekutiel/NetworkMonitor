import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { DeviceTemplate, TemplateLibraryStatus, TemplateLoadError } from '../api/types';
import { RefreshIcon } from '../components/icons';
import { TemplateDraftButton } from '../components/assistant/TemplateDraftWizard';
import { ConfidenceChip, useTemplateLibrary, VerifiedBadge } from '../components/Templates';
import { Button, Card, cx, Empty, ErrorBox, Loading, Notice, PageHeader } from '../components/ui';
import { useAuth } from '../hooks/auth';
import { formatDateTime } from '../lib/format';
import {
  describeEntry,
  describeMatch,
  describeWhen,
  entryConfidence,
  groupByVendor,
  isolateLtrRuns,
  l10n,
  vendorLabel,
} from '../lib/templates';

const chip =
  'inline-flex items-center whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-medium ring-1 ring-inset';

function SourceBadge({ tpl }: { tpl: DeviceTemplate }) {
  const { t } = useTranslation();
  if (!tpl.source) return null;
  const user = tpl.source === 'user';
  return (
    <span
      title={tpl.file || undefined}
      className={cx(
        chip,
        user
          ? 'bg-violet-50 text-violet-800 ring-violet-200 dark:bg-violet-950/50 dark:text-violet-300 dark:ring-violet-900'
          : 'bg-slate-50 text-slate-600 ring-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700',
      )}
    >
      {t(`templates.source.${tpl.source}`)}
      {user && tpl.overrides_builtin && ` · ${t('templates.overrides')}`}
    </span>
  );
}

function ErrorsPanel({ errors }: { errors: TemplateLoadError[] }) {
  const { t } = useTranslation();
  if (!errors.length) return null;
  return (
    <div
      role="alert"
      className="rounded-md bg-red-50 p-3 ring-1 ring-inset ring-red-200 dark:bg-red-950/40 dark:ring-red-900"
      data-testid="template-errors"
    >
      <div className="mb-2 text-sm font-medium text-red-800 dark:text-red-300">
        {t('templates.loadErrors', { count: errors.length })}
      </div>
      <ul className="space-y-1.5 text-xs">
        {errors.map((e, i) => (
          <li key={`${e.file}-${i}`} className="min-w-0">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
              <span
                className={cx(
                  chip,
                  'bg-white text-slate-600 ring-slate-200 dark:bg-slate-900 dark:text-slate-300 dark:ring-slate-700',
                )}
              >
                {t(`templates.source.${e.source}`)}
              </span>
              <code dir="ltr" className="break-all font-medium text-red-900 dark:text-red-200">
                {e.file}
                {e.path && <span className="text-red-700 dark:text-red-400"> · {e.path}</span>}
              </code>
            </div>
            <div
              dir="ltr"
              className="mt-0.5 break-words text-left text-red-700 rtl:text-right dark:text-red-300"
            >
              {e.message}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function LibraryCard({ lib, isAdmin }: { lib: TemplateLibraryStatus; isAdmin: boolean }) {
  const { t } = useTranslation();
  return (
    <Card title={t('templates.library')}>
      <div className="space-y-3 text-sm">
        <p className="text-slate-700 dark:text-slate-300">
          {t('templates.libraryLine', {
            builtin: lib.builtin_count,
            user: lib.user_count,
            errors: lib.errors.length,
          })}{' '}
          <span className="text-xs text-slate-500">
            · {t('templates.loadedAt', { time: formatDateTime(lib.loaded_at) })}
          </span>
        </p>
        {lib.overridden.length > 0 && (
          <p className="text-xs text-slate-600 dark:text-slate-300">
            {t('templates.overriddenList')}:{' '}
            {lib.overridden.map((id, i) => (
              <span key={id}>
                {i > 0 && ', '}
                <code dir="ltr">{id}</code>
              </span>
            ))}
          </p>
        )}
        {isAdmin && lib.user_dir && (
          <div className="text-xs text-slate-600 dark:text-slate-300">
            <div>
              {t('templates.userDir')}:{' '}
              <code
                dir="ltr"
                className="break-all rounded bg-slate-100 px-1 py-0.5 dark:bg-slate-800"
              >
                {lib.user_dir}
              </code>
            </div>
            <p className="mt-1 text-slate-500">{t('templates.userDirHint')}</p>
          </div>
        )}
        {isAdmin && <ErrorsPanel errors={lib.errors} />}
      </div>
    </Card>
  );
}

function TemplateCard({ tpl }: { tpl: DeviceTemplate }) {
  const { t, i18n } = useTranslation();
  return (
    <Card
      title={
        <span className="flex flex-wrap items-center gap-1.5">
          <bdi dir="auto">{l10n(tpl.name, i18n.language)}</bdi>
          {tpl.requires_snmp && (
            <span
              className={cx(
                chip,
                'bg-sky-50 text-sky-800 ring-sky-200 dark:bg-sky-950 dark:text-sky-300 dark:ring-sky-900',
              )}
            >
              SNMP
            </span>
          )}
          {tpl.verified !== undefined && <VerifiedBadge tpl={tpl} />}
          <SourceBadge tpl={tpl} />
          {tpl.version !== undefined && (
            <span className="text-[11px] font-normal text-slate-500">
              {t('templates.version', { version: tpl.version })}
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
      {l10n(tpl.description, i18n.language) && (
        <p className="mb-3 text-sm text-slate-700 dark:text-slate-200">
          {l10n(tpl.description, i18n.language)}
        </p>
      )}
      {tpl.verified && tpl.verified_note && (
        <p className="-mt-2 mb-3 text-xs text-green-700 dark:text-green-400">
          {t('templates.verifiedOn')}: <bdi dir="auto">{tpl.verified_note}</bdi>
        </p>
      )}
      <dl className="space-y-2 text-sm">
        <div>
          <dt className="text-xs font-medium text-slate-500">{t('templates.matches')}</dt>
          <dd className="break-words text-slate-700 dark:text-slate-300">
            {describeMatch(tpl.match, t)}{' '}
            <span className="text-xs text-slate-500">
              ({t('templates.priority', { priority: tpl.priority })})
            </span>
          </dd>
        </div>
        <div>
          <dt className="text-xs font-medium text-slate-500">{t('templates.creates')}</dt>
          <dd>
            <ul className="mt-0.5 space-y-1 text-slate-700 dark:text-slate-300">
              {tpl.entries.map((e, i) => {
                const conf = entryConfidence(e);
                const when = e.kind === 'sensor' ? describeWhen(e.when, t) : [];
                return (
                  <li
                    key={i}
                    className="min-w-0 border-s-2 border-slate-200 ps-2 dark:border-slate-700"
                  >
                    <div className="flex items-start justify-between gap-2">
                      <bdi dir="auto" className="min-w-0 break-words">
                        {describeEntry(e, i18n.language, t)}
                      </bdi>
                      {conf && <ConfidenceChip confidence={conf} />}
                    </div>
                    {when.map((w, j) => (
                      <div key={j} className="text-xs text-slate-500">
                        {t('templates.when')}:{' '}
                        <bdi dir="ltr" className="break-all">
                          {w}
                        </bdi>
                      </div>
                    ))}
                    {e.notes && (
                      <div dir="auto" className="break-words text-xs text-slate-500">
                        {isolateLtrRuns(e.notes)}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          </dd>
        </div>
      </dl>
    </Card>
  );
}

export function TemplatesPage() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const q = useTemplateLibrary();
  const [reloaded, setReloaded] = useState<TemplateLibraryStatus | null>(null);
  const reload = useMutation({
    mutationFn: endpoints.reloadTemplates,
    onSuccess: (lib) => {
      setReloaded(lib);
      // Show the new status at once; the refetch brings the templates.
      qc.setQueryData(qk.deviceTemplates, (old: typeof q.data) =>
        old ? { ...old, library: lib } : old,
      );
      void qc.invalidateQueries({ queryKey: qk.deviceTemplates });
      void qc.invalidateQueries({ queryKey: ['deviceTemplate'] });
    },
  });
  const items = q.data
    ? [...q.data.items].sort((a, b) => b.priority - a.priority || a.id.localeCompare(b.id))
    : [];
  const groups = groupByVendor(items);
  const lib = q.data?.library;

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('templates.title')}
        subtitle={t('templates.subtitle')}
        actions={
          isAdmin && (
            <>
              <TemplateDraftButton />
              <Button busy={reload.isPending} onClick={() => reload.mutate()}>
                <RefreshIcon size={14} /> {t('templates.reload')}
              </Button>
            </>
          )
        }
      />
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} />
      <ErrorBox error={reload.error} />
      {reloaded && !reload.isPending && (
        <Notice tone={reloaded.errors.length ? 'info' : 'success'}>
          {t('templates.reloadDone', {
            builtin: reloaded.builtin_count,
            user: reloaded.user_count,
            errors: reloaded.errors.length,
          })}
        </Notice>
      )}
      {lib && <LibraryCard lib={lib} isAdmin={isAdmin} />}
      {q.data && !items.length && <Empty>{t('templates.none')}</Empty>}
      {groups.map((g) => (
        <section key={g.vendor ?? '-'} className="space-y-2">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-600 dark:text-slate-300">
            {g.vendor ? vendorLabel(g.vendor) : t('templates.vendorGeneric')}
            <span className="text-xs font-normal text-slate-500">({g.items.length})</span>
          </h2>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {g.items.map((tpl) => (
              <TemplateCard key={tpl.id} tpl={tpl} />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
