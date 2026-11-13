import { useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { endpoints4 } from '../../api/endpoints4';
import type { AwsAccessMode, AwsConnection } from '../../api/types';
import { accessModesFor, type AccessErrors } from '../../lib/aws';
import { Button, ErrorBox, Field, FieldGroup, Input, Select } from '../ui';
import { AwsPoliciesPanel } from './AwsPoliciesPanel';

/** ExternalId with *Generate* (`POST /api/aws/external-id`, 32 characters; nothing stored). */
export function ExternalIdField({
  value,
  err,
  onChange,
}: {
  value: string;
  err?: string;
  onChange: (v: string) => void;
}) {
  const { t } = useTranslation();
  const gen = useMutation({
    mutationFn: endpoints4.externalId,
    onSuccess: (r) => onChange(r.external_id),
  });
  return (
    <Field
      label={t('aws.access.externalId')}
      hint={
        err ? (
          <span className="text-red-600">{t(`aws.err.${err}`)}</span>
        ) : (
          t('aws.access.externalIdHint')
        )
      }
    >
      <div className="flex gap-2">
        <Input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          dir="ltr"
          autoComplete="off"
          spellCheck={false}
          className="font-mono"
          aria-invalid={!!err}
          data-testid="external-id"
        />
        <Button
          size="sm"
          onClick={() => gen.mutate()}
          busy={gen.isPending}
          data-testid="external-id-generate"
        >
          {t('aws.access.generate')}
        </Button>
      </div>
      <ErrorBox error={gen.error} className="mt-1" />
    </Field>
  );
}

export type AccessValue = {
  /** '' = the default connection */
  connection_id: string;
  mode: AwsAccessMode;
  role_arn: string;
  external_id: string;
};

/**
 * [4] Shared *AWS access* block (architecture § 15.1.10): connection (default marked, link to
 * *Manage connections*), how to reach the service — the connection directly, *Assume a role*
 * (role ARN, ExternalId with *Generate*) or *Another role of the same Roles Anywhere profile*
 * (offered only for a Roles Anywhere connection) — and the expected IAM policies of the hop.
 */
export function AwsAccessBlock({
  value,
  connections,
  errors,
  onChange,
  service = 'Bedrock',
}: {
  value: AccessValue;
  connections: AwsConnection[] | undefined;
  errors: AccessErrors;
  onChange: (v: AccessValue) => void;
  service?: string;
}) {
  const { t } = useTranslation();
  const list = connections ?? [];
  const def = list.find((c) => c.is_default) ?? null;
  const selected = value.connection_id
    ? (list.find((c) => String(c.id) === value.connection_id) ?? null)
    : def;
  const modes = accessModesFor(selected?.auth_mode ?? null);
  const set = (patch: Partial<AccessValue>) => onChange({ ...value, ...patch });
  const errText = (k?: string) =>
    k ? <span className="text-red-600">{t(`aws.err.${k}`)}</span> : undefined;
  // the rendered hop policies are those of sts:AssumeRole (an ra_role hop needs the profile instead)
  const roleOk = value.mode === 'assume_role' && !!value.role_arn.trim() && !errors.role_arn;
  return (
    <div className="space-y-3" data-testid="aws-access">
      {connections && list.length === 0 && (
        <p className="text-xs text-amber-700 dark:text-amber-300" data-testid="no-connection">
          {t('aws.access.noConnection')}
        </p>
      )}
      <div className="flex flex-wrap items-end gap-3">
        <Field label={t('aws.access.connection')} className="min-w-[16rem] flex-1">
          <Select
            value={value.connection_id}
            onChange={(e) => {
              const next = e.target.value;
              const c = next ? list.find((x) => String(x.id) === next) : def;
              // ra_role is only offered for a Roles Anywhere connection
              const mode =
                value.mode === 'ra_role' && c?.auth_mode !== 'roles_anywhere'
                  ? 'direct'
                  : value.mode;
              set({ connection_id: next, mode });
            }}
            data-testid="access-connection"
          >
            <option value="">
              {def
                ? t('aws.access.defaultOption', { name: def.name })
                : t('aws.access.defaultNone')}
            </option>
            {list.map((c) => (
              <option key={c.id} value={String(c.id)}>
                {c.name}
                {c.is_default ? ` (${t('aws.connections.defaultBadge')})` : ''}
              </option>
            ))}
          </Select>
        </Field>
        <Link
          to="/settings?tab=aws"
          className="pb-2 text-xs font-medium text-sky-700 hover:underline dark:text-sky-400"
          data-testid="manage-connections"
        >
          {t('aws.access.manage')}
        </Link>
      </div>
      <FieldGroup label={t('aws.access.how', { service })} hint={errText(errors.mode)}>
        <div role="radiogroup" className="flex flex-col gap-1">
          {modes.map((m) => (
            <label key={m} className="inline-flex items-start gap-2 text-sm">
              <input
                type="radio"
                name="aws_access_mode"
                className="mt-1"
                checked={value.mode === m}
                onChange={() => set({ mode: m })}
                data-testid={`access-${m}`}
              />
              <span>
                <span className="font-medium">{t(`aws.access.mode.${m}`)}</span>
                <span className="block text-xs text-slate-500">
                  {t(`aws.access.modeHint.${m}`)}
                </span>
              </span>
            </label>
          ))}
        </div>
      </FieldGroup>
      {value.mode !== 'direct' && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field
            label={t('aws.access.roleArn')}
            required
            hint={value.role_arn.trim() ? errText(errors.role_arn) : undefined}
          >
            <Input
              value={value.role_arn}
              onChange={(e) => set({ role_arn: e.target.value })}
              dir="ltr"
              placeholder="arn:aws:iam::222222222222:role/netmon-bedrock"
              aria-invalid={!!value.role_arn.trim() && !!errors.role_arn}
              data-testid="access-role-arn"
            />
          </Field>
          {value.mode === 'assume_role' && (
            <ExternalIdField
              value={value.external_id}
              err={errors.external_id}
              onChange={(v) => set({ external_id: v })}
            />
          )}
        </div>
      )}
      {selected && (
        <AwsPoliciesPanel
          connectionId={selected.id}
          roleArn={roleOk ? value.role_arn.trim() : null}
          externalId={roleOk && !errors.external_id ? value.external_id.trim() : null}
        />
      )}
    </div>
  );
}
