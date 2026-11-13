// [1.4.1] Validation of the Security settings card (the server validates again).

/** scheme://host[:port]; the host may start with "*." (same rule as the server). */
const ORIGIN_RE =
  /^https?:\/\/(\*\.)?[a-z0-9]([a-z0-9-]{0,62})(\.[a-z0-9]([a-z0-9-]{0,62}))*(:\d{1,5})?$/i;

/** One origin per line (or separated by spaces / commas); a trailing "/" is dropped. */
export function parseFrameAncestors(text: string): { origins: string[]; invalid: string[] } {
  const origins: string[] = [];
  const invalid: string[] = [];
  for (const raw of text.split(/[\s,]+/)) {
    const v = raw.trim().replace(/\/+$/, '').toLowerCase();
    if (!v) continue;
    if (!ORIGIN_RE.test(v) || v.length > 255) invalid.push(raw);
    else if (!origins.includes(v)) origins.push(v);
  }
  return { origins, invalid };
}

function intIn(v: string, min: number, max: number): boolean {
  const s = v.trim();
  if (!/^\d+$/.test(s)) return false;
  const n = Number(s);
  return n >= min && n <= max;
}

export function securitySettingsErrors(form: {
  maxAge: string;
  ipCap: string;
  ancestors: string;
}): {
  maxAge: boolean;
  ipCap: boolean;
  ancestors: boolean;
} {
  const parsed = parseFrameAncestors(form.ancestors);
  return {
    maxAge: !intIn(form.maxAge, 1, 720),
    ipCap: !intIn(form.ipCap, 0, 10000),
    ancestors: parsed.invalid.length > 0 || parsed.origins.length > 20,
  };
}
