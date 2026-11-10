import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { isApiError } from '../api/client';
import { endpoints3 } from '../api/endpoints3';
import type { Sensor } from '../api/types';
import { AckDetails } from '../components/Acknowledge';
import { StatusBadge } from '../components/Status';
import { Button, ErrorBox, Field, Loading, Notice, Textarea } from '../components/ui';
import { useFeatures } from '../hooks/features';
import { formatDateTime, formatRelative } from '../lib/format';
import { ACK_TOKEN_KEY, ackTokenFromHash } from '../lib/kiosk';
import { AuthShell } from './AuthShell';

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
  return (
    <AuthShell title={t('chatAck.pageTitle')}>
      <div className="space-y-4" data-testid="ack-page">
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
              <>
                <div className="space-y-1 text-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <StatusBadge status={(done ?? r.sensor).status} />
                    <Link
                      to={`/sensors/${r.sensor.id}`}
                      className="font-semibold text-sky-700 hover:underline dark:text-sky-400"
                    >
                      {r.device_name} · {r.sensor.name}
                    </Link>
                  </div>
                  <p className="text-slate-600 dark:text-slate-300">
                    <bdi dir="auto">{r.sensor.status_message}</bdi>
                  </p>
                  {r.problem_since && (
                    <p className="text-xs text-slate-500">
                      {t('chatAck.since', {
                        time: formatDateTime(r.problem_since),
                        rel: formatRelative(r.problem_since),
                      })}
                    </p>
                  )}
                </div>
                {!r.active && !done && <Notice>{t('chatAck.notActive')}</Notice>}
                {ackNow && (
                  <div className="rounded-md bg-sky-50 p-3 text-sm ring-1 ring-inset ring-sky-200 dark:bg-sky-950/40 dark:ring-sky-900">
                    <p className="mb-1 font-medium">{t('chatAck.already')}</p>
                    <AckDetails ack={ackNow} />
                  </div>
                )}
                {done && <Notice tone="success">{t('chatAck.done')}</Notice>}
                {r.active && !ackNow && !done && (
                  <>
                    {r.can_acknowledge ? (
                      <form
                        className="space-y-3"
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
                            onChange={(e) => setComment(e.target.value)}
                            dir="auto"
                          />
                        </Field>
                        <ErrorBox error={ack.error} />
                        <Button
                          type="submit"
                          variant="primary"
                          className="w-full"
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
              </>
            )}
          </>
        )}
        <p className="text-center text-xs">
          <Link to="/" className="text-sky-700 hover:underline dark:text-sky-400">
            {t('chatAck.openApp')}
          </Link>
        </p>
      </div>
    </AuthShell>
  );
}
