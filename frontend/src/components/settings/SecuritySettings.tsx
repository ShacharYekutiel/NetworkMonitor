// [1.4.1] Settings → General → Security: the hardening options that could break a legitimate
// deployment are settings with a secure default (architecture § Security hardening).
import { useEffect, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import type { Settings } from '../../api/types';
import { cardAnchor } from '../../lib/settingsIndex';
import { parseFrameAncestors, securitySettingsErrors } from '../../lib/security';
import { useSaveSettings } from '../features/common';
import { Button, Card, Checkbox, ErrorBox, Field, Input, Textarea } from '../ui';

export function SecurityCard({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const init = () => ({
    loopback: settings.egress_allow_loopback ?? false,
    maxAge: String(settings.session_max_age_h ?? 168),
    ipCap: String(settings.login_ip_max_failures ?? 20),
    ancestors: (settings.frame_ancestors ?? []).join('\n'),
  });
  const [form, setForm] = useState(init);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => setForm(init()), [settings]);
  const save = useSaveSettings();
  const err = securitySettingsErrors(form);
  const invalid = err.maxAge || err.ipCap || err.ancestors;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (invalid) return;
    save.mutate({
      egress_allow_loopback: form.loopback,
      session_max_age_h: Number(form.maxAge),
      login_ip_max_failures: Number(form.ipCap),
      frame_ancestors: parseFrameAncestors(form.ancestors).origins,
    });
  };
  const hint = (bad: boolean, ok: string, errKey: string) =>
    bad ? <span className="text-status-down">{t(errKey)}</span> : ok;
  return (
    <Card title={t('settings.security.title')} anchor={cardAnchor('security')}>
      <form onSubmit={submit} className="space-y-4" data-testid="security-settings">
        <p className="text-xs text-ink-muted">{t('settings.security.intro')}</p>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field
            label={t('settings.security.sessionMaxAge')}
            hint={hint(
              err.maxAge,
              t('settings.security.sessionMaxAgeHint'),
              'settings.security.sessionMaxAgeErr',
            )}
          >
            <Input
              type="number"
              min={1}
              max={720}
              value={form.maxAge}
              onChange={(e) => setForm((x) => ({ ...x, maxAge: e.target.value }))}
              dir="ltr"
              aria-invalid={err.maxAge}
              data-anchor="security-session-max-age"
            />
          </Field>
          <Field
            label={t('settings.security.loginIpCap')}
            hint={hint(
              err.ipCap,
              t('settings.security.loginIpCapHint'),
              'settings.security.loginIpCapErr',
            )}
          >
            <Input
              type="number"
              min={0}
              max={10000}
              value={form.ipCap}
              onChange={(e) => setForm((x) => ({ ...x, ipCap: e.target.value }))}
              dir="ltr"
              aria-invalid={err.ipCap}
              data-anchor="security-login-ip-cap"
            />
          </Field>
        </div>
        <div data-anchor="security-allow-loopback">
          <Checkbox
            label={t('settings.security.allowLoopback')}
            checked={form.loopback}
            onChange={(v) => setForm((x) => ({ ...x, loopback: v }))}
          />
          <p className="mt-1 text-xs text-ink-muted">{t('settings.security.allowLoopbackHint')}</p>
        </div>
        <Field
          label={t('settings.security.frameAncestors')}
          hint={hint(
            err.ancestors,
            t('settings.security.frameAncestorsHint'),
            'settings.security.frameAncestorsErr',
          )}
        >
          <Textarea
            rows={3}
            value={form.ancestors}
            onChange={(e) => setForm((x) => ({ ...x, ancestors: e.target.value }))}
            dir="ltr"
            placeholder="https://wall.example.com"
            aria-invalid={err.ancestors}
            data-anchor="security-frame-ancestors"
          />
        </Field>
        <ErrorBox error={save.error} />
        <div className="flex justify-end">
          <Button type="submit" variant="primary" busy={save.isPending} disabled={invalid}>
            {t('common.save')}
          </Button>
        </div>
      </form>
    </Card>
  );
}
