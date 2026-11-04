import { describe, expect, it } from 'vitest';
import i18n from '../i18n';
import { ApiError } from '../api/client';
import { errorMessage } from './errors';

describe('errorMessage', () => {
  it('translates by code', async () => {
    await i18n.changeLanguage('en');
    expect(errorMessage(i18n.t, new ApiError(401, 'invalid_credentials', 'Login failed'))).toBe(
      'Wrong username or password.',
    );
    await i18n.changeLanguage('he');
    expect(errorMessage(i18n.t, new ApiError(401, 'invalid_credentials', 'Login failed'))).toBe(
      'שם המשתמש או הסיסמה שגויים.',
    );
    await i18n.changeLanguage('en');
  });

  it('falls back to the server message for unknown codes', () => {
    expect(errorMessage(i18n.t, new ApiError(400, 'brand_new_code', 'Server says no'))).toBe(
      'Server says no',
    );
  });

  it('appends validation details and upstream reasons', () => {
    const v = new ApiError(422, 'validation_error', 'x', [
      { loc: ['body', 'interval_s'], msg: 'must be >= 10' },
    ]);
    expect(errorMessage(i18n.t, v)).toContain('interval_s: must be >= 10');
    const s = new ApiError(502, 'snmp_error', 'timeout');
    expect(errorMessage(i18n.t, s)).toBe('SNMP request failed: timeout');
  });
});
