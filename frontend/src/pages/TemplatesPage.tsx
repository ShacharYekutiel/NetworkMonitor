import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { TFunction } from 'i18next';
import type {
  DeviceTemplate,
  TemplateEntry,
  TemplateLibraryStatus,
  TemplateLoadError,
} from '../api/types';
import { AlertIcon, ChevronIcon, RefreshIcon } from '../components/icons';
import { TemplateDraftButton } from '../components/assistant/TemplateDraftWizard';
import { ConfidenceChip, useTemplateLibrary, VerifiedBadge } from '../components/Templates';
import {
  Button,
  Card,
  CHIP,
  cx,
  Empty,
  ErrorBox,
  Loading,
  Notice,
  PageHeader,
  TAG,
  TAG_TONE,
} from '../components/ui';
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

const chip = CHIP;

function SourceBadge({ tpl }: { tpl: DeviceTemplate }) {
  const { t } = useTranslation();
  if (!tpl.source) return null;
  const user = tpl.source === 'user';
  return (
    <span
      title={tpl.file || undefined}
      className={cx(TAG, user ? TAG_TONE.accent : TAG_TONE.neutral)}
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
      className="rounded-lg bg-status-down-soft px-4 py-3.5"
      data-testid="template-errors"
    >
      <div className="mb-2 flex items-center gap-2 font-semibold">
        <AlertIcon size={18} className="text-status-down" />
        {t('templates.loadErrors', { count: errors.length })}
      </div>
      <ul className="m-0 grid list-none gap-2 p-0 text-[13px]">
        {errors.map((e, i) => (
          <li key={`${e.file}-${i}`} className="min-w-0">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
              <span className={cx(chip, 'h-6 bg-surface text-ink-muted')}>
                {t(`templates.source.${e.source}`)}
              </span>
              <code dir="ltr" className="break-all font-medium text-status-down">
                {e.file}
                {e.path && <span> · {e.path}</span>}
              </code>
            </div>
            <div dir="ltr" className="mt-0.5 break-words text-left text-ink-muted rtl:text-right">
              {e.message}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** The library status as quiet header lines (no card): counts, load time, user folder. */
function LibraryMeta({ lib, isAdmin }: { lib: TemplateLibraryStatus; isAdmin: boolean }) {
  const { t } = useTranslation();
  return (
    <>
      <span>
        {t('templates.libraryLine', {
          builtin: lib.builtin_count,
          user: lib.user_count,
          errors: lib.errors.length,
        })}
        {' · '}
        {t('templates.loadedAt', { time: formatDateTime(lib.loaded_at) })}
      </span>
      {lib.overridden.length > 0 && (
        <span>
          {t('templates.overriddenList')}:{' '}
          {lib.overridden.map((id, i) => (
            <span key={id}>
              {i > 0 && ', '}
              <code dir="ltr">{id}</code>
            </span>
          ))}
        </span>
      )}
      {isAdmin && lib.user_dir && (
        <span title={t('templates.userDirHint')}>
          {t('templates.userDir')}:{' '}
          <code dir="ltr" className="break-all">
            {lib.user_dir}
          </code>
        </span>
      )}
    </>
  );
}

/** A short name per entry for the card's one-line summary ("Ping · CPU · Interfaces"). */
function entryShort(e: TemplateEntry, lang: string, t: TFunction): string {
  if (e.kind === 'snmp_interfaces') return t('templates.shortInterfaces');
  const raw =
    e.kind === 'sensor' ? l10n(e.name, lang) || t(`sensorType.${e.type}`) : l10n(e.name, lang);
  return raw
    .replace(/\{[a-z0-9_]+\}/gi, '')
    .replace(/\s+/g, ' ')
    .trim();
}

/** Template card (prototype templates: name + tag, the sensors in one grey line, version); the
 * match rules and every entry open under "Details". */
function TemplateCard({ tpl, vendor }: { tpl: DeviceTemplate; vendor: string }) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const summary = [...new Set(tpl.entries.map((e) => entryShort(e, lang, t)).filter(Boolean))];
  const description = l10n(tpl.description, lang);
  return (
    <Card
      className="flex flex-col"
      bodyClassName="flex flex-1 flex-col gap-3 px-[22px] pb-5 pt-0"
      title={
        <span className="grid gap-0.5">
          <span className="text-[12.5px] font-medium text-ink-subtle">{vendor}</span>
          <span className="flex flex-wrap items-center gap-2">
            <bdi dir="auto">{l10n(tpl.name, lang)}</bdi>
            {tpl.verified !== undefined && <VerifiedBadge tpl={tpl} />}
          </span>
        </span>
      }
      actions={
        <code dir="ltr" className="num text-xs text-ink-subtle">
          {tpl.id}
        </code>
      }
    >
      {/* the description, or else the sensors in one grey line */}
      <p className="m-0 text-[13.5px] text-ink-muted">
        <bdi dir="auto">{description || isolateLtrRuns(summary.join(' · '))}</bdi>
      </p>
      {tpl.verified && tpl.verified_note && (
        <p className="m-0 text-xs text-ink-muted">
          {t('templates.verifiedOn')}: <bdi dir="auto">{tpl.verified_note}</bdi>
        </p>
      )}
      <details className="group mt-auto min-w-0">
        <summary className="flex cursor-pointer list-none flex-wrap items-center gap-2 text-[13px] text-ink-muted marker:hidden hover:text-ink [&::-webkit-details-marker]:hidden">
          {tpl.version !== undefined && (
            <span className="num">{t('templates.version', { version: tpl.version })}</span>
          )}
          {tpl.requires_snmp && <span className={cx(TAG, TAG_TONE.neutral)}>SNMP</span>}
          <SourceBadge tpl={tpl} />
          <span className="ms-auto inline-flex items-center gap-1">
            {t('templates.details')}
            <ChevronIcon
              size={14}
              className="transition-transform group-open:rotate-90 rtl:rotate-180 rtl:group-open:rotate-90"
            />
          </span>
        </summary>
        <dl className="mt-3 grid gap-3 border-t border-line pt-3 text-sm">
          <div>
            <dt className="text-[12.5px] text-ink-muted">{t('templates.matches')}</dt>
            <dd className="m-0 break-words text-ink">
              {describeMatch(tpl.match, t)}{' '}
              <span className="text-xs text-ink-muted">
                ({t('templates.priority', { priority: tpl.priority })})
              </span>
            </dd>
          </div>
          <div>
            <dt className="text-[12.5px] text-ink-muted">{t('templates.creates')}</dt>
            <dd className="m-0">
              <ul className="m-0 mt-1 grid list-none gap-2 p-0 text-ink">
                {tpl.entries.map((e, i) => {
                  const conf = entryConfidence(e);
                  const when = e.kind === 'sensor' ? describeWhen(e.when, t) : [];
                  return (
                    <li key={i} className="min-w-0">
                      <div className="flex items-start justify-between gap-2">
                        <bdi dir="auto" className="min-w-0 break-words">
                          {describeEntry(e, lang, t)}
                        </bdi>
                        {conf && <ConfidenceChip confidence={conf} />}
                      </div>
                      {when.map((w, j) => (
                        <div key={j} className="text-xs text-ink-muted">
                          {t('templates.when')}:{' '}
                          <bdi dir="ltr" className="break-all">
                            {w}
                          </bdi>
                        </div>
                      ))}
                      {e.notes && (
                        <div dir="auto" className="break-words text-xs text-ink-muted">
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
      </details>
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
        meta={lib && <LibraryMeta lib={lib} isAdmin={isAdmin} />}
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
      {lib && isAdmin && <ErrorsPanel errors={lib.errors} />}
      {q.data && !items.length && <Empty>{t('templates.none')}</Empty>}
      {/* prototype .three: one grid of cards, by vendor (the vendor above each name) */}
      <div className="grid grid-cols-1 items-start gap-4 md:grid-cols-2 xl:grid-cols-3">
        {groups.flatMap((g) =>
          g.items.map((tpl) => (
            <TemplateCard
              key={tpl.id}
              tpl={tpl}
              vendor={g.vendor ? vendorLabel(g.vendor) : t('templates.vendorGeneric')}
            />
          )),
        )}
      </div>
    </div>
  );
}
