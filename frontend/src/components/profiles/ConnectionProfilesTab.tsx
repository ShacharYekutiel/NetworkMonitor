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
import { ColumnsMenu, DataTable, labelOf, useDataTable, type DataColumn } from '../kitTable';
import { EditIcon, PlayIcon, PlusIcon, TrashIcon } from '../icons';
import { useToast } from '../toast';
import { Button, Card, ConfirmDialog, Empty, ErrorBox, ICON_BUTTON, Input, Loading } from '../ui';
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
  const cols: DataColumn<ConnectionProfile>[] = [
    {
      id: 'name',
      label: t('profiles.name'),
      minWidth: 200,
      wrap: true,
      cell: (p) => (
        <>
          <div className="font-medium">
            <bdi>{p.name}</bdi>
          </div>
          {p.description && (
            <div className="mt-0.5 max-w-[28rem] text-xs text-ink-muted" dir="auto">
              {p.description}
            </div>
          )}
          {p.credentials.credentials_unreadable && (
            <div className="mt-0.5 text-xs text-status-down">{t('profiles.unreadable')}</div>
          )}
        </>
      ),
    },
    {
      id: 'kind',
      label: t('profiles.kindLabel'),
      width: 90,
      cell: (p) => t(`profiles.kind.${p.kind}`),
    },
    {
      id: 'usedBy',
      label: t('profiles.usedBy'),
      minWidth: 220,
      wrap: true,
      cell: (p) => <UsedBy p={p} onOpen={() => setUsage({ p, blocked: false })} />,
    },
    {
      id: 'devices',
      label: t('profiles.devicesAffected'),
      numeric: true,
      width: 150,
      cell: (p) => p.usage.devices_effective,
    },
    {
      id: 'actions',
      label: t('profiles.actions'),
      pin: 'end',
      fixed: true,
      width: 130,
      cell: (p) => (
        <RowActions
          p={p}
          onTest={() => setTesting(p)}
          onEdit={() => setEditing(p)}
          onDelete={() => askDelete(p)}
        />
      ),
    },
  ];
  const layout = useDataTable('connection-profiles', cols);
  return (
    <Card
      title={t('profiles.title')}
      anchor={cardAnchor('connectionProfiles')}
      actions={
        <>
          <ColumnsMenu layout={layout} labelOf={(id) => labelOf(cols, id)} />
          <Button variant="primary" size="sm" onClick={() => setEditing('new')}>
            <PlusIcon size={14} /> {t('profiles.add')}
          </Button>
        </>
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
          {/* 1.5.1 convention: the shared table, one card per profile when it does not fit */}
          <div className="-mx-[22px]">
            <DataTable
              layout={layout}
              columns={layout.columns}
              rows={items}
              rowKey={(p) => p.id}
              rowProps={(p) => ({ 'data-testid': `profile-row-${p.id}` })}
              testId="profiles-table"
              cardsWhenNarrow
            />
          </div>
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
