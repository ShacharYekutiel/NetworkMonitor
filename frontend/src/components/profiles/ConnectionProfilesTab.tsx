// [profiles] Settings → Connection profiles (admin; always present, no feature toggle).
import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { isApiError } from '../../api/client';
import { profilesApi, qkp } from '../../api/profiles';
import type { ConnectionProfile, CredentialKind } from '../../api/types';
import { CREDENTIAL_KINDS } from '../../api/types';
import { cardAnchor } from '../../lib/settingsIndex';
import { ModeRadios } from '../CredentialModePicker';
import { EditIcon, PlayIcon, PlusIcon, TrashIcon } from '../icons';
import { useToast } from '../toast';
import {
  Button,
  Card,
  ConfirmDialog,
  Empty,
  ErrorBox,
  ICON_BUTTON,
  Input,
  Loading,
  td,
  th,
} from '../ui';
import { ProfileForm } from './ProfileForm';
import { ProfileTestDialog } from './ProfileTestDialog';
import { ProfileUsageDialog } from './ProfileUsageDialog';

type KindFilter = 'all' | CredentialKind;

/** Profiles matching the filters (the list is small; filtered client-side). */
export function filterProfiles(
  items: ConnectionProfile[],
  kind: KindFilter,
  q: string,
): ConnectionProfile[] {
  const needle = q.trim().toLowerCase();
  return items.filter(
    (p) =>
      (kind === 'all' || p.kind === kind) && (!needle || p.name.toLowerCase().includes(needle)),
  );
}

export function usageTotal(p: ConnectionProfile): number {
  return p.usage.groups + p.usage.devices + p.usage.discovery_profiles;
}

function UsedBy({ p, onOpen }: { p: ConnectionProfile; onOpen: () => void }) {
  const { t } = useTranslation();
  if (!usageTotal(p)) return <span className="text-ink-muted">{t('profiles.unused')}</span>;
  return (
    <button
      type="button"
      onClick={onOpen}
      className="text-start text-accent-ink hover:underline"
      aria-label={t('profiles.showUsage', { name: p.name })}
    >
      {t('profiles.usedByText', {
        groups: t('profiles.usedGroups', { count: p.usage.groups }),
        devices: t('profiles.usedDevices', { count: p.usage.devices }),
        discovery: t('profiles.usedDiscovery', { count: p.usage.discovery_profiles }),
      })}
    </button>
  );
}

function RowActions({
  p,
  onTest,
  onEdit,
  onDelete,
}: {
  p: ConnectionProfile;
  onTest: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex items-center justify-end gap-1">
      <Button
        size="sm"
        variant="ghost"
        className={ICON_BUTTON.sm}
        aria-label={t('profiles.testOne', { name: p.name })}
        title={t('profiles.test')}
        onClick={onTest}
      >
        <PlayIcon size={14} className="rtl:rotate-180" />
      </Button>
      <Button
        size="sm"
        variant="ghost"
        className={ICON_BUTTON.sm}
        aria-label={t('profiles.edit', { name: p.name })}
        title={t('common.edit')}
        onClick={onEdit}
      >
        <EditIcon size={14} />
      </Button>
      <Button
        size="sm"
        variant="ghost"
        className={ICON_BUTTON.sm}
        aria-label={t('profiles.delete', { name: p.name })}
        title={t('common.delete')}
        onClick={onDelete}
      >
        <TrashIcon size={14} />
      </Button>
    </div>
  );
}

export function ConnectionProfilesTab() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const [kind, setKind] = useState<KindFilter>('all');
  const [q, setQ] = useState('');
  const [editing, setEditing] = useState<ConnectionProfile | 'new' | null>(null);
  const [testing, setTesting] = useState<ConnectionProfile | null>(null);
  const [usage, setUsage] = useState<{ p: ConnectionProfile; blocked: boolean } | null>(null);
  const [deleting, setDeleting] = useState<ConnectionProfile | null>(null);
  const list = useQuery({ queryKey: qkp.list(), queryFn: () => profilesApi.list() });
  const items = useMemo(
    () => filterProfiles(list.data?.items ?? [], kind, q),
    [list.data, kind, q],
  );

  const del = useMutation({
    mutationFn: (p: ConnectionProfile) => profilesApi.remove(p.id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qkp.all });
      toast.push('success', t('profiles.deleted'));
      setDeleting(null);
    },
    onError: (e, p) => {
      // refused by the server (a use appeared meanwhile): show where it is used
      if (isApiError(e) && e.code === 'profile_in_use') {
        setDeleting(null);
        setUsage({ p, blocked: true });
        void qc.invalidateQueries({ queryKey: qkp.all });
      }
    },
  });
  const askDelete = (p: ConnectionProfile) => {
    del.reset();
    if (usageTotal(p) > 0) setUsage({ p, blocked: true });
    else setDeleting(p);
  };

  const all = list.data?.items ?? [];
  return (
    <Card
      title={t('profiles.title')}
      anchor={cardAnchor('connectionProfiles')}
      actions={
        <Button variant="primary" size="sm" onClick={() => setEditing('new')}>
          <PlusIcon size={14} /> {t('profiles.add')}
        </Button>
      }
    >
      <p className="m-0 mb-4 text-[13.5px] text-ink-muted">{t('profiles.intro')}</p>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <ModeRadios
          label={t('profiles.kindLabel')}
          value={kind}
          onChange={setKind}
          options={[
            { value: 'all' as KindFilter, label: t('profiles.allKinds') },
            ...CREDENTIAL_KINDS.map((k) => ({
              value: k as KindFilter,
              label: t(`profiles.kind.${k}`),
            })),
          ]}
          testId="profiles-kind-filter"
        />
        <Input
          type="search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t('profiles.search')}
          aria-label={t('profiles.search')}
          className="w-full sm:w-56"
        />
      </div>
      {list.isLoading && <Loading />}
      <ErrorBox error={list.error} />
      {list.isSuccess && !all.length && (
        <Empty>
          {t('profiles.none')} {t('profiles.noneHint')}
        </Empty>
      )}
      {list.isSuccess && all.length > 0 && !items.length && <Empty>{t('profiles.noMatch')}</Empty>}
      {items.length > 0 && (
        <>
          {/* wide screens: a table */}
          <div className="-mx-[22px] hidden overflow-x-auto md:block">
            <table className="w-full text-sm" data-testid="profiles-table">
              <thead>
                <tr>
                  <th className={th}>{t('profiles.name')}</th>
                  <th className={th}>{t('profiles.kindLabel')}</th>
                  <th className={th}>{t('profiles.usedBy')}</th>
                  <th className={`${th} text-end`}>{t('profiles.devicesAffected')}</th>
                  <th className={`${th} text-end`}>
                    <span className="sr-only">{t('profiles.actions')}</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((p) => (
                  <tr key={p.id} data-testid={`profile-row-${p.id}`}>
                    <td className={td}>
                      <div className="font-medium">
                        <bdi>{p.name}</bdi>
                      </div>
                      {p.description && (
                        <div className="mt-0.5 max-w-[28rem] text-xs text-ink-muted" dir="auto">
                          {p.description}
                        </div>
                      )}
                      {p.credentials.credentials_unreadable && (
                        <div className="mt-0.5 text-xs text-status-down">
                          {t('profiles.unreadable')}
                        </div>
                      )}
                    </td>
                    <td className={td}>{t(`profiles.kind.${p.kind}`)}</td>
                    <td className={td}>
                      <UsedBy p={p} onOpen={() => setUsage({ p, blocked: false })} />
                    </td>
                    <td className={`${td} text-end tabular-nums`}>{p.usage.devices_effective}</td>
                    <td className={td}>
                      <RowActions
                        p={p}
                        onTest={() => setTesting(p)}
                        onEdit={() => setEditing(p)}
                        onDelete={() => askDelete(p)}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {/* narrow screens: one card per profile (no horizontal scrolling) */}
          <ul
            className="m-0 list-none divide-y divide-line p-0 md:hidden"
            data-testid="profiles-list"
          >
            {items.map((p) => (
              <li key={p.id} className="space-y-1 py-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="break-words font-medium">
                      <bdi>{p.name}</bdi>{' '}
                      <span className="text-xs font-normal text-ink-muted">
                        · {t(`profiles.kind.${p.kind}`)}
                      </span>
                    </div>
                    {p.description && (
                      <div className="text-xs text-ink-muted" dir="auto">
                        {p.description}
                      </div>
                    )}
                  </div>
                  <RowActions
                    p={p}
                    onTest={() => setTesting(p)}
                    onEdit={() => setEditing(p)}
                    onDelete={() => askDelete(p)}
                  />
                </div>
                {p.credentials.credentials_unreadable && (
                  <div className="text-xs text-status-down">{t('profiles.unreadable')}</div>
                )}
                <div className="text-xs">
                  <UsedBy p={p} onOpen={() => setUsage({ p, blocked: false })} />
                </div>
                <div className="text-xs text-ink-muted">
                  {t('profiles.devicesAffected')}: {p.usage.devices_effective}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}

      <ProfileForm
        open={editing !== null}
        onClose={() => setEditing(null)}
        profile={editing && editing !== 'new' ? editing : undefined}
        defaultKind={kind === 'all' ? 'snmp' : kind}
      />
      {testing && (
        <ProfileTestDialog
          open
          onClose={() => setTesting(null)}
          kind={testing.kind}
          name={testing.name}
          profileId={testing.id}
        />
      )}
      <ProfileUsageDialog
        profile={usage?.p ?? null}
        blocked={usage?.blocked}
        onClose={() => setUsage(null)}
      />
      <ConfirmDialog
        open={deleting !== null}
        title={deleting ? t('profiles.deleteTitle', { name: deleting.name }) : ''}
        message={t('profiles.deleteConfirm')}
        onConfirm={() => deleting && del.mutate(deleting)}
        onClose={() => setDeleting(null)}
        busy={del.isPending}
        error={del.error}
      />
    </Card>
  );
}
