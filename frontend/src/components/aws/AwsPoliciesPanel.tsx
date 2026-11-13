import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints4, qk4 } from '../../api/endpoints4';
import type { AwsPolicies } from '../../api/types';
import { noteKey, policyJson } from '../../lib/aws';
import { CopyIcon } from '../icons';
import { cardAnchor } from '../../lib/settingsIndex';
import { useToast } from '../toast';
import { Button, ErrorBox, Spinner } from '../ui';

const PARTS = ['base_trust_policy', 'hop_caller_policy', 'hop_trust_policy'] as const;

/**
 * *Expected IAM policies* (architecture § 15.1.10): the JSON this connection (and one hop)
 * needs, rendered by the server from the stored values (`GET …/policies`), with copy buttons.
 * Collapsible; fetched when opened.
 */
export function AwsPoliciesPanel({
  connectionId,
  roleArn,
  externalId,
  defaultOpen = false,
}: {
  connectionId: number;
  roleArn?: string | null;
  externalId?: string | null;
  defaultOpen?: boolean;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const [open, setOpen] = useState(defaultOpen);
  const role = roleArn?.trim() || '';
  const ext = externalId?.trim() || '';
  const q = useQuery({
    queryKey: qk4.policies(connectionId, role, ext),
    queryFn: () => endpoints4.policies(connectionId, role, ext),
    enabled: open,
  });
  const copy = (p: Record<string, unknown>) => {
    void navigator.clipboard?.writeText(policyJson(p));
    toast.push('success', t('assistant.copied'));
  };
  return (
    <details
      className="rounded-md border border-slate-200 dark:border-slate-700"
      open={open}
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
      data-anchor={cardAnchor('awsPolicies')}
      data-testid="aws-policies"
    >
      <summary className="cursor-pointer select-none px-3 py-2 text-sm font-semibold">
        {t('aws.policies.title')}
      </summary>
      <div className="space-y-3 border-t border-slate-200 p-3 text-xs dark:border-slate-700">
        <p className="text-slate-500">{t('aws.policies.intro')}</p>
        {q.isLoading && <Spinner className="h-4 w-4" />}
        <ErrorBox error={q.error} />
        {q.data && <PolicyParts p={q.data} onCopy={copy} />}
      </div>
    </details>
  );
}

function PolicyParts({
  p,
  onCopy,
}: {
  p: AwsPolicies;
  onCopy: (p: Record<string, unknown>) => void;
}) {
  const { t } = useTranslation();
  const shown = PARTS.filter((k) => p[k]);
  return (
    <>
      {shown.length === 0 && <p className="text-slate-500">{t('aws.policies.none')}</p>}
      {shown.map((k) => (
        <section key={k} className="space-y-1" data-policy={k}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h5 className="font-medium">{t(`aws.policies.${k}`)}</h5>
            <Button size="sm" onClick={() => onCopy(p[k]!)} data-testid={`copy-${k}`}>
              <CopyIcon size={12} /> {t('aws.policies.copy')}
            </Button>
          </div>
          <pre
            dir="ltr"
            className="max-h-72 overflow-auto rounded bg-slate-50 p-2 text-start font-mono text-[11px] leading-snug text-slate-800 ring-1 ring-inset ring-slate-200 dark:bg-slate-950 dark:text-slate-200 dark:ring-slate-800"
          >
            {policyJson(p[k]!)}
          </pre>
        </section>
      ))}
      {p.notes.length > 0 && (
        <div className="space-y-0.5" data-testid="policy-notes">
          <p className="font-medium text-slate-500">{t('aws.policies.notes')}</p>
          <ul className="list-disc ps-4 text-slate-500">
            {p.notes.map((n) => {
              const k = noteKey(n);
              return k ? (
                <li key={n} data-note={k}>
                  {t(`aws.policies.note.${k}`)}
                </li>
              ) : (
                <li key={n} dir="ltr" className="text-start">
                  {n}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </>
  );
}
