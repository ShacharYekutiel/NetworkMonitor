import { describe, expect, it } from 'vitest';
import { parseUserAgent, userAgentLabel } from './userAgent';

const CHROME_WIN =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';
const EDGE_WIN = `${CHROME_WIN} Edg/131.0.2903.70`;
const SAFARI_IPHONE =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1';
const FIREFOX_LINUX = 'Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0';
const SAFARI_MAC =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15';
const CHROME_ANDROID =
  'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36';
const HEADLESS =
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/131.0.6778.33 Safari/537.36';

describe('userAgentLabel (signed-in browsers)', () => {
  it('names the common browsers and systems', () => {
    expect(userAgentLabel(CHROME_WIN)).toBe('Chrome · Windows');
    expect(userAgentLabel(EDGE_WIN)).toBe('Edge · Windows');
    expect(userAgentLabel(SAFARI_IPHONE)).toBe('Safari · iPhone');
    expect(userAgentLabel(FIREFOX_LINUX)).toBe('Firefox · Linux');
    expect(userAgentLabel(SAFARI_MAC)).toBe('Safari · macOS');
    expect(userAgentLabel(CHROME_ANDROID)).toBe('Chrome · Android');
    expect(userAgentLabel(HEADLESS)).toBe('Chrome · Linux');
  });
  it('marks phones', () => {
    expect(parseUserAgent(SAFARI_IPHONE)?.mobile).toBe(true);
    expect(parseUserAgent(CHROME_ANDROID)?.mobile).toBe(true);
    expect(parseUserAgent(CHROME_WIN)?.mobile).toBe(false);
  });
  it('falls back to the raw header, and to null without one', () => {
    expect(userAgentLabel('python-requests/2.32')).toBe('python-requests/2.32');
    expect(userAgentLabel('curl/8.5.0')).toBe('curl');
    expect(userAgentLabel(null)).toBeNull();
    expect(userAgentLabel('')).toBeNull();
  });
});
