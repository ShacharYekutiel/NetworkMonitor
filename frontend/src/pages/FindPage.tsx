import { useEffect, useState, type FormEvent } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk, type MacSearchQuery } from '../api/endpoints';
import type { MacLocation } from '../api/types';
import { SearchIcon } from '../components/icons';
import { Button, Card, cx, Empty, ErrorBox, Input, Loading, PageHeader } from '../components/ui';
import { formatDateTime, formatRelative } from '../lib/format';
import { colonMac, parseSearchInput, portLink } from '../lib/ports';

const th =
  'whitespace-nowrap border-b border-slate-200 px-3 py-2 text-start text-xs font-medium uppercase tracking-wide text-slate-500 dark:border-slate-800';
const td =
  'whitespace-nowrap border-b border-slate-100 px-3 py-2 align-middle dark:border-slate-800/70';
const chip =
  'inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium ring-1 ring-inset';

function Row({ x }: { x: MacLocation }) {
  const { t } = useTranslation();
  return (
    <tr
      className={cx(x.best && 'bg-green-50 dark:bg-green-950/30', !x.present && 'text-slate-500')}
      data-best={x.best || undefined}
    >
      <td className={cx(td, 'font-mono text-xs')}>
        <bdi dir="ltr">{x.mac}</bdi>
        {x.best && (
          <span className={cx(chip, 'ms-2 bg-green-600 font-sans text-white ring-green-600')}>
            {t('find.best')}
          </span>
        )}
      </td>
      <td className={td}>
        {x.locally_administered ? (
          <span className="text-slate-500">{t('ports.mac.random')}</span>
        ) : (
          <bdi dir="auto">{x.vendor ?? '—'}</bdi>
        )}
      </td>
      <td className={td}>
        <bdi dir="ltr">{x.ip ?? '—'}</bdi>
      </td>
      <td className={td}>
        <bdi dir="ltr">{x.hostname ?? '—'}</bdi>
      </td>
      <td className={td}>
        <Link
          to={`/devices/${x.device_id}`}
          className="text-sky-700 hover:underline dark:text-sky-400"
        >
          <bdi dir="auto">{x.device_name}</bdi>
        </Link>
        <div className="text-xs text-slate-500">
          <bdi dir="ltr">{x.device_host}</bdi>
        </div>
      </td>
      <td className={td}>
        <Link
          to={portLink(x)}
          className="font-medium text-sky-700 hover:underline dark:text-sky-400"
          data-testid="find-port-link"
        >
          <bdi dir="ltr">{x.port}</bdi>
        </Link>
        {!!x.lag_members?.length && (
          <div className="text-xs text-slate-500" data-testid="find-lag-members">
            {t('ports.lag.members', { list: x.lag_members.join(', ') })}
          </div>
        )}
        {x.port_alias && (
          <div className="max-w-[12rem] truncate text-xs text-slate-500">
            <bdi dir="auto">{x.port_alias}</bdi>
          </div>
        )}
      </td>
      <td className={cx(td, 'tabular-nums')}>{x.vlan ?? '—'}</td>
      <td className={td}>
        {x.is_uplink ? (
          <span
            className={cx(
              chip,
              'bg-sky-50 text-sky-800 ring-sky-200 dark:bg-sky-950 dark:text-sky-300 dark:ring-sky-900',
            )}
          >
            {t('find.uplink')}
          </span>
        ) : (
          <span className="text-xs text-slate-500">{t('find.access')}</span>
        )}
      </td>
      <td className={td} title={formatDateTime(x.last_seen)}>
        {formatRelative(x.last_seen)}
      </td>
      <td className={td}>{x.present ? t('find.present') : t('find.gone')}</td>
    </tr>
  );
}

export function FindPage() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const urlQuery: MacSearchQuery | null = params.get('ip')
    ? { ip: params.get('ip') as string }
    : params.get('q')
      ? { q: params.get('q') as string }
      : null;
  const [input, setInput] = useState(params.get('ip') ?? params.get('q') ?? '');
  const [invalid, setInvalid] = useState(false);
  useEffect(() => {
    setInput(params.get('ip') ?? params.get('q') ?? '');
  }, [params]);

  const q = useQuery({
    queryKey: qk.macSearch(urlQuery ?? { q: '' }),
    queryFn: () => endpoints.searchMac(urlQuery as MacSearchQuery),
    enabled: urlQuery !== null,
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const parsed = parseSearchInput(input);
    setInvalid(!parsed);
    if (!parsed) return;
    setParams('ip' in parsed ? { ip: parsed.ip } : { q: parsed.q });
  };

  // The highlighted answer is for an exact MAC or an IP (a prefix may match many MACs).
  const best = q.data?.query.mac || q.data?.query.ip ? q.data.items.find((x) => x.best) : undefined;
  const query = q.data?.query;

  return (
    <div className="space-y-4">
      <PageHeader title={t('find.title')} subtitle={t('find.subtitle')} />
      <form onSubmit={submit} className="flex flex-wrap gap-2" role="search">
        <div className="relative min-w-0 flex-1 basis-64">
          <SearchIcon
            size={15}
            className="pointer-events-none absolute start-2.5 top-1/2 -translate-y-1/2 text-slate-400"
          />
          <Input
            value={input}
            onChange={(e) => {
              setInput(e.target.value);
              setInvalid(false);
            }}
            placeholder={t('find.placeholder')}
            aria-label={t('find.label')}
            aria-invalid={invalid}
            dir={input ? 'ltr' : undefined}
            className="ps-8 font-mono"
            autoFocus
          />
        </div>
        <Button type="submit" variant="primary">
          {t('find.search')}
        </Button>
      </form>
      {invalid && (
        <ErrorBox error={new Error(t('errors.validation_error') + ' ' + t('find.invalidHint'))} />
      )}
      {q.isFetching && !q.data && <Loading />}
      <ErrorBox error={q.error} />
      {q.data && query && (
        <>
          <p className="text-sm text-slate-600 dark:text-slate-300" data-testid="find-summary">
            {query.ip
              ? t('find.forIp', { ip: query.ip })
              : query.mac
                ? t('find.forMac', { mac: query.mac })
                : t('find.forPrefix', { prefix: colonMac(query.mac_prefix ?? '') })}{' '}
            · {t('find.count', { count: q.data.items.length })}
          </p>
          {best && (
            <Card>
              <div
                className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm"
                data-testid="find-best"
              >
                <span className="font-medium text-green-700 dark:text-green-400">
                  {t('find.bestTitle')}
                </span>
                <bdi dir="ltr" className="font-mono">
                  {best.mac}
                </bdi>
                <span className="text-slate-500">→</span>
                <Link
                  to={`/devices/${best.device_id}`}
                  className="text-sky-700 hover:underline dark:text-sky-400"
                >
                  <bdi dir="auto">{best.device_name}</bdi>
                </Link>
                <Link
                  to={portLink(best)}
                  className="font-semibold text-sky-700 hover:underline dark:text-sky-400"
                >
                  {t('ports.portN', { name: best.port })}
                </Link>
                {best.vlan !== null && (
                  <span className="text-slate-500">{t('ports.vlanN', { vlan: best.vlan })}</span>
                )}
                {best.ip && (
                  <bdi dir="ltr" className="text-slate-600 dark:text-slate-300">
                    {best.ip}
                  </bdi>
                )}
                {best.hostname && (
                  <bdi dir="ltr" className="text-slate-500">
                    {best.hostname}
                  </bdi>
                )}
              </div>
            </Card>
          )}
          <Card bodyClassName="p-0">
            {q.data.items.length === 0 ? (
              <Empty>{t('find.none')}</Empty>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm" data-testid="find-results">
                  <thead>
                    <tr>
                      <th className={th}>{t('ports.mac.mac')}</th>
                      <th className={th}>{t('ports.mac.vendor')}</th>
                      <th className={th}>{t('ports.mac.ip')}</th>
                      <th className={th}>{t('ports.mac.hostname')}</th>
                      <th className={th}>{t('find.switch')}</th>
                      <th className={th}>{t('find.port')}</th>
                      <th className={th}>{t('ports.mac.vlan')}</th>
                      <th className={th}>{t('find.portType')}</th>
                      <th className={th}>{t('ports.mac.lastSeen')}</th>
                      <th className={th}>{t('find.presence')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {q.data.items.map((x) => (
                      <Row key={`${x.mac}-${x.device_id}-${x.if_index}`} x={x} />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
          {q.data.arp.length > 0 && (
            <Card title={t('find.arpTitle')} bodyClassName="p-0">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr>
                      <th className={th}>{t('ports.mac.ip')}</th>
                      <th className={th}>{t('ports.mac.mac')}</th>
                      <th className={th}>{t('ports.mac.hostname')}</th>
                      <th className={th}>{t('find.arpSource')}</th>
                      <th className={th}>{t('ports.mac.lastSeen')}</th>
                      <th className={th}>{t('find.presence')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {q.data.arp.map((a) => (
                      <tr
                        key={`${a.ip}-${a.mac}-${a.source_device_id}`}
                        className={a.present ? undefined : 'text-slate-500'}
                      >
                        <td className={td}>
                          <bdi dir="ltr">{a.ip}</bdi>
                        </td>
                        <td className={cx(td, 'font-mono text-xs')}>
                          <bdi dir="ltr">{a.mac}</bdi>
                        </td>
                        <td className={td}>
                          <bdi dir="ltr">{a.hostname ?? '—'}</bdi>
                        </td>
                        <td className={td}>
                          <Link
                            to={`/devices/${a.source_device_id}`}
                            className="text-sky-700 hover:underline dark:text-sky-400"
                          >
                            <bdi dir="auto">{a.source_device_name}</bdi>
                          </Link>
                        </td>
                        <td className={td} title={formatDateTime(a.last_seen)}>
                          {formatRelative(a.last_seen)}
                        </td>
                        <td className={td}>{a.present ? t('find.present') : t('find.gone')}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )}
        </>
      )}
      {!urlQuery && <p className="text-sm text-slate-500">{t('find.help')}</p>}
    </div>
  );
}
