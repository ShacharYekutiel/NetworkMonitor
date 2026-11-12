// [1.3] Apply template → "Ports" section: which interfaces get sensors.
import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { ApplyInterface } from '../api/types';
import { formatSpeed } from '../lib/ports';
import {
  filterInterfaces,
  initialPortPick,
  sensorsToRemove,
  type PortsChoice,
} from '../lib/templates';
import { CheckIcon, SearchIcon } from './icons';
import { Button, Checkbox, cx, ErrorBox, FieldGroup, Input, Loading } from './ui';

const CHOICES: PortsChoice[] = ['none', 'important', 'all', 'choose'];

function operDot(s: string): string {
  if (s === 'up') return 'bg-green-600';
  if (s === 'down' || s === 'lowerLayerDown') return 'bg-red-600';
  return 'bg-slate-500';
}

const chip =
  'inline-flex items-center whitespace-nowrap rounded px-1 py-px text-[10px] font-semibold ring-1 ring-inset';
const REASON_TONE: Record<string, string> = {
  uplink:
    'bg-sky-50 text-sky-800 ring-sky-200 dark:bg-sky-950/50 dark:text-sky-300 dark:ring-sky-900',
  lag: 'bg-violet-50 text-violet-800 ring-violet-200 dark:bg-violet-950/50 dark:text-violet-300 dark:ring-violet-900',
  trunk:
    'bg-teal-50 text-teal-800 ring-teal-200 dark:bg-teal-950/50 dark:text-teal-300 dark:ring-teal-900',
};

function InterfaceRow({
  it,
  checked,
  onToggle,
}: {
  it: ApplyInterface;
  checked: boolean;
  onToggle: (on: boolean) => void;
}) {
  const { t } = useTranslation();
  const speed = formatSpeed(it.speed_bps);
  return (
    <li>
      <label
        className={cx(
          'flex cursor-pointer items-center gap-2 px-2 py-1 text-sm',
          checked ? 'bg-sky-50 dark:bg-sky-950/40' : 'hover:bg-slate-50 dark:hover:bg-slate-800/50',
        )}
        data-testid="apply-port-row"
      >
        <input
          type="checkbox"
          className="h-4 w-4 shrink-0 rounded border-slate-300 text-sky-600 focus:ring-sky-500 dark:border-slate-600 dark:bg-slate-900"
          checked={checked}
          onChange={(e) => onToggle(e.target.checked)}
        />
        <span
          className={cx('h-2.5 w-2.5 shrink-0 rounded-full', operDot(it.oper_status))}
          title={t(`ports.ifStatus.${it.oper_status}`, { defaultValue: it.oper_status })}
          aria-label={t(`ports.ifStatus.${it.oper_status}`, { defaultValue: it.oper_status })}
        />
        <bdi dir="ltr" className="shrink-0 font-medium">
          {it.name}
        </bdi>
        {it.alias && (
          <bdi dir="auto" className="min-w-0 truncate text-xs text-slate-500">
            {it.alias}
          </bdi>
        )}
        <span className="ms-auto flex shrink-0 items-center gap-1">
          {it.reasons.map((r) => (
            <span key={r} className={cx(chip, REASON_TONE[r] ?? REASON_TONE.uplink)}>
              {t(`applyPorts.reason.${r}`, { defaultValue: r })}
            </span>
          ))}
          {it.has_sensor && (
            <span
              className="inline-flex items-center gap-0.5 text-[11px] text-green-700 dark:text-green-400"
              title={t('applyPorts.hasSensorTitle')}
              data-testid="has-sensor"
            >
              <CheckIcon size={11} />
              <span className="hidden sm:inline">{t('applyPorts.hasSensor')}</span>
            </span>
          )}
          {speed && (
            <bdi dir="ltr" className="w-12 text-end text-xs tabular-nums text-slate-500">
              {speed}
            </bdi>
          )}
        </span>
      </label>
    </li>
  );
}

export function ApplyPortsSection({
  deviceId,
  templateId,
  choice,
  onChoice,
  picked,
  onPicked,
  remove,
  onRemove,
}: {
  deviceId: number;
  templateId: string;
  choice: PortsChoice;
  onChoice: (c: PortsChoice) => void;
  /** null until the list has loaded once (then the important + monitored ports are preselected) */
  picked: Set<number> | null;
  onPicked: (s: Set<number>) => void;
  remove: boolean;
  onRemove: (v: boolean) => void;
}) {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');
  const need = choice === 'choose' || remove;
  const q = useQuery({
    queryKey: qk.applyInterfaces(deviceId, templateId),
    queryFn: () => endpoints.applyTemplateInterfaces(deviceId, templateId),
    enabled: need && !!templateId,
    staleTime: 30_000,
  });
  const items = useMemo(() => q.data?.items ?? [], [q.data]);
  useEffect(() => {
    if (q.data && picked === null) onPicked(new Set(initialPortPick(q.data.items)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q.data, picked]);
  const sel = picked ?? new Set<number>();
  const shown = useMemo(() => filterInterfaces(items, query), [items, query]);
  const toRemove = q.data ? sensorsToRemove(items, choice, sel) : null;
  const toggle = (i: number, on: boolean) => {
    const next = new Set(sel);
    if (on) next.add(i);
    else next.delete(i);
    onPicked(next);
  };

  return (
    <div
      className="space-y-2 rounded-md border border-slate-200 p-3 dark:border-slate-700"
      data-testid="apply-ports"
    >
      <FieldGroup label={t('applyPorts.title')}>
        <div className="flex flex-wrap gap-x-4 gap-y-1.5">
          {CHOICES.map((c) => (
            <label key={c} className="inline-flex cursor-pointer items-center gap-1.5 text-sm">
              <input
                type="radio"
                name="apply-ports"
                value={c}
                checked={choice === c}
                onChange={() => onChoice(c)}
                className="h-4 w-4 border-slate-300 text-sky-600 dark:border-slate-600 dark:bg-slate-900"
              />
              {t(`applyPorts.choice.${c}`)}
            </label>
          ))}
        </div>
        <p className="mt-1 text-xs text-slate-500">{t(`applyPorts.hint.${choice}`)}</p>
      </FieldGroup>

      {need && q.isLoading && <Loading />}
      <ErrorBox error={q.error} />
      {q.data?.inventory === 'missing' && (
        <p
          className="rounded-md bg-amber-50 px-3 py-1.5 text-xs text-amber-900 ring-1 ring-inset ring-amber-200 dark:bg-amber-950/40 dark:text-amber-200 dark:ring-amber-900"
          data-testid="apply-ports-missing"
        >
          {t('applyPorts.inventoryMissing')}
        </p>
      )}

      {choice === 'choose' && q.data && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative min-w-0 flex-1 basis-40">
              <SearchIcon
                size={14}
                className="pointer-events-none absolute start-2.5 top-1/2 -translate-y-1/2 text-slate-400"
              />
              <Input
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t('applyPorts.search')}
                aria-label={t('applyPorts.search')}
                dir={query ? 'auto' : undefined}
                className="ps-8"
              />
            </div>
            <Button size="sm" onClick={() => onPicked(new Set(items.map((i) => i.if_index)))}>
              {t('common.selectAll')}
            </Button>
            <Button size="sm" onClick={() => onPicked(new Set())}>
              {t('applyPorts.clear')}
            </Button>
            <Button
              size="sm"
              onClick={() =>
                onPicked(new Set(items.filter((i) => i.important).map((i) => i.if_index)))
              }
            >
              {t('applyPorts.selectImportant')}
            </Button>
          </div>
          <div className="text-xs text-slate-500" data-testid="apply-ports-count">
            {t('applyPorts.selectedOf', {
              n: items.filter((i) => sel.has(i.if_index)).length,
              total: items.length,
            })}
          </div>
          {items.length === 0 ? (
            <p className="text-sm text-slate-500">{t('applyPorts.noPorts')}</p>
          ) : (
            <ul
              className="max-h-64 divide-y divide-slate-100 overflow-y-auto rounded-md ring-1 ring-inset ring-slate-200 dark:divide-slate-800 dark:ring-slate-700"
              data-testid="apply-ports-list"
            >
              {shown.map((it) => (
                <InterfaceRow
                  key={it.if_index}
                  it={it}
                  checked={sel.has(it.if_index)}
                  onToggle={(on) => toggle(it.if_index, on)}
                />
              ))}
            </ul>
          )}
        </div>
      )}

      {choice !== 'all' && (
        <div className="space-y-1">
          <Checkbox label={t('applyPorts.removeUnselected')} checked={remove} onChange={onRemove} />
          {remove && toRemove && (
            <p
              className="text-xs font-medium text-red-600 dark:text-red-400"
              data-testid="apply-ports-remove-count"
            >
              {t('applyPorts.removeCount', { count: toRemove.length })}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
