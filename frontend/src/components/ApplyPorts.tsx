// [1.3] Apply template → "Ports" section: which interfaces get sensors.
import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { ApplyInterface } from '../api/types';
import { formatSpeed } from '../lib/ports';
import {
  coveredPorts,
  filterInterfaces,
  initialPortPick,
  sensorsToRemove,
  type PortsChoice,
} from '../lib/templates';
import {
  EMPTY_FILTER,
  filterActive,
  filterErrors,
  filteredPick,
  hasPortData,
  previewCounts,
  type PortFilter,
} from '../lib/noise';
import { CheckIcon, SearchIcon } from './icons';
import {
  BANNER,
  BANNER_TONE,
  Button,
  Checkbox,
  cx,
  ErrorBox,
  FieldGroup,
  Input,
  Field,
  Loading,
  Select,
  TAG,
  TAG_TONE,
} from './ui';

const CHOICES: PortsChoice[] = ['none', 'important', 'all', 'choose'];

function operDot(s: string): string {
  if (s === 'up') return 'bg-status-up';
  if (s === 'down' || s === 'lowerLayerDown') return 'bg-status-down';
  return 'bg-status-paused';
}

const REASON_TONE: Record<string, string> = {
  uplink: TAG_TONE.accent,
  lag: TAG_TONE.accent,
  trunk: TAG_TONE.accent,
  irf: TAG_TONE.warning,
  description: TAG_TONE.accent,
  marked: TAG_TONE.accent,
};

/** [if-noise] "Choose ports" filter: VLANs, description, port mode, network neighbour. */
function PortFilterPanel({
  items,
  onPick,
}: {
  items: ApplyInterface[];
  onPick: (ids: number[]) => void;
}) {
  const { t } = useTranslation();
  const [f, setF] = useState<PortFilter>(EMPTY_FILTER);
  const [open, setOpen] = useState(false);
  const err = filterErrors(f);
  const bad = Object.values(err).some(Boolean);
  const data = hasPortData(items);
  const set = (patch: Partial<PortFilter>) => setF((x) => ({ ...x, ...patch }));
  const n = bad ? 0 : filteredPick(items, f).length;
  const errText = (on: boolean, key: string) =>
    on ? <span className="text-status-down">{t(key)}</span> : undefined;
  return (
    <div className="rounded-lg border border-line px-3 py-2" data-testid="apply-ports-filter">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="inline-flex items-center gap-1 text-sm font-medium text-accent-ink hover:underline"
        data-testid="apply-ports-filter-toggle"
      >
        {t('applyFilter.title')}
        {filterActive(f) && <span aria-hidden="true">•</span>}
      </button>
      {open && (
        <div className="mt-2 space-y-2">
          <p className="text-xs text-ink-muted">{t('applyFilter.intro')}</p>
          {!data && (
            <p className={cx(BANNER, BANNER_TONE.warning, 'py-1.5 text-xs')}>
              {t('applyFilter.noData')}
            </p>
          )}
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <Field
              label={t('applyFilter.includeVlans')}
              hint={errText(err.includeVlans, 'applyFilter.vlanErr')}
            >
              <Input
                value={f.includeVlans}
                onChange={(e) => set({ includeVlans: e.target.value })}
                placeholder="20, 100-110"
                dir="ltr"
                disabled={!data}
                data-testid="filter-include-vlans"
              />
            </Field>
            <Field
              label={t('applyFilter.excludeVlans')}
              hint={errText(err.excludeVlans, 'applyFilter.vlanErr')}
            >
              <Input
                value={f.excludeVlans}
                onChange={(e) => set({ excludeVlans: e.target.value })}
                placeholder="10"
                dir="ltr"
                disabled={!data}
                data-testid="filter-exclude-vlans"
              />
            </Field>
            <Field
              label={t('applyFilter.descrInclude')}
              hint={errText(err.descrInclude, 'applyFilter.regexErr')}
            >
              <Input
                value={f.descrInclude}
                onChange={(e) => set({ descrInclude: e.target.value })}
                placeholder="uplink|server"
                dir="ltr"
                className="font-mono"
                data-testid="filter-descr-include"
              />
            </Field>
            <Field
              label={t('applyFilter.descrExclude')}
              hint={errText(err.descrExclude, 'applyFilter.regexErr')}
            >
              <Input
                value={f.descrExclude}
                onChange={(e) => set({ descrExclude: e.target.value })}
                placeholder="pc|printer"
                dir="ltr"
                className="font-mono"
                data-testid="filter-descr-exclude"
              />
            </Field>
            <Field label={t('applyFilter.mode')}>
              <Select
                value={f.mode}
                disabled={!data}
                onChange={(e) => set({ mode: e.target.value as PortFilter['mode'] })}
                data-testid="filter-mode"
              >
                {(['any', 'access', 'trunk'] as const).map((m) => (
                  <option key={m} value={m}>
                    {t(`applyFilter.modes.${m}`)}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label={t('applyFilter.neighbor')}>
              <Select
                value={f.neighbor}
                disabled={!data}
                onChange={(e) => set({ neighbor: e.target.value as PortFilter['neighbor'] })}
                data-testid="filter-neighbor"
              >
                {(['any', 'with', 'without'] as const).map((m) => (
                  <option key={m} value={m}>
                    {t(`applyFilter.neighbors.${m}`)}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant="primary"
              disabled={bad || !filterActive(f)}
              onClick={() => onPick(filteredPick(items, f))}
              data-testid="filter-apply"
            >
              {t('applyFilter.select', { count: n })}
            </Button>
            <Button size="sm" onClick={() => setF(EMPTY_FILTER)} disabled={!filterActive(f)}>
              {t('applyFilter.reset')}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

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
          checked ? 'bg-accent-soft' : 'hover:bg-surface-hover',
        )}
        data-testid="apply-port-row"
      >
        <input
          type="checkbox"
          className="m-0 h-4 w-4 shrink-0 accent-accent"
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
          <bdi dir="auto" className="min-w-0 truncate text-xs text-ink-muted">
            {it.alias}
          </bdi>
        )}
        <span className="ms-auto flex shrink-0 items-center gap-1">
          {it.reasons.map((r) => (
            <span key={r} className={cx(TAG, REASON_TONE[r] ?? REASON_TONE.uplink)}>
              {t(`applyPorts.reason.${r}`, { defaultValue: r })}
            </span>
          ))}
          {it.has_sensor && (
            <span
              className="inline-flex items-center gap-0.5 text-[11px] text-status-up"
              title={t('applyPorts.hasSensorTitle')}
              data-testid="has-sensor"
            >
              <CheckIcon size={11} />
              <span className="hidden sm:inline">{t('applyPorts.hasSensor')}</span>
            </span>
          )}
          {speed && (
            <bdi dir="ltr" className="w-12 text-end text-xs tabular-nums text-ink-muted">
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
  // [if-noise] also loaded for "important" / "all": the preview counts what will be monitored
  const q = useQuery({
    queryKey: qk.applyInterfaces(deviceId, templateId),
    queryFn: () => endpoints.applyTemplateInterfaces(deviceId, templateId),
    enabled: choice !== 'none' && !!templateId,
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
  const preview = q.data ? previewCounts(items, coveredPorts(items, choice, sel)) : null;
  const toggle = (i: number, on: boolean) => {
    const next = new Set(sel);
    if (on) next.add(i);
    else next.delete(i);
    onPicked(next);
  };

  return (
    <div className="space-y-2 rounded-lg border border-line px-4 py-3.5" data-testid="apply-ports">
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
                className="m-0 h-4 w-4 accent-accent"
              />
              {t(`applyPorts.choice.${c}`)}
            </label>
          ))}
        </div>
        <p className="mt-1 text-xs text-ink-muted">{t(`applyPorts.hint.${choice}`)}</p>
      </FieldGroup>

      {need && q.isLoading && <Loading />}
      {need && <ErrorBox error={q.error} />}
      {preview && choice !== 'none' && items.length > 0 && (
        <p className="text-xs text-ink-muted" data-testid="apply-ports-preview">
          {t('applyFilter.preview', { will: preview.will, wont: preview.wont })}
        </p>
      )}
      {need && q.data?.inventory === 'missing' && (
        <p
          className={cx(BANNER, BANNER_TONE.warning, 'py-1.5 text-xs')}
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
                className="pointer-events-none absolute start-2.5 top-1/2 -translate-y-1/2 text-ink-subtle"
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
          <PortFilterPanel items={items} onPick={(ids) => onPicked(new Set(ids))} />
          <div className="text-xs text-ink-muted" data-testid="apply-ports-count">
            {t('applyPorts.selectedOf', {
              n: items.filter((i) => sel.has(i.if_index)).length,
              total: items.length,
            })}
          </div>
          {items.length === 0 ? (
            <p className="text-sm text-ink-muted">{t('applyPorts.noPorts')}</p>
          ) : (
            <ul
              className="max-h-64 divide-y divide-line overflow-y-auto rounded-lg border border-line bg-surface"
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
              className="text-xs font-medium text-status-down"
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
