import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { DevicePicker } from '../Dependency';
import { GroupSelect } from '../EntityForms';
import { Button, Checkbox, cx, Input, Select } from '../ui';
import { type LogFilter, parseList } from '../../lib/logs';
import { useEventRules } from './rules';
import { FacilityPicker, SeveritySelect, TimeRangeSelect } from './common';
import { facilityLabel } from './common';

/** Text fields are applied on Enter / "Apply"; selects apply at once. */
type Draft = { q: string; programs: string; sourceIp: string; hostname: string; trapOid: string };
const draftOf = (f: LogFilter): Draft => ({
  q: f.q,
  programs: f.programs.join(', '),
  sourceIp: f.sourceIp,
  hostname: f.hostname,
  trapOid: f.trapOid,
});

function FacilityMenu({ value, onChange }: { value: number[]; onChange: (v: number[]) => void }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', h);
    return () => document.removeEventListener('mousedown', h);
  }, [open]);
  return (
    <div className="relative" ref={ref}>
      <Button size="sm" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        {t('logs.facility')}:{' '}
        <bdi dir="ltr">{value.length ? value.map(facilityLabel).join(', ') : t('logs.any')}</bdi>
      </Button>
      {open && (
        <div className="absolute start-0 z-30 mt-1 w-80 rounded-md bg-surface p-3 shadow-pop ring-1 ring-line">
          <FacilityPicker value={value} onChange={onChange} />
          <div className="mt-2 flex justify-end">
            <Button size="sm" variant="ghost" onClick={() => onChange([])}>
              {t('logs.clearSelection')}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

export function LogFilterBar({
  kind,
  filter,
  onChange,
  fixedDevice,
  extra,
}: {
  kind: 'syslog' | 'trap';
  filter: LogFilter;
  onChange: (p: Partial<LogFilter>) => void;
  fixedDevice: boolean;
  extra?: ReactNode;
}) {
  const { t } = useTranslation();
  const rules = useEventRules();
  const [draft, setDraft] = useState<Draft>(() => draftOf(filter));
  const [more, setMore] = useState(false);
  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    onChange({
      q: draft.q,
      programs: parseList(draft.programs),
      sourceIp: draft.sourceIp.trim(),
      hostname: draft.hostname.trim(),
      trapOid: draft.trapOid.trim(),
    });
  };
  const kindRules = rules.data?.filter((r) => r.kind === kind) ?? [];
  return (
    <form onSubmit={submit} className="space-y-2" data-testid={`${kind}-filters`}>
      <div className="flex flex-wrap items-center gap-2">
        <TimeRangeSelect filter={filter} onChange={onChange} />
        <div className="flex-1" />
        {extra}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {!fixedDevice && (
          <>
            <div className="w-64 max-w-full">
              <DevicePicker
                value={filter.deviceId}
                onChange={(v) => onChange({ deviceId: v, unknown: false })}
                allowNone
                label={t('logs.device')}
              />
            </div>
            <div className="w-44">
              <GroupSelect
                value={filter.groupId}
                onChange={(v) => onChange({ groupId: v, unknown: false })}
                allowRoot
                rootLabel={t('logs.anyGroup')}
              />
            </div>
            <Checkbox
              label={t('logs.unknownOnly')}
              checked={filter.unknown}
              onChange={(v) => onChange({ unknown: v, deviceId: null, groupId: null })}
            />
          </>
        )}
        {kind === 'syslog' && (
          <>
            <div className="w-44">
              <SeveritySelect
                value={filter.severityMax}
                onChange={(v) => onChange({ severityMax: v })}
                label={t('logs.severity')}
              />
            </div>
            <FacilityMenu value={filter.facilities} onChange={(v) => onChange({ facilities: v })} />
            <Input
              value={draft.programs}
              onChange={(e) => setDraft({ ...draft, programs: e.target.value })}
              onBlur={() => submit()}
              placeholder={t('logs.program')}
              aria-label={t('logs.program')}
              className="w-36"
              dir="ltr"
            />
          </>
        )}
        {kind === 'trap' && (
          <Input
            value={draft.trapOid}
            onChange={(e) => setDraft({ ...draft, trapOid: e.target.value })}
            onBlur={() => submit()}
            placeholder={t('traps.oidPrefix')}
            aria-label={t('traps.oidPrefix')}
            className="w-48 font-mono"
            dir="ltr"
          />
        )}
        <Input
          type="search"
          value={draft.q}
          maxLength={200}
          onChange={(e) => setDraft({ ...draft, q: e.target.value })}
          placeholder={kind === 'syslog' ? t('logs.textPlaceholder') : t('traps.textPlaceholder')}
          aria-label={t('logs.text')}
          className="min-w-[10rem] flex-1"
          dir={draft.q ? 'auto' : undefined}
        />
        <Button type="submit" size="sm">
          {t('logs.apply')}
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setMore((m) => !m)} aria-expanded={more}>
          {more ? t('logs.lessFilters') : t('logs.moreFilters')}
        </Button>
      </div>
      {more && (
        <div className={cx('flex flex-wrap items-center gap-2')}>
          <Input
            value={draft.sourceIp}
            onChange={(e) => setDraft({ ...draft, sourceIp: e.target.value })}
            onBlur={() => submit()}
            placeholder={t('logs.sourceIp')}
            aria-label={t('logs.sourceIp')}
            className="w-40 font-mono"
            dir="ltr"
          />
          {kind === 'syslog' && (
            <Input
              value={draft.hostname}
              onChange={(e) => setDraft({ ...draft, hostname: e.target.value })}
              onBlur={() => submit()}
              placeholder={t('logs.hostname')}
              aria-label={t('logs.hostname')}
              className="w-40"
              dir="ltr"
            />
          )}
          <Select
            value={filter.ruleId === null ? '' : String(filter.ruleId)}
            onChange={(e) => onChange({ ruleId: e.target.value ? Number(e.target.value) : null })}
            aria-label={t('logs.rule')}
            className="w-56"
          >
            <option value="">{t('logs.anyRule')}</option>
            {kindRules.map((r) => (
              <option key={r.id} value={r.id}>
                {r.name}
              </option>
            ))}
          </Select>
        </div>
      )}
    </form>
  );
}
