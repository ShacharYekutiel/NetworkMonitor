import { useEffect, useState, type FormEvent } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk, type MacSearchQuery } from '../api/endpoints';
import type { MacLocation } from '../api/types';
import { SearchIcon } from '../components/icons';
import { Faceplate } from '../components/ports/Faceplate';
import {
  Button,
  Card,
  cx,
  Empty,
  ErrorBox,
  GroupList,
  GroupTitle,
  KeyValue,
  ListRow,
  Loading,
  PageHeader,
  TAG,
  TAG_TONE,
  td as baseTd,
  th as baseTh,
} from '../components/ui';
import { formatDateTime, formatRelative } from '../lib/format';
import { colonMac, parseSearchInput, portLink } from '../lib/ports';

const th = baseTh;
const td = cx(baseTd, 'whitespace-nowrap');

function Row({ x }: { x: MacLocation }) {
  const { t } = useTranslation();
  return (
    <tr
      className={cx('hover:bg-surface-hover', !x.present && 'text-ink-muted')}
      data-best={x.best || undefined}
    >
      <td className={cx(td, 'num font-medium')}>
        <bdi dir="ltr">{x.mac}</bdi>
        {x.best && <span className={cx(TAG, TAG_TONE.up, 'ms-2 font-sans')}>{t('find.best')}</span>}
      </td>
      <td className={td}>
        {x.locally_administered ? (
          <span className="text-ink-muted">{t('ports.mac.random')}</span>
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
        <Link to={`/devices/${x.device_id}`} className="font-display text-ink hover:underline">
          <bdi dir="auto">{x.device_name}</bdi>
        </Link>
        <div className="num text-xs text-ink-muted">
          <bdi dir="ltr">{x.device_host}</bdi>
        </div>
      </td>
      <td className={td}>
        <Link
          to={portLink(x)}
          className="num font-medium text-ink underline decoration-line-strong underline-offset-[3px] hover:decoration-ink"
          data-testid="find-port-link"
        >
          <bdi dir="ltr">{x.port}</bdi>
        </Link>
        {!!x.lag_members?.length && (
          <div className="text-xs text-ink-muted" data-testid="find-lag-members">
            {t('ports.lag.members', { list: x.lag_members.join(', ') })}
          </div>
        )}
        {x.port_alias && (
          <div className="max-w-[12rem] truncate text-xs text-ink-muted">
            <bdi dir="auto">{x.port_alias}</bdi>
          </div>
        )}
      </td>
      <td className={cx(td, 'tabular-nums')}>{x.vlan ?? '—'}</td>
      <td className={td}>
        {x.is_uplink ? (
          <span className={cx(TAG, TAG_TONE.neutral)}>{t('find.uplink')}</span>
        ) : (
          <span className="text-xs text-ink-muted">{t('find.access')}</span>
        )}
      </td>
      <td className={td} title={formatDateTime(x.last_seen)}>
        {formatRelative(x.last_seen)}
      </td>
      <td className={td}>{x.present ? t('find.present') : t('find.gone')}</td>
    </tr>
  );
}

/**
 * The most likely place (prototype "איתור": the switch front panel with the port selected, the
 * facts under it) and every location as a row next to it.
 */
function BestCard({ best, others }: { best: MacLocation; others: MacLocation[] }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const ports = useQuery({
    queryKey: qk.devicePorts(best.device_id),
    queryFn: () => endpoints.devicePorts(best.device_id),
  });
  const device = useQuery({
    queryKey: qk.device(best.device_id),
    queryFn: () => endpoints.device(best.device_id),
  });
  const d = device.data;
  return (
    <div className="grid grid-cols-1 items-start gap-6 xl:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
      <Card
        title={
          <span className="inline-flex flex-wrap items-center gap-2.5">
            {best.is_uplink ? t('find.bestUplink') : t('find.bestAccess')}
            <span className={cx(TAG, best.present ? TAG_TONE.up : TAG_TONE.neutral)}>
              {best.present ? t('find.now') : t('find.gone')}
            </span>
          </span>
        }
      >
        <div className="grid gap-4" data-testid="find-best">
          {ports.data && ports.data.ports.length > 0 && (
            <Faceplate
              ports={ports.data.ports}
              selected={best.if_index}
              onSelect={(p) =>
                navigate(portLink({ device_id: best.device_id, if_index: p.if_index }))
              }
              name={best.device_name}
              model={d?.identity?.model ?? d?.identity?.vendor ?? d?.description ?? undefined}
              status={d?.status}
            />
          )}
          <KeyValue
            items={[
              {
                k: t('find.switch'),
                v: (
                  <Link
                    to={`/devices/${best.device_id}`}
                    className="font-display text-ink hover:underline"
                  >
                    <bdi dir="auto">{best.device_name}</bdi>
                  </Link>
                ),
              },
              {
                k: t('find.port'),
                v: (
                  <Link to={portLink(best)} className="num font-medium text-ink hover:underline">
                    <bdi dir="ltr">{best.port}</bdi>
                    {best.vlan !== null && <span> · {t('ports.vlanN', { vlan: best.vlan })}</span>}
                  </Link>
                ),
              },
              ...(best.ip
                ? [
                    {
                      k: t('ports.mac.ip'),
                      v: (
                        <bdi dir="ltr" className="num">
                          {best.ip}
                        </bdi>
                      ),
                    },
                  ]
                : []),
              {
                k: t('find.name'),
                v: (
                  <span>
                    {best.hostname && <bdi dir="ltr">{best.hostname}</bdi>}
                    {best.hostname && (best.vendor || best.locally_administered) && ' · '}
                    {best.locally_administered ? (
                      t('ports.mac.random')
                    ) : (
                      <bdi dir="auto">{best.vendor ?? (best.hostname ? '' : '—')}</bdi>
                    )}
                  </span>
                ),
              },
              {
                k: t('ports.mac.mac'),
                v: (
                  <bdi dir="ltr" className="num">
                    {best.mac}
                  </bdi>
                ),
              },
            ]}
          />
        </div>
      </Card>
      <GroupList label={t('find.locations')}>
        {others.map((x) => (
          <ListRow
            key={`${x.mac}-${x.device_id}-${x.if_index}`}
            dot={x.present ? 'bg-status-up' : 'bg-status-paused'}
            to={portLink(x)}
            title={
              <bdi dir="ltr" className="font-display">
                {x.device_name} · {x.port}
              </bdi>
            }
            subtitle={`${x.is_uplink ? t('find.uplink') : t('find.access')} · ${formatRelative(x.last_seen)}`}
          />
        ))}
      </GroupList>
    </div>
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
      {/* the spotlight field (prototype .spot): one big search line */}
      <form
        onSubmit={submit}
        role="search"
        className="flex items-center gap-3 rounded-xl bg-surface py-2.5 pe-2.5 ps-5 shadow-card"
      >
        <SearchIcon size={20} className="shrink-0 text-ink-subtle" />
        <input
          value={input}
          onChange={(e) => {
            setInput(e.target.value);
            setInvalid(false);
          }}
          placeholder={t('find.placeholder')}
          aria-label={t('find.label')}
          aria-invalid={invalid}
          dir={input ? 'ltr' : undefined}
          className="num h-11 min-w-0 flex-1 bg-transparent text-[20px] text-ink outline-none placeholder:text-ink-subtle rtl:text-right"
          autoFocus
        />
        <Button type="submit" variant="primary">
          {t('find.search')}
        </Button>
      </form>
      {!urlQuery && <p className="m-0 mx-1 text-[13px] text-ink-subtle">{t('find.help')}</p>}
      {invalid && (
        <ErrorBox error={new Error(t('errors.validation_error') + ' ' + t('find.invalidHint'))} />
      )}
      {q.isFetching && !q.data && <Loading />}
      <ErrorBox error={q.error} />
      {q.data && query && (
        <>
          <p className="m-0 mx-1 text-sm text-ink-muted" data-testid="find-summary">
            {query.ip
              ? t('find.forIp', { ip: query.ip })
              : query.mac
                ? t('find.forMac', { mac: query.mac })
                : t('find.forPrefix', { prefix: colonMac(query.mac_prefix ?? '') })}{' '}
            · {t('find.count', { count: q.data.items.length })}
          </p>
          {best && <BestCard best={best} others={q.data.items} />}
          {best && <GroupTitle>{t('find.allResults')}</GroupTitle>}
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
                        className={a.present ? undefined : 'text-ink-muted'}
                      >
                        <td className={td}>
                          <bdi dir="ltr">{a.ip}</bdi>
                        </td>
                        <td className={cx(td, 'font-mono text-[13px]')}>
                          <bdi dir="ltr">{a.mac}</bdi>
                        </td>
                        <td className={td}>
                          <bdi dir="ltr">{a.hostname ?? '—'}</bdi>
                        </td>
                        <td className={td}>
                          <Link
                            to={`/devices/${a.source_device_id}`}
                            className="text-accent-ink hover:underline"
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
    </div>
  );
}
