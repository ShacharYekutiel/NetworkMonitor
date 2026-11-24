import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { isApiError } from '../api/client';
import { endpoints3 } from '../api/endpoints3';
import type { Sensor } from '../api/types';
import { AckDetails } from '../components/Acknowledge';
import { LanguageSwitcher, ThemeToggle } from '../components/Controls';
import { Mark } from '../components/Layout';
import { Button, cx, ErrorBox, Field, Loading, Notice, TAG, Textarea } from '../components/ui';
import { useFeatures } from '../hooks/features';
import { useLocalizeMessage } from '../hooks/localize';
import { useSensorTypes } from '../hooks/queries';
import { formatDateTime, formatRelative } from '../lib/format';
import { ACK_TOKEN_KEY, ackTokenFromHash } from '../lib/kiosk';
import { STATUS_SOFT, statusClass } from '../lib/status';
import { AUTH_WIDE } from './AuthShell';

function readToken(): string | null {
  const h = ackTokenFromHash(window.location.hash);
  if (h) return h;
  try {
    return sessionStorage.getItem(ACK_TOKEN_KEY);
  } catch {
    return null;
  }
}

/** [3] Ack page (/ack#<token>): problem summary + Acknowledge (admin), outside the app shell. */
export function AckPage() {
  const { t } = useTranslation();
  useSensorTypes(); // [1.3.2] outside the app shell: load the named value maps here
  const localize = useLocalizeMessage();
  const { features } = useFeatures();
  const [token] = useState(readToken);
  const [comment, setComment] = useState('');
  const [done, setDone] = useState<Sensor | null>(null);
  const q = useQuery({
    queryKey: ['chat-ack-resolve', token],
    queryFn: () => endpoints3.resolveAck(token as string),
    enabled: !!token,
    retry: false,
    staleTime: Infinity,
  });
  const ack = useMutation({
    mutationFn: () => endpoints3.acknowledgeAck(token as string, comment.trim()),
    onSuccess: (s) => {
      setDone(s);
      try {
        sessionStorage.removeItem(ACK_TOKEN_KEY);
      } catch {
        /* ignore */
      }
    },
  });
  const expired =
    !token ||
    (isApiError(q.error) && (q.error.code === 'ack_expired' || q.error.code === 'ack_invalid'));
  const r = q.data;
  const ackNow = done?.acknowledgement ?? r?.acknowledgement ?? null;
  const st = (done ?? r?.sensor)?.status;
  return (
    // prototype "chatack": a phone-width column on the grey stage, the problem as one card
    <div className="flex min-h-screen items-center justify-center bg-bg px-4 py-8">
      <div className="grid w-full max-w-[390px] gap-4" data-testid="ack-page">
        <div className="flex items-center justify-between gap-2">
          <span className="flex items-center gap-2" dir="ltr">
            <Mark size={22} />
            <span className="num text-[15px] font-semibold">Network Monitor</span>
          </span>
          <span className="flex gap-2">
            <LanguageSwitcher />
            <ThemeToggle />
          </span>
        </div>
        <h1 className="sr-only">{t('chatAck.pageTitle')}</h1>
        {!features.chat_ack &&
        q.isError &&
        isApiError(q.error) &&
        q.error.code === 'feature_disabled' ? (
          <Notice>{t('chatAck.off')}</Notice>
        ) : expired ? (
          <Notice>{t('chatAck.expired')}</Notice>
        ) : (
          <>
            {q.isLoading && <Loading />}
            <ErrorBox error={q.error} />
            {r && (
              <section className="grid gap-3.5 rounded-xl bg-surface p-[22px] shadow-card">
                {st && (
                  <span
                    className={cx(TAG, statusClass(st, STATUS_SOFT), 'justify-self-start')}
                    title={r.problem_since ? formatDateTime(r.problem_since) : undefined}
                  >
                    {t(`status.${st}`)}
                    {r.problem_since && ` · ${formatRelative(r.problem_since)}`}
                  </span>
                )}
                <Link
                  to={`/sensors/${r.sensor.id}`}
                  className="text-[22px] font-semibold leading-tight text-ink no-underline hover:underline"
                >
                  <bdi dir="auto" className="font-display">
                    {r.device_name} · {r.sensor.name}
                  </bdi>
                </Link>
                <p className="m-0 text-ink-muted">
                  <bdi dir="auto">{localize(r.sensor.status_message)}</bdi>
                </p>
                {r.problem_since && (
                  <p className="m-0 text-xs text-ink-subtle">
                    {t('chatAck.since', {
                      time: formatDateTime(r.problem_since),
                      rel: formatRelative(r.problem_since),
                    })}
                  </p>
                )}
                {!r.active && !done && <Notice>{t('chatAck.notActive')}</Notice>}
                {ackNow && (
                  <div className="rounded-lg bg-accent-soft px-4 py-3 text-sm">
                    <p className="mb-1 font-medium">{t('chatAck.already')}</p>
                    <AckDetails ack={ackNow} />
                  </div>
                )}
                {done && <Notice tone="success">{t('chatAck.done')}</Notice>}
                {r.active && !ackNow && !done && (
                  <>
                    {r.can_acknowledge ? (
                      <form
                        className="grid gap-3"
                        onSubmit={(e) => {
                          e.preventDefault();
                          ack.mutate();
                        }}
                      >
                        <Field label={t('chatAck.comment')}>
                          <Textarea
                            rows={2}
                            maxLength={1000}
                            value={comment}
                            placeholder={t('chatAck.commentPlaceholder')}
                            onChange={(e) => setComment(e.target.value)}
                            dir="auto"
                          />
                        </Field>
                        <ErrorBox error={ack.error} />
                        <Button
                          type="submit"
                          variant="primary"
                          className={AUTH_WIDE}
                          busy={ack.isPending}
                        >
                          {t('chatAck.acknowledge')}
                        </Button>
                      </form>
                    ) : (
                      <Notice>{t('chatAck.adminOnly')}</Notice>
                    )}
                  </>
                )}
              </section>
            )}
          </>
        )}
        <p className="m-0 text-center text-[13px]">
          <Link to="/" className="text-ink-muted hover:text-ink">
            {t('chatAck.openApp')}
          </Link>
        </p>
      </div>
    </div>
  );
}
