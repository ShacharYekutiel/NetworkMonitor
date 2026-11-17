// [premium] A readable browser / OS from a User-Agent header (the signed-in browsers list).
// Small and local on purpose: only the common browsers and systems, anything else → null.

export type UaInfo = { browser: string | null; os: string | null; mobile: boolean };

const BROWSERS: [RegExp, string][] = [
  [/\bEdg(?:e|A|iOS)?\//, 'Edge'],
  [/\b(?:OPR|Opera)\//, 'Opera'],
  [/\bSamsungBrowser\//, 'Samsung Internet'],
  [/\b(?:Firefox|FxiOS)\//, 'Firefox'],
  [/\b(?:HeadlessChrome|Chrome|CriOS|Chromium)\//, 'Chrome'],
  [/\bVersion\/[\d.]+.*\bSafari\//, 'Safari'],
  [/\bcurl\//, 'curl'],
];

function osOf(ua: string): string | null {
  if (/\biPhone\b/.test(ua)) return 'iPhone';
  if (/\biPad\b/.test(ua)) return 'iPad';
  if (/\bAndroid\b/.test(ua)) return 'Android';
  if (/\bCrOS\b/.test(ua)) return 'ChromeOS';
  if (/\bWindows\b/.test(ua)) return 'Windows';
  if (/\bMac OS X\b|\bMacintosh\b/.test(ua)) return 'macOS';
  if (/\bLinux\b|\bX11\b/.test(ua)) return 'Linux';
  return null;
}

export function parseUserAgent(ua: string | null | undefined): UaInfo | null {
  if (!ua) return null;
  const browser = BROWSERS.find(([re]) => re.test(ua))?.[1] ?? null;
  const os = osOf(ua);
  if (!browser && !os) return null;
  return { browser, os, mobile: /\b(?:Mobile|iPhone|Android)\b/.test(ua) && !/\biPad\b/.test(ua) };
}

/** "Chrome · Windows"; the raw header when nothing is recognised; null without a header. */
export function userAgentLabel(ua: string | null | undefined): string | null {
  if (!ua) return null;
  const p = parseUserAgent(ua);
  if (!p) return ua;
  return [p.browser, p.os].filter(Boolean).join(' · ');
}
