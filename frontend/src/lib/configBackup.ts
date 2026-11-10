// [3] Config backup helpers (architecture.md § 13.2, api.md § [3] Config backup). Pure.
import type {
  BackupResult,
  BackupStatus,
  SshCredentials,
  SshCredentialsInput,
} from '../api/types3';

// ---------------------------------------------------------------------------
// SSH credentials form (secret rules as SnmpCredentials: omitted keep, "" clears, string replaces)

export type SecretState = { value: string; clear: boolean };
export type SshForm = {
  /** false = inherit (sent as null) */
  own: boolean;
  username: string;
  port: string;
  password: SecretState;
  private_key: SecretState;
  passphrase: SecretState;
  enable_password: SecretState;
  legacy_algorithms: boolean;
};

const blank = (): SecretState => ({ value: '', clear: false });

export function sshFormFrom(c: SshCredentials | null | undefined): SshForm {
  return {
    own: !!c,
    username: c?.username ?? '',
    port: String(c?.port ?? 22),
    password: blank(),
    private_key: blank(),
    passphrase: blank(),
    enable_password: blank(),
    legacy_algorithms: c?.legacy_algorithms ?? false,
  };
}

const SECRETS = ['password', 'private_key', 'passphrase', 'enable_password'] as const;

function secretOut(s: SecretState): string | undefined {
  if (s.clear) return '';
  return s.value ? s.value : undefined;
}

/** null = inherit; secrets only when typed (or "" to clear). */
export function sshFormToInput(f: SshForm): SshCredentialsInput | null {
  if (!f.own) return null;
  const out: SshCredentialsInput = {
    username: f.username.trim(),
    port: Number(f.port) || 22,
    legacy_algorithms: f.legacy_algorithms,
  };
  for (const k of SECRETS) {
    const v = secretOut(f[k]);
    if (v !== undefined) out[k] = v;
  }
  return out;
}

/** Whether a secret will be set after saving. */
function willHave(s: SecretState, isSet: boolean): boolean {
  if (s.clear) return false;
  return !!s.value || isSet;
}

/** i18n keys of the problems (data-model § [3] SSH credentials validation). */
export function sshFormErrors(f: SshForm, existing: SshCredentials | null | undefined): string[] {
  if (!f.own) return [];
  const errs: string[] = [];
  const u = f.username.trim();
  if (!u || u.length > 64 || !/^[\x21-\x7e]+$/.test(u)) errs.push('ssh.err.username');
  const p = Number(f.port);
  if (!/^\d+$/.test(f.port.trim()) || p < 1 || p > 65535) errs.push('ssh.err.port');
  const hasPw = willHave(f.password, !!existing?.password_set);
  const hasKey = willHave(f.private_key, !!existing?.private_key_set);
  if (!hasPw && !hasKey) errs.push('ssh.err.secret');
  if (f.private_key.value && f.private_key.value.length > 16384) errs.push('ssh.err.keyLong');
  if (f.private_key.value && !/-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/.test(f.private_key.value))
    errs.push('ssh.err.keyFormat');
  return errs;
}

// ---------------------------------------------------------------------------
// Unified diff

export type DiffRow = {
  type: 'hunk' | 'context' | 'add' | 'del' | 'meta';
  text: string;
  oldNo: number | null;
  newNo: number | null;
};

const HUNK_RE = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

/** Parses a unified diff (difflib output) into numbered rows. */
export function parseUnifiedDiff(diff: string): DiffRow[] {
  const rows: DiffRow[] = [];
  let o = 0;
  let n = 0;
  for (const line of diff.replace(/\r\n/g, '\n').split('\n')) {
    if (line.startsWith('--- ') || line.startsWith('+++ ')) {
      rows.push({ type: 'meta', text: line, oldNo: null, newNo: null });
      continue;
    }
    const h = HUNK_RE.exec(line);
    if (h) {
      o = Number(h[1]);
      n = Number(h[2]);
      rows.push({ type: 'hunk', text: line, oldNo: null, newNo: null });
      continue;
    }
    if (line.startsWith('+'))
      rows.push({ type: 'add', text: line.slice(1), oldNo: null, newNo: n++ });
    else if (line.startsWith('-'))
      rows.push({ type: 'del', text: line.slice(1), oldNo: o++, newNo: null });
    else if (line.startsWith(' '))
      rows.push({ type: 'context', text: line.slice(1), oldNo: o++, newNo: n++ });
    else if (line.startsWith('\\'))
      rows.push({ type: 'meta', text: line, oldNo: null, newNo: null });
    // empty trailing line: ignored
  }
  return rows;
}

export type SidePair = { left: DiffRow | null; right: DiffRow | null; hunk?: string };

/** Pairs deletions with following additions for a side-by-side view. */
export function sideBySide(rows: DiffRow[]): SidePair[] {
  const out: SidePair[] = [];
  let dels: DiffRow[] = [];
  let adds: DiffRow[] = [];
  const flush = () => {
    const len = Math.max(dels.length, adds.length);
    for (let i = 0; i < len; i++) out.push({ left: dels[i] ?? null, right: adds[i] ?? null });
    dels = [];
    adds = [];
  };
  for (const r of rows) {
    if (r.type === 'del') {
      if (adds.length) flush();
      dels.push(r);
    } else if (r.type === 'add') adds.push(r);
    else {
      flush();
      if (r.type === 'hunk') out.push({ left: null, right: null, hunk: r.text });
      else if (r.type === 'context') out.push({ left: r, right: r });
    }
  }
  flush();
  return out;
}

// ---------------------------------------------------------------------------
// Search highlight

export type Segment = { text: string; hit: boolean };

/** Splits a line into highlighted / plain segments (case-insensitive; invalid regex → no hits). */
export function highlight(text: string, q: string, regex: boolean): Segment[] {
  if (!q) return [{ text, hit: false }];
  let re: RegExp;
  try {
    re = new RegExp(regex ? q : q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
  } catch {
    return [{ text, hit: false }];
  }
  const out: Segment[] = [];
  let last = 0;
  for (const m of text.matchAll(re)) {
    const i = m.index ?? 0;
    if (!m[0].length) continue;
    if (i > last) out.push({ text: text.slice(last, i), hit: false });
    out.push({ text: m[0], hit: true });
    last = i + m[0].length;
  }
  if (last < text.length) out.push({ text: text.slice(last), hit: false });
  return out.length ? out : [{ text, hit: false }];
}

/** null when the search box is valid (2–200 chars; the regex compiles), else an i18n key. */
export function searchError(q: string, regex: boolean): string | null {
  const v = q.trim();
  if (v.length < 2) return 'configBackup.searchShort';
  if (v.length > 200) return 'configBackup.searchLong';
  if (regex) {
    try {
      new RegExp(v);
    } catch {
      return 'configBackup.searchRegex';
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Status

export function resultTone(r: BackupResult | null): 'ok' | 'muted' | 'error' | 'warn' {
  switch (r) {
    case 'ok':
      return 'ok';
    case 'unchanged':
      return 'muted';
    case 'failed':
      return 'error';
    case 'cancelled':
      return 'warn';
    default:
      return 'muted';
  }
}

/** A host key that needs the admin (changed since pinning). */
export function hostKeyChanged(s: Pick<BackupStatus, 'host_key'>): boolean {
  return s.host_key?.state === 'changed';
}

/** "+12 −3" (true minus sign). */
export function changeStat(added: number, removed: number): string {
  return `+${added} −${removed}`;
}
