import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../../api/endpoints';
import type { UnknownSource } from '../../api/types';
import { useAuth } from '../../hooks/auth';
import { errorMessage } from '../../lib/errors';
import { formatDateTime, formatRelative } from '../../lib/format';
import { DevicePicker } from '../Dependency';
import { useToast } from '../toast';
import {
  Button,
  Card,
  Empty,
  ErrorBox,
  Loading,
  Modal,
  Notice,
  Select,
  Table,
  td,
  th,
} from '../ui';

function AssignModal({ src, onClose }: { src: UnknownSource | null; onClose: () => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const [device, setDevice] = useState<number | null>(null);
  const m = useMutation({
    mutationFn: () => endpoints.assignSource(src!.source_ip, device!),
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ['unknown-sources'] });
      void qc.invalidateQueries({ queryKey: qk.device(r.device_id) });
      toast.push('success', t('unknownSources.assigned'));
      onClose();
    },
  });
  return (
    <Modal
      open={!!src}
      onClose={() => {
        m.reset();
        setDevice(null);
        onClose();
      }}
      title={t('unknownSources.assignTitle', { ip: src?.source_ip ?? '' })}
      footer={
        <>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button
            variant="primary"
            disabled={device === null}
            busy={m.isPending}
            onClick={() => m.mutate()}
          >
            {t('unknownSources.assign')}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <p className="text-sm text-slate-600 dark:text-slate-300">
          {t('unknownSources.assignHint')}
        </p>
        <DevicePicker value={device} onChange={setDevice} label={t('logs.device')} />
        <ErrorBox error={m.error} />
      </div>
    </Modal>
  );
}

/** [2b-3] Senders not attributed to a device: assign to a device or ignore. */
export function UnknownSourcesView() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const toast = useToast();
  const [days, setDays] = useState(7);
  const [assigning, setAssigning] = useState<UnknownSource | null>(null);
  const q = useQuery({
    queryKey: qk.unknownSources(days),
    queryFn: () => endpoints.unknownSources(days),
  });
  // "Ignore" appends to settings.ingest_ignored_sources (admin).
  const ignore = useMutation({
    mutationFn: async (ip: string) => {
      const s = await endpoints.settings();
      const list = s.ingest_ignored_sources ?? [];
      if (list.includes(ip)) return s;
      return endpoints.updateSettings({ ingest_ignored_sources: [...list, ip] });
    },
    onSuccess: (s) => {
      qc.setQueryData(qk.settings, s);
      void qc.invalidateQueries({ queryKey: ['unknown-sources'] });
      toast.push('success', t('unknownSources.ignored'));
    },
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  return (
    <div className="space-y-3" data-testid="unknown-sources">
      <div className="flex flex-wrap items-center gap-2">
        <p className="flex-1 text-sm text-slate-600 dark:text-slate-300">
          {t('unknownSources.intro')}
        </p>
        <Select
          value={String(days)}
          onChange={(e) => setDays(Number(e.target.value))}
          className="w-36"
          aria-label={t('unknownSources.days')}
        >
          {[1, 7, 30].map((d) => (
            <option key={d} value={d}>
              {t('unknownSources.lastDays', { n: d })}
            </option>
          ))}
        </Select>
      </div>
      <Card bodyClassName="p-0">
        {q.isLoading && <Loading />}
        <ErrorBox error={q.error} className="m-3" />
        {q.data && !q.data.items.length && <Empty>{t('unknownSources.none')}</Empty>}
        {q.data && q.data.items.length > 0 && (
          <Table>
            <thead>
              <tr>
                <th className={th}>{t('logs.sourceIp')}</th>
                <th className={th}>{t('unknownSources.lastHostname')}</th>
                <th className={th}>{t('unknownSources.syslogCount')}</th>
                <th className={th}>{t('unknownSources.trapCount')}</th>
                <th className={th}>{t('unknownSources.lastSeen')}</th>
                <th className={th} />
              </tr>
            </thead>
            <tbody>
              {q.data.items.map((s) => (
                <tr key={s.source_ip} className={s.ignored ? 'opacity-60' : undefined}>
                  <td className={`${td} font-mono`}>
                    <bdi dir="ltr">{s.source_ip}</bdi>
                    {s.ignored && (
                      <span className="ms-2 rounded bg-slate-100 px-1 text-[10px] text-slate-500 dark:bg-slate-800">
                        {t('unknownSources.ignoredBadge')}
                      </span>
                    )}
                  </td>
                  <td className={td}>
                    <bdi dir="ltr">{s.last_hostname || '—'}</bdi>
                  </td>
                  <td className={`${td} tabular-nums`}>{s.syslog_count}</td>
                  <td className={`${td} tabular-nums`}>{s.trap_count}</td>
                  <td className={`${td} whitespace-nowrap`} title={formatDateTime(s.last_seen)}>
                    {formatRelative(s.last_seen)}
                  </td>
                  <td className={`${td} whitespace-nowrap text-end`}>
                    {isAdmin && (
                      <>
                        <Button size="sm" onClick={() => setAssigning(s)}>
                          {t('unknownSources.assign')}
                        </Button>{' '}
                        {!s.ignored && (
                          <Button
                            size="sm"
                            variant="ghost"
                            busy={ignore.isPending && ignore.variables === s.source_ip}
                            onClick={() => ignore.mutate(s.source_ip)}
                          >
                            {t('unknownSources.ignore')}
                          </Button>
                        )}
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      {!isAdmin && <Notice>{t('unknownSources.viewerHint')}</Notice>}
      <AssignModal src={assigning} onClose={() => setAssigning(null)} />
    </div>
  );
}
