// [profiles] "Used by": the groups, devices and discovery profiles that reference a profile.
import { Link } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { profilesApi, qkp } from '../../api/profiles';
import type { ConnectionProfile } from '../../api/types';
import { Button, ErrorBox, Loading, Ltr, Modal, Notice } from '../ui';

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-1.5">
      <h4 className="m-0 text-[13.5px] font-semibold text-ink">{title}</h4>
      <ul className="m-0 list-none space-y-1 p-0 text-sm">{children}</ul>
    </section>
  );
}

export function ProfileUsageDialog({
  profile,
  blocked,
  onClose,
}: {
  profile: ConnectionProfile | null;
  /** opened because a delete was refused (profile_in_use) */
  blocked?: boolean;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const q = useQuery({
    queryKey: qkp.usage(profile?.id ?? 0),
    queryFn: () => profilesApi.usage(profile!.id),
    enabled: !!profile,
  });
  const u = q.data;
  const empty = u && !u.groups.length && !u.devices.length && !u.discovery_profiles.length;
  const link = 'font-medium text-accent-ink hover:underline';
  return (
    <Modal
      open={!!profile}
      onClose={onClose}
      closeOnBackdrop
      testId="profile-usage-dialog"
      title={
        profile
          ? t(blocked ? 'profiles.inUseTitle' : 'profiles.usageTitle', { name: profile.name })
          : ''
      }
      footer={<Button onClick={onClose}>{t('common.close')}</Button>}
    >
      <div className="space-y-4">
        {blocked && <Notice tone="warning">{t('profiles.inUseText')}</Notice>}
        {q.isLoading && <Loading />}
        <ErrorBox error={q.error} />
        {u && (
          <>
            <p className="m-0 text-[13.5px] text-ink-muted">
              {t('profiles.effective', { count: u.devices_effective })}
            </p>
            {empty && <p className="m-0 text-sm">{t('profiles.noUsage')}</p>}
            {u.groups.length > 0 && (
              <Section title={t('profiles.groups')}>
                {u.groups.map((g) => (
                  <li key={g.id} className="min-w-0 break-words">
                    {g.path.length > 0 && (
                      <span className="text-ink-muted">
                        <bdi>{g.path.join(' / ')}</bdi> /{' '}
                      </span>
                    )}
                    <Link to={`/groups/${g.id}`} className={link} onClick={onClose}>
                      <bdi>{g.name}</bdi>
                    </Link>
                  </li>
                ))}
              </Section>
            )}
            {u.devices.length > 0 && (
              <Section title={t('profiles.devices')}>
                {u.devices.map((d) => (
                  <li key={d.id} className="flex min-w-0 flex-wrap items-baseline gap-x-2">
                    <Link to={`/devices/${d.id}`} className={link} onClick={onClose}>
                      <bdi>{d.name}</bdi>
                    </Link>
                    <Ltr className="font-mono text-xs text-ink-muted">{d.host}</Ltr>
                    <span className="text-xs text-ink-muted">
                      · <bdi>{d.group_name}</bdi>
                    </span>
                  </li>
                ))}
              </Section>
            )}
            {u.discovery_profiles.length > 0 && (
              <Section title={t('profiles.discoveryProfiles')}>
                {u.discovery_profiles.map((p) => (
                  <li key={p.id}>
                    <Link to="/discovery" className={link} onClick={onClose}>
                      <bdi>{p.name}</bdi>
                    </Link>
                  </li>
                ))}
              </Section>
            )}
            {u.truncated && <p className="m-0 text-xs text-ink-muted">{t('profiles.truncated')}</p>}
          </>
        )}
      </div>
    </Modal>
  );
}
