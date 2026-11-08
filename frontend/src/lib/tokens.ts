// [2b-3] API tokens. Pure.
import type { ApiToken, Role } from '../api/types';

/** A token's role may not exceed its creator's. */
export function tokenRoles(own: Role): Role[] {
  return own === 'admin' ? ['viewer', 'admin'] : ['viewer'];
}

/** 1–365 days. */
export function expiresDaysOk(s: string): boolean {
  if (!/^\d+$/.test(s.trim())) return false;
  const n = Number(s);
  return n >= 1 && n <= 365;
}

export function tokenNameOk(s: string): boolean {
  const v = s.trim();
  return v.length >= 1 && v.length <= 128;
}

/** Active first, then newest. */
export function sortTokens(items: ApiToken[]): ApiToken[] {
  const rank = { active: 0, expired: 1, revoked: 2 } as const;
  return [...items].sort(
    (a, b) => rank[a.state] - rank[b.state] || b.created_at.localeCompare(a.created_at),
  );
}

/** Whole days until expiry (negative when past). */
export function daysLeft(expiresAt: string, now = Date.now()): number {
  return Math.floor((Date.parse(expiresAt) - now) / 86_400_000);
}
