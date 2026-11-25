// [profiles] The SNMP version next to the origin line of a group / device page ("Manual (v2c)",
// "Profile Core (v3)", "Inherited from group X · manual (v2c)").
import { useQuery } from '@tanstack/react-query';
import { endpoints, qk } from '../api/endpoints';
import { profilesApi, qkp } from '../api/profiles';
import type { CredentialOrigin, SnmpCredentials, SnmpVersion } from '../api/types';
import { snmpVersionSource } from '../lib/credentials';
import { useAuth } from './auth';

export function useSnmpOriginVersion(
  origin: CredentialOrigin | null | undefined,
  own: SnmpCredentials | null | undefined,
): SnmpVersion | null {
  const { isAdmin } = useAuth();
  const src = snmpVersionSource(origin, isAdmin);
  const groupId = src?.from === 'group' ? src.groupId : 0;
  const profileId = src?.from === 'profile' ? src.profileId : 0;
  const group = useQuery({
    queryKey: qk.group(groupId),
    queryFn: () => endpoints.group(groupId),
    enabled: groupId > 0,
  });
  const profile = useQuery({
    queryKey: qkp.one(profileId),
    queryFn: () => profilesApi.get(profileId),
    enabled: profileId > 0,
  });
  if (!src) return null;
  if (src.from === 'own') return own?.version ?? null;
  if (src.from === 'group') return group.data?.snmp_credentials?.version ?? null;
  const p = profile.data;
  return p?.kind === 'snmp' ? ((p.credentials as SnmpCredentials).version ?? null) : null;
}
