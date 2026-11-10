import { useEffect, useState, type FormEvent } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints } from '../../api/endpoints';
import type {
  EventRule,
  EventRuleAction,
  EventRuleKind,
  RuleScope,
  RuleTestResult,
  VarbindOp,
} from '../../api/types';
import { EVENT_RULE_ACTIONS, isPassiveAction, VARBIND_OPS } from '../../api/types';
import { useFeatures } from '../../hooks/features';
import {
  formToInput,
  IF_INDEX_OID,
  MAX_VARBINDS,
  ruleToForm,
  validateRule,
  type MatchForm,
  type RuleErrors,
  type RuleForm,
} from '../../lib/eventRules';
import { formatDateTime } from '../../lib/format';
import { DevicePicker } from '../Dependency';
import { GroupSelect } from '../EntityForms';
import { PlusIcon, TrashIcon } from '../icons';
import { useToast } from '../toast';
import {
  Button,
  Checkbox,
  cx,
  ErrorBox,
  Field,
  FieldGroup,
  Input,
  Modal,
  Notice,
  Select,
  Textarea,
} from '../ui';
import { FacilityPicker, SeveritySelect } from './common';

/** Well-known trap OIDs offered as suggestions (SNMPv2-MIB snmpTraps). */
const KNOWN_TRAPS: [string, string][] = [
  ['1.3.6.1.6.3.1.1.5.1', 'coldStart'],
  ['1.3.6.1.6.3.1.1.5.2', 'warmStart'],
  ['1.3.6.1.6.3.1.1.5.3', 'linkDown'],
  ['1.3.6.1.6.3.1.1.5.4', 'linkUp'],
  ['1.3.6.1.6.3.1.1.5.5', 'authenticationFailure'],
];

function Err({ e, field: k }: { e: RuleErrors; field: string }) {
  const { t } = useTranslation();
  return e[k] ? <span className="text-xs text-red-600">{t(e[k] as string)}</span> : null;
}

function MatchEditor({
  kind,
  value,
  onChange,
  errors,
  prefix,
}: {
  kind: EventRuleKind;
  value: MatchForm;
  onChange: (m: MatchForm) => void;
  errors: RuleErrors;
  prefix: string;
}) {
  const { t } = useTranslation();
  const set = (p: Partial<MatchForm>) => onChange({ ...value, ...p });
  if (kind === 'syslog')
    return (
      <div className="space-y-3">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label={t('eventRules.severityMax')}>
            <SeveritySelect
              value={value.severityMax}
              onChange={(v) => set({ severityMax: v })}
              label={t('eventRules.severityMax')}
            />
          </Field>
          <Field
            label={t('eventRules.programs')}
            hint={
              errors[`${prefix}.programs`] ? (
                <Err e={errors} field={`${prefix}.programs`} />
              ) : (
                t('eventRules.programsHint')
              )
            }
          >
            <Input
              value={value.programs}
              onChange={(e) => set({ programs: e.target.value })}
              dir="ltr"
            />
          </Field>
        </div>
        <FieldGroup label={t('eventRules.facilities')} hint={t('eventRules.facilitiesHint')}>
          <FacilityPicker value={value.facilities} onChange={(v) => set({ facilities: v })} />
        </FieldGroup>
        <Field
          label={t('eventRules.text')}
          hint={
            errors[`${prefix}.text`] ? (
              <Err e={errors} field={`${prefix}.text`} />
            ) : (
              t('eventRules.textHint')
            )
          }
        >
          <Input
            value={value.text}
            onChange={(e) => set({ text: e.target.value })}
            maxLength={200}
            dir="ltr"
            className="font-mono"
          />
        </Field>
        <Checkbox
          label={t('eventRules.textRegex')}
          checked={value.textRegex}
          onChange={(v) => set({ textRegex: v })}
        />
      </div>
    );
  const setRow = (i: number, p: Partial<MatchForm['varbinds'][number]>) =>
    set({ varbinds: value.varbinds.map((r, j) => (j === i ? { ...r, ...p } : r)) });
  return (
    <div className="space-y-3">
      <Field
        label={t('eventRules.trapOids')}
        hint={
          errors[`${prefix}.trapOids`] ? (
            <Err e={errors} field={`${prefix}.trapOids`} />
          ) : (
            t('eventRules.trapOidsHint')
          )
        }
      >
        <Textarea
          rows={2}
          value={value.trapOids}
          onChange={(e) => set({ trapOids: e.target.value })}
          dir="ltr"
          className="font-mono"
        />
      </Field>
      <div className="flex flex-wrap gap-1">
        {KNOWN_TRAPS.map(([oid, name]) => (
          <Button
            key={oid}
            size="sm"
            variant="ghost"
            onClick={() =>
              set({ trapOids: value.trapOids.trim() ? `${value.trapOids.trim()}\n${oid}` : oid })
            }
          >
            <PlusIcon size={12} /> <bdi dir="ltr">{name}</bdi>
          </Button>
        ))}
      </div>
      <FieldGroup label={t('eventRules.varbinds')} hint={t('eventRules.varbindsHint')}>
        <div className="space-y-1.5">
          {value.varbinds.map((r, i) => (
            <div key={i} className="flex flex-wrap items-center gap-1.5">
              <Input
                value={r.oid}
                onChange={(e) => setRow(i, { oid: e.target.value })}
                placeholder="1.3.6.1.2.1.2.2.1.7"
                aria-label={t('eventRules.varbindOid')}
                className="w-56 font-mono"
                dir="ltr"
                aria-invalid={!!errors[`${prefix}.varbinds.${i}.oid`]}
              />
              <Select
                value={r.op}
                onChange={(e) => setRow(i, { op: e.target.value as VarbindOp })}
                aria-label={t('eventRules.varbindOp')}
                className="w-36"
              >
                {VARBIND_OPS.map((op) => (
                  <option key={op} value={op}>
                    {t(`eventRules.op.${op}`)}
                  </option>
                ))}
              </Select>
              {r.op !== 'exists' && r.op !== 'absent' && (
                <Input
                  value={r.value}
                  onChange={(e) => setRow(i, { value: e.target.value })}
                  aria-label={t('eventRules.varbindValue')}
                  className="w-40"
                  dir="ltr"
                  aria-invalid={!!errors[`${prefix}.varbinds.${i}.value`]}
                />
              )}
              <Button
                size="sm"
                variant="ghost"
                aria-label={t('common.delete')}
                onClick={() => set({ varbinds: value.varbinds.filter((_, j) => j !== i) })}
              >
                <TrashIcon size={13} />
              </Button>
              <Err e={errors} field={`${prefix}.varbinds.${i}.oid`} />
              <Err e={errors} field={`${prefix}.varbinds.${i}.value`} />
            </div>
          ))}
          {value.varbinds.length < MAX_VARBINDS && (
            <Button
              size="sm"
              onClick={() =>
                set({ varbinds: [...value.varbinds, { oid: '', op: 'eq', value: '' }] })
              }
            >
              <PlusIcon size={12} /> {t('eventRules.addVarbind')}
            </Button>
          )}
        </div>
      </FieldGroup>
      <Field label={t('eventRules.ports')} hint={t('eventRules.portsHint')}>
        <Select
          value={value.ports}
          onChange={(e) => set({ ports: e.target.value as MatchForm['ports'] })}
        >
          <option value="any">{t('eventRules.portsAny')}</option>
          <option value="important">{t('eventRules.portsImportant')}</option>
        </Select>
      </Field>
    </div>
  );
}

function TestResults({ r }: { r: RuleTestResult }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-2" data-testid="rule-test">
      <p className="text-sm">
        {t('eventRules.testSummary', {
          scanned: r.scanned,
          matched: r.matched,
          cleared: r.cleared,
        })}
      </p>
      {r.items.length > 0 && (
        <div className="max-h-64 overflow-auto rounded ring-1 ring-slate-200 dark:ring-slate-800">
          <table className="w-full text-xs">
            <tbody>
              {r.items.map((it) => (
                <tr
                  key={`${it.kind}-${it.id}`}
                  className="border-b border-slate-100 dark:border-slate-800"
                >
                  <td className="whitespace-nowrap px-2 py-1 text-slate-500">
                    {formatDateTime(it.time)}
                  </td>
                  <td className="px-2 py-1">
                    <span
                      className={cx(
                        'rounded px-1 text-[10px] font-semibold uppercase',
                        it.result === 'raise'
                          ? 'bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-200'
                          : it.result === 'clear'
                            ? 'bg-green-100 text-green-800 dark:bg-green-900/50 dark:text-green-200'
                            : 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300',
                      )}
                    >
                      {t(`eventRules.result.${it.result}`)}
                    </span>
                  </td>
                  <td className="whitespace-nowrap px-2 py-1">
                    <bdi dir="auto">{it.device_name ?? '—'}</bdi>
                  </td>
                  <td className="px-2 py-1 font-mono">
                    {it.key && <span className="me-1 text-slate-500">[{it.key}]</span>}
                    <bdi dir="ltr">{it.text}</bdi>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function RuleEditorModal({
  open,
  onClose,
  rule,
  initial,
  onSaved,
}: {
  open: boolean;
  onClose: () => void;
  rule?: EventRule | null;
  /** prefill of a new rule (e.g. "Create rule from this message") */
  initial?: RuleForm | null;
  onSaved?: (r: EventRule) => void;
}) {
  const { t } = useTranslation();
  const { features } = useFeatures();
  const qc = useQueryClient();
  const toast = useToast();
  const [f, setF] = useState<RuleForm>(() => ruleToForm(rule ?? null));
  const [touched, setTouched] = useState(false);
  const [testResult, setTestResult] = useState<RuleTestResult | null>(null);
  useEffect(() => {
    if (!open) return;
    setF(rule ? ruleToForm(rule) : (initial ?? ruleToForm(null)));
    setTouched(false);
    setTestResult(null);
  }, [open, rule, initial]);
  const set = (p: Partial<RuleForm>) => setF((x) => ({ ...x, ...p }));
  const errors = validateRule(f);
  const invalid = Object.keys(errors).length > 0;
  const shown = touched ? errors : {};

  const save = useMutation({
    mutationFn: () => {
      const body = formToInput(f);
      if (rule) {
        // kind is immutable (PATCH with kind → 422)
        const patch = { ...body };
        delete patch.kind;
        return endpoints.updateEventRule(rule.id, patch);
      }
      return endpoints.createEventRule(body);
    },
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ['event-rules'] });
      toast.push('success', t('common.saved'));
      onSaved?.(r);
      onClose();
    },
  });
  const test = useMutation({
    mutationFn: () => endpoints.testEventRule(formToInput(f), 24),
    onMutate: () => setTestResult(null),
    onSuccess: setTestResult,
  });
  const reset = useMutation({
    mutationFn: () => endpoints.resetEventRule(rule!.id),
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ['event-rules'] });
      setF(ruleToForm(r));
      toast.push('success', t('eventRules.resetDone'));
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (!invalid) save.mutate();
  };
  const active = !isPassiveAction(f.action);

  return (
    <Modal
      open={open}
      onClose={onClose}
      wide
      title={
        <span className="inline-flex items-center gap-2">
          {rule ? t('eventRules.edit') : t('eventRules.add')}
          {rule?.builtin && <BuiltinBadge />}
        </span>
      }
    >
      <form onSubmit={submit} className="space-y-4" data-testid="rule-editor">
        {rule && <p className="text-xs text-slate-500">{t('eventRules.editClearsConditions')}</p>}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-4">
          <Field
            label={t('common.name')}
            required
            className="sm:col-span-2"
            hint={<Err e={shown} field="name" />}
          >
            <Input value={f.name} onChange={(e) => set({ name: e.target.value })} maxLength={128} />
          </Field>
          <Field label={t('eventRules.kind')}>
            <Select
              value={f.kind}
              disabled={!!rule}
              onChange={(e) => {
                const kind = e.target.value as EventRuleKind;
                set({
                  kind,
                  match: ruleToForm(null, kind).match,
                  clearMatch: ruleToForm(null, kind).clearMatch,
                });
              }}
            >
              <option value="syslog">{t('eventRules.kinds.syslog')}</option>
              <option value="trap">{t('eventRules.kinds.trap')}</option>
            </Select>
          </Field>
          <Field
            label={t('eventRules.priority')}
            hint={
              shown.priority ? <Err e={shown} field="priority" /> : t('eventRules.priorityHint')
            }
          >
            <Input
              type="number"
              min={0}
              max={100000}
              value={f.priority}
              onChange={(e) => set({ priority: e.target.value })}
              dir="ltr"
            />
          </Field>
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label={t('eventRules.scope')} hint={<Err e={shown} field="scope" />}>
            <Select value={f.scope} onChange={(e) => set({ scope: e.target.value as RuleScope })}>
              {(['global', 'group', 'device'] as const).map((s) => (
                <option key={s} value={s}>
                  {t(`maintenance.scopes.${s}`)}
                </option>
              ))}
            </Select>
          </Field>
          {f.scope === 'group' && (
            <Field label={t('eventRules.group')} className="sm:col-span-2">
              <GroupSelect
                value={f.groupId}
                onChange={(v) => set({ groupId: v })}
                allowRoot
                rootLabel={t('eventRules.chooseGroup')}
              />
            </Field>
          )}
          {f.scope === 'device' && (
            <FieldGroup label={t('eventRules.device')} className="sm:col-span-2">
              <DevicePicker
                value={f.deviceId}
                onChange={(v) => set({ deviceId: v })}
                label={t('eventRules.device')}
              />
            </FieldGroup>
          )}
        </div>

        <fieldset className="rounded-md border border-slate-200 p-3 dark:border-slate-700">
          <legend className="px-1 text-xs font-semibold text-slate-600 dark:text-slate-400">
            {t('eventRules.match')}
          </legend>
          <MatchEditor
            kind={f.kind}
            value={f.match}
            onChange={(m) => set({ match: m })}
            errors={shown}
            prefix="match"
          />
        </fieldset>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label={t('eventRules.action')} hint={t(`eventRules.actionHint.${f.action}`)}>
            <Select
              value={f.action}
              onChange={(e) => set({ action: e.target.value as EventRuleAction })}
            >
              {EVENT_RULE_ACTIONS.filter(
                (a) => a !== 'config_backup' || features.config_backup || f.action === a,
              ).map((a) => (
                <option key={a} value={a}>
                  {t(`eventRules.actions.${a}`)}
                </option>
              ))}
            </Select>
          </Field>
          {active && f.kind === 'trap' && (
            <Field
              label={t('eventRules.keyVarbind')}
              hint={shown.key ? <Err e={shown} field="key" /> : t('eventRules.keyVarbindHint')}
            >
              <div className="flex gap-1.5">
                <Input
                  value={f.keyVarbindOid}
                  onChange={(e) => set({ keyVarbindOid: e.target.value })}
                  dir="ltr"
                  className="font-mono"
                />
                <Button size="sm" onClick={() => set({ keyVarbindOid: IF_INDEX_OID })}>
                  ifIndex
                </Button>
              </div>
            </Field>
          )}
          {active && f.kind === 'syslog' && (
            <Field
              label={t('eventRules.keyRegex')}
              hint={shown.key ? <Err e={shown} field="key" /> : t('eventRules.keyRegexHint')}
            >
              <Input
                value={f.keyRegex}
                onChange={(e) => set({ keyRegex: e.target.value })}
                maxLength={200}
                dir="ltr"
                className="font-mono"
              />
            </Field>
          )}
        </div>

        {active && (
          <fieldset className="space-y-3 rounded-md border border-slate-200 p-3 dark:border-slate-700">
            <legend className="px-1 text-xs font-semibold text-slate-600 dark:text-slate-400">
              {t('eventRules.clearing')}
            </legend>
            <p className="text-xs text-slate-500">{t('eventRules.clearingHint')}</p>
            <Checkbox
              label={t('eventRules.clearOnMatch')}
              checked={f.hasClearMatch}
              onChange={(v) => set({ hasClearMatch: v })}
            />
            {f.hasClearMatch && (
              <div className="ms-6">
                <MatchEditor
                  kind={f.kind}
                  value={f.clearMatch}
                  onChange={(m) => set({ clearMatch: m })}
                  errors={shown}
                  prefix="clearMatch"
                />
              </div>
            )}
            <Field
              label={t('eventRules.clearAfter')}
              hint={
                shown.clearAfterMin ? (
                  <Err e={shown} field="clearAfterMin" />
                ) : (
                  t('eventRules.clearAfterHint')
                )
              }
              className="max-w-xs"
            >
              <Input
                type="number"
                min={1}
                max={10080}
                value={f.clearAfterMin}
                onChange={(e) => set({ clearAfterMin: e.target.value })}
                dir="ltr"
              />
            </Field>
            <Err e={shown} field="clear" />
          </fieldset>
        )}

        <Checkbox
          label={t('common.enabled')}
          checked={f.enabled}
          onChange={(v) => set({ enabled: v })}
        />
        {testResult && <TestResults r={testResult} />}
        <ErrorBox error={save.error ?? test.error ?? reset.error} />
        {touched && invalid && <Notice>{t('eventRules.fixErrors')}</Notice>}
        <div className="flex flex-wrap justify-end gap-2">
          <Button
            className="me-auto"
            busy={test.isPending}
            disabled={invalid}
            onClick={() => test.mutate()}
            title={t('eventRules.testHint')}
          >
            {t('eventRules.test')}
          </Button>
          {rule?.builtin && (
            <Button busy={reset.isPending} onClick={() => reset.mutate()}>
              {t('eventRules.reset')}
            </Button>
          )}
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="submit" variant="primary" busy={save.isPending}>
            {t('common.save')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function BuiltinBadge() {
  const { t } = useTranslation();
  return (
    <span className="rounded bg-violet-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-violet-800 dark:bg-violet-900/50 dark:text-violet-200">
      {t('eventRules.builtin')}
    </span>
  );
}
