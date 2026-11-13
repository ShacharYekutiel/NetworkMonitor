import { describe, expect, it } from 'vitest';
import { parseFrameAncestors, securitySettingsErrors } from './security';

describe('[1.4.1] security settings', () => {
  it('parses frame ancestors: one origin per line, normalised, de-duplicated', () => {
    expect(
      parseFrameAncestors(
        'https://Wall.example.com/\nhttps://*.noc.example:8443, https://wall.example.com',
      ),
    ).toEqual({
      origins: ['https://wall.example.com', 'https://*.noc.example:8443'],
      invalid: [],
    });
    expect(parseFrameAncestors('').origins).toEqual([]);
  });

  it('rejects anything that is not a plain origin', () => {
    for (const bad of [
      '*',
      'wall.example.com',
      'https://a.example/path',
      "'none'",
      'javascript:alert(1)',
      'https://a.example;script-src',
    ])
      expect(parseFrameAncestors(bad).invalid, bad).toHaveLength(1);
  });

  it('validates the numeric fields', () => {
    const ok = { maxAge: '168', ipCap: '20', ancestors: '' };
    expect(securitySettingsErrors(ok)).toEqual({ maxAge: false, ipCap: false, ancestors: false });
    expect(securitySettingsErrors({ ...ok, maxAge: '0' }).maxAge).toBe(true);
    expect(securitySettingsErrors({ ...ok, maxAge: '721' }).maxAge).toBe(true);
    expect(securitySettingsErrors({ ...ok, ipCap: '0' }).ipCap).toBe(false);
    expect(securitySettingsErrors({ ...ok, ipCap: '-1' }).ipCap).toBe(true);
    expect(securitySettingsErrors({ ...ok, ancestors: 'nope' }).ancestors).toBe(true);
  });
});
