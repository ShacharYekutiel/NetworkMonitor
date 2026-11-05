// [2b-1] Fingerprint / identity display (discovery results and the device page).
import { useTranslation } from 'react-i18next';
import type { ClassifiedBy, Confidence, DeviceIdentity, Fingerprint } from '../../api/types';
import { formatDateTime } from '../../lib/format';
import { GlobeIcon, ListIcon, QuestionIcon, RadarIcon } from '../icons';
import { cx, Ltr, TruncLtr } from '../ui';
import { DeviceClassBadge, VendorBadge } from './Badges';

const CONF_TONE: Record<Confidence, string> = {
  high: 'bg-green-50 text-green-800 ring-green-200 dark:bg-green-950 dark:text-green-300 dark:ring-green-900',
  medium:
    'bg-amber-50 text-amber-800 ring-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:ring-amber-900',
  low: 'bg-slate-50 text-slate-600 ring-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700',
};

function ClassifiedByIcon({ by }: { by: ClassifiedBy }) {
  switch (by) {
    case 'snmp':
      return <RadarIcon size={11} />;
    case 'fingerprint':
      return <GlobeIcon size={11} />;
    case 'ports':
      return <ListIcon size={11} />;
    default:
      return <QuestionIcon size={11} />;
  }
}

/** Confidence chip with the classification source icon (SNMP / web / ports). */
export function ConfidenceChip({ confidence, by }: { confidence?: Confidence; by?: ClassifiedBy }) {
  const { t } = useTranslation();
  if (!confidence) return null;
  const title = `${t('confidence.label')}: ${t(`confidence.${confidence}`)} · ${t('classifiedBy.label')}: ${t(`classifiedBy.${by ?? 'none'}`)}`;
  return (
    <span
      title={title}
      className={cx(
        'inline-flex items-center gap-1 whitespace-nowrap rounded px-1 py-px text-[11px] font-medium ring-1 ring-inset',
        CONF_TONE[confidence] ?? CONF_TONE.low,
      )}
    >
      <ClassifiedByIcon by={by ?? 'none'} />
      {t(`classifiedBy.${by ?? 'none'}`)} · {t(`confidence.${confidence}`)}
    </span>
  );
}

/** True when the fingerprint has anything worth showing. */
export function hasFingerprint(fp: Fingerprint | null | undefined): fp is Fingerprint {
  return !!fp && (fp.http.length > 0 || !!fp.ssh || !!fp.telnet);
}

/** First HTTP title/server of a fingerprint (one-line summary for tables). */
export function fingerprintSummary(fp: Fingerprint | null | undefined): string | null {
  if (!fp) return null;
  const h = fp.http.find((x) => x.title) ?? fp.http.find((x) => x.server);
  if (h) return h.title ?? h.server;
  return fp.ssh?.banner ?? fp.telnet?.banner ?? null;
}

/** Per-port HTTP title/server/realm/markers and the SSH/telnet banners. */
export function FingerprintView({ fp }: { fp: Fingerprint }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-2 text-xs">
      {fp.http.map((h) => (
        <div
          key={`${h.scheme}${h.port}`}
          className="min-w-0 rounded bg-slate-50 p-2 ring-1 ring-inset ring-slate-200 dark:bg-slate-800/60 dark:ring-slate-700"
        >
          <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 font-mono">
            <Ltr className="font-semibold">
              {h.scheme}:{h.port}
            </Ltr>
            {h.status !== null && <Ltr className="text-slate-500">HTTP {h.status}</Ltr>}
            {h.redirects > 0 && (
              <span className="text-slate-500">
                {t('fingerprint.redirects', { count: h.redirects })}
              </span>
            )}
            {h.error && <span className="text-red-600 dark:text-red-400">{h.error}</span>}
          </div>
          <dl className="mt-1 grid grid-cols-[auto_minmax(0,1fr)] gap-x-2 gap-y-0.5">
            {h.title && (
              <>
                <dt className="text-slate-500">{t('fingerprint.title')}</dt>
                <dd dir="auto" className="break-words">
                  {h.title}
                </dd>
              </>
            )}
            {h.server && (
              <>
                <dt className="text-slate-500">{t('fingerprint.server')}</dt>
                <dd>
                  <TruncLtr title={h.server}>{h.server}</TruncLtr>
                </dd>
              </>
            )}
            {h.realm && (
              <>
                <dt className="text-slate-500">{t('fingerprint.realm')}</dt>
                <dd>
                  <TruncLtr title={h.realm}>{h.realm}</TruncLtr>
                </dd>
              </>
            )}
            {h.markers.length > 0 && (
              <>
                <dt className="text-slate-500">{t('fingerprint.markers')}</dt>
                <dd className="flex flex-wrap gap-1">
                  {h.markers.map((m) => (
                    <span key={m} className="rounded bg-slate-200 px-1 font-mono dark:bg-slate-700">
                      <Ltr>{m}</Ltr>
                    </span>
                  ))}
                </dd>
              </>
            )}
          </dl>
        </div>
      ))}
      {fp.ssh && (
        <div className="flex min-w-0 gap-2">
          <span className="shrink-0 text-slate-500">
            {t('fingerprint.ssh')} <Ltr>:{fp.ssh.port}</Ltr>
          </span>
          <TruncLtr className="font-mono" title={fp.ssh.banner ?? ''}>
            {fp.ssh.banner ?? '—'}
          </TruncLtr>
        </div>
      )}
      {fp.telnet && (
        <div className="flex min-w-0 gap-2">
          <span className="shrink-0 text-slate-500">
            {t('fingerprint.telnet')} <Ltr>:{fp.telnet.port}</Ltr>
          </span>
          <TruncLtr className="font-mono" title={fp.telnet.banner ?? ''}>
            {fp.telnet.banner ?? '—'}
          </TruncLtr>
        </div>
      )}
      {!hasFingerprint(fp) && <p className="text-slate-500">{t('fingerprint.none')}</p>}
    </div>
  );
}

/** Stored or freshly identified device identity. */
export function IdentityView({
  identity,
  templateName,
}: {
  identity: DeviceIdentity;
  templateName?: string;
}) {
  const { t } = useTranslation();
  return (
    <div className="space-y-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <DeviceClassBadge cls={identity.device_class} />
        {identity.vendor && <VendorBadge vendor={identity.vendor} />}
        {identity.model && (
          <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs dark:bg-slate-800">
            <Ltr>{identity.model}</Ltr>
          </span>
        )}
        <ConfidenceChip confidence={identity.confidence} by={identity.classified_by} />
      </div>
      <dl className="grid grid-cols-1 gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
        <div className="flex min-w-0 gap-2">
          <dt className="shrink-0 text-slate-500">{t('identify.suggested')}:</dt>
          <dd className="min-w-0">{templateName ?? identity.suggested_template}</dd>
        </div>
        <div className="flex gap-2">
          <dt className="text-slate-500">{t('identify.identifiedAt')}:</dt>
          <dd>{formatDateTime(identity.identified_at)}</dd>
        </div>
        <div className="flex gap-2">
          <dt className="text-slate-500">SNMP:</dt>
          <dd>{identity.snmp_ok ? t('common.yes') : t('common.no')}</dd>
        </div>
        {identity.open_ports.length > 0 && (
          <div className="flex gap-2">
            <dt className="text-slate-500">{t('discovery.openPorts')}:</dt>
            <dd className="font-mono">
              <Ltr>{identity.open_ports.join(' ')}</Ltr>
            </dd>
          </div>
        )}
        {identity.sys_object_id && (
          <div className="flex min-w-0 gap-2 sm:col-span-2">
            <dt className="shrink-0 text-slate-500">sysObjectID:</dt>
            <dd className="min-w-0">
              <TruncLtr className="font-mono">{identity.sys_object_id}</TruncLtr>
            </dd>
          </div>
        )}
        {identity.sys_descr && (
          <div className="flex min-w-0 gap-2 sm:col-span-2">
            <dt className="shrink-0 text-slate-500">sysDescr:</dt>
            <dd className="min-w-0">
              <TruncLtr title={identity.sys_descr}>{identity.sys_descr}</TruncLtr>
            </dd>
          </div>
        )}
      </dl>
      {hasFingerprint(identity.fingerprint) && (
        <div>
          <div className="mb-1 text-xs font-semibold text-slate-600 dark:text-slate-300">
            {t('fingerprint.heading')}
          </div>
          <FingerprintView fp={identity.fingerprint} />
        </div>
      )}
    </div>
  );
}
