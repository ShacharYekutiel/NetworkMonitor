// [2b-3] Microsoft Teams (Workflows) webhook URL checks. Pure.
export function urlHost(url: string): string | null {
  try {
    return new URL(url.trim()).hostname.toLowerCase();
  } catch {
    return null;
  }
}

/** Retiring Office 365 connector hosts (api.md: legacy when the host ends with these). */
export function isLegacyTeamsHost(host: string | null | undefined): boolean {
  const h = (host ?? '').toLowerCase();
  return h.endsWith('webhook.office.com') || h.endsWith('outlook.office.com');
}

/** null when OK, else an i18n key. `required` on create (a stored URL is kept on edit). */
export function teamsUrlError(url: string, required: boolean): string | null {
  const v = url.trim();
  if (!v) return required ? 'validation.required' : null;
  if (v.length > 2048) return 'teams.urlLong';
  if (!/^https:\/\//i.test(v)) return 'teams.urlHttps';
  return urlHost(v) ? null : 'teams.urlInvalid';
}
