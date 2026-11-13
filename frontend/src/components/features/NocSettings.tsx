import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints3, qk3 } from '../../api/endpoints3';
import type { Language, Settings } from '../../api/types';
import type { KioskScreen, KioskTokenResponse, KioskWidget } from '../../api/types3';
import { KIOSK_WIDGETS } from '../../api/types3';
import { useFeatures } from '../../hooks/features';
import { formatDateTime } from '../../lib/format';
import { kioskUrl, parseAllowedSources, refreshError } from '../../lib/kiosk';
import { qrMatrix, qrPath } from '../../lib/qr';
import { moveItem } from '../../lib/reports';
import { GroupSelect } from '../EntityForms';
import { ChevronIcon, EditIcon, PlusIcon, RefreshIcon, TrashIcon } from '../icons';
import { useToast } from '../toast';
import {
  Button,
  Card,
  Checkbox,
  ConfirmDialog,
  Empty,
  ErrorBox,
  Field,
  FieldGroup,
  Input,
  Loading,
  Modal,
  Select,
  Table,
  Textarea,
  td,
  th,
} from '../ui';
import { FeatureTab } from './FeatureToggle';
import { cardAnchor } from '../../lib/settingsIndex';

export function QrCode({ text, size = 192 }: { text: string; size?: number }) {
  const m = useMemo(() => qrMatrix(text), [text]);
  if (!m) return null;
  const { d, size: n } = qrPath(m);
  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${n} ${n}`}
      shapeRendering="crispEdges"
      role="img"
      aria-label="QR"
      data-testid="qr"
      className="rounded bg-surface"
    >
      <rect width={n} height={n} fill="#fff" />
      <path d={d} fill="#000" />
    </svg>
  );
}

/** The token is shown only here (after create / regenerate). */
export function ShowUrlDialog({
  resp,
  onClose,
}: {
  resp: KioskTokenResponse | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  useEffect(() => setCopied(false), [resp]);
  if (!resp) return null;
  const url = kioskUrl(resp, window.location.origin);
  return (
    <Modal
      open
      onClose={onClose}
      title={t('kiosk.urlTitle', { name: resp.item.name })}
      footer={
        <Button variant="primary" onClick={onClose}>
          {t('kiosk.urlDone')}
        </Button>
      }
    >
      <div className="space-y-3" data-testid="kiosk-url-dialog">
        <p className="text-sm text-status-warning">{t('kiosk.urlOnce')}</p>
        <div className="flex gap-2">
          <Input
            readOnly
            value={url}
            dir="ltr"
            className="font-mono text-xs"
            onFocus={(e) => e.target.select()}
          />
          <Button
            onClick={() => {
              void navigator.clipboard?.writeText(url).then(() => setCopied(true));
            }}
          >
            {copied ? t('kiosk.copied') : t('kiosk.copy')}
          </Button>
        </div>
        <div className="flex justify-center">
          <QrCode text={url} />
        </div>
        <p className="text-xs text-ink-muted">{t('kiosk.urlHint')}</p>
      </div>
    </Modal>
  );
}

function ScreenModal({
  open,
  screen,
  onClose,
  onToken,
}: {
  open: boolean;
  screen: KioskScreen | null;
  onClose: () => void;
  onToken: (r: KioskTokenResponse) => void;
}) {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const [name, setName] = useState('');
  const [enabled, setEnabled] = useState(true);
  const [sources, setSources] = useState('');
  const [scope, setScope] = useState<number | null>(null);
  const [widgets, setWidgets] = useState<KioskWidget[]>([]);
  const [language, setLanguage] = useState<Language>('en');
  const [addresses, setAddresses] = useState(false);
  const [refresh, setRefresh] = useState('30');
  const [touched, setTouched] = useState(false);
  useEffect(() => {
    if (!open) return;
    setName(screen?.name ?? '');
    setEnabled(screen?.enabled ?? true);
    setSources((screen?.allowed_sources ?? []).join('\n'));
    setScope(screen?.scope_group_id ?? null);
    setWidgets(screen?.widgets ?? ['summary', 'down_devices', 'problems', 'recent_alerts']);
    setLanguage(screen?.language ?? (i18n.language === 'he' ? 'he' : 'en'));
    setAddresses(screen?.show_addresses ?? false);
    setRefresh(String(screen?.refresh_s ?? 30));
    setTouched(false);
  }, [open, screen, i18n.language]);
  const src = parseAllowedSources(sources);
  const refErr = refreshError(refresh);
  const nameErr = !name.trim() || name.length > 128;
  const invalid = !!src.error || !!refErr || nameErr || !widgets.length;
  const m = useMutation({
    mutationFn: async () => {
      const body = {
        name: name.trim(),
        enabled,
        allowed_sources: src.list,
        scope_group_id: scope,
        widgets,
        language,
        show_addresses: addresses,
        refresh_s: Number(refresh),
      };
      if (screen) {
        await endpoints3.updateKioskScreen(screen.id, body);
        return null;
      }
      return endpoints3.createKioskScreen(body);
    },
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: qk3.kioskScreens });
      toast.push('success', t('common.saved'));
      onClose();
      if (r) onToken(r);
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (!invalid) m.mutate();
  };
  const ordered = [...widgets, ...KIOSK_WIDGETS.filter((w) => !widgets.includes(w))];
  return (
    <Modal open={open} onClose={onClose} title={screen ? t('kiosk.edit') : t('kiosk.add')} wide>
      <form onSubmit={submit} className="space-y-3">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label={t('common.name')} required>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={128}
              autoFocus
            />
          </Field>
          <Field label={t('kiosk.scope')}>
            <GroupSelect
              value={scope}
              onChange={setScope}
              allowRoot
              rootLabel={t('kiosk.everything')}
            />
          </Field>
          <Field
            label={t('kiosk.sources')}
            required
            className="sm:col-span-2"
            hint={
              touched && src.error ? (
                <span className="text-status-down">{t(src.error)}</span>
              ) : (
                t('kiosk.sourcesHint')
              )
            }
          >
            <Textarea
              rows={2}
              value={sources}
              onChange={(e) => setSources(e.target.value)}
              dir="ltr"
              className="font-mono"
              placeholder="10.0.20.15"
            />
          </Field>
          <Field label={t('common.language')}>
            <Select value={language} onChange={(e) => setLanguage(e.target.value as Language)}>
              <option value="he">עברית</option>
              <option value="en">English</option>
            </Select>
          </Field>
          <Field
            label={t('kiosk.refresh')}
            hint={
              touched && refErr ? (
                <span className="text-status-down">{t(refErr)}</span>
              ) : (
                t('kiosk.refreshHint')
              )
            }
          >
            <Input
              type="number"
              min={10}
              max={300}
              value={refresh}
              onChange={(e) => setRefresh(e.target.value)}
              dir="ltr"
            />
          </Field>
        </div>
        <FieldGroup label={t('kiosk.widgets')} hint={t('kiosk.widgetsHint')}>
          <ul className="space-y-1">
            {ordered.map((w) => {
              const i = widgets.indexOf(w);
              return (
                <li key={w} className="flex items-center gap-2">
                  <Checkbox
                    label={t(`kiosk.widget.${w}`)}
                    checked={i >= 0}
                    onChange={(v) => setWidgets((x) => (v ? [...x, w] : x.filter((y) => y !== w)))}
                  />
                  {i >= 0 && (
                    <span className="ms-auto inline-flex gap-1">
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={i === 0}
                        aria-label={t('reports.moveUp')}
                        onClick={() => setWidgets((x) => moveItem(x, i, -1))}
                      >
                        <ChevronIcon size={12} className="-rotate-90" />
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={i === widgets.length - 1}
                        aria-label={t('reports.moveDown')}
                        onClick={() => setWidgets((x) => moveItem(x, i, 1))}
                      >
                        <ChevronIcon size={12} className="rotate-90" />
                      </Button>
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
          {touched && !widgets.length && (
            <p className="text-xs text-status-down">{t('kiosk.err.widgets')}</p>
          )}
        </FieldGroup>
        <div className="flex flex-col gap-1.5">
          <Checkbox label={t('kiosk.showAddresses')} checked={addresses} onChange={setAddresses} />
          <Checkbox label={t('common.enabled')} checked={enabled} onChange={setEnabled} />
        </div>
        <ErrorBox error={m.error} />
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="submit" variant="primary" busy={m.isPending}>
            {t('common.save')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

/** [3] Settings → NOC screens */
export function NocTab({ settings }: { settings: Settings }) {
  const { t } = useTranslation();
  const { features } = useFeatures();
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({
    queryKey: qk3.kioskScreens,
    queryFn: endpoints3.kioskScreens,
    enabled: features.noc,
  });
  const [editing, setEditing] = useState<KioskScreen | null | undefined>(undefined);
  const [token, setToken] = useState<KioskTokenResponse | null>(null);
  const [regen, setRegen] = useState<KioskScreen | null>(null);
  const [del, setDel] = useState<KioskScreen | null>(null);
  const regenerate = useMutation({
    mutationFn: (id: number) => endpoints3.regenerateKiosk(id),
    onSuccess: (r) => {
      setRegen(null);
      setToken(r);
      void qc.invalidateQueries({ queryKey: qk3.kioskScreens });
    },
  });
  const remove = useMutation({
    mutationFn: (id: number) => endpoints3.deleteKioskScreen(id),
    onSuccess: () => {
      setDel(null);
      toast.push('success', t('common.deleted'));
      void qc.invalidateQueries({ queryKey: qk3.kioskScreens });
    },
  });
  return (
    <FeatureTab feature="noc" settings={settings}>
      <Card
        anchor={cardAnchor('noc')}
        title={t('kiosk.title')}
        bodyClassName="p-0"
        actions={
          features.noc && (
            <Button size="sm" variant="primary" onClick={() => setEditing(null)}>
              <PlusIcon size={12} /> {t('kiosk.add')}
            </Button>
          )
        }
      >
        <p className="border-b border-line px-4 py-2 text-xs text-ink-muted">{t('kiosk.intro')}</p>
        {q.isLoading && <Loading />}
        <ErrorBox error={q.error} className="m-3" />
        {q.data && !q.data.items.length && <Empty>{t('kiosk.empty')}</Empty>}
        {q.data && q.data.items.length > 0 && (
          <Table>
            <thead>
              <tr>
                <th className={th}>{t('common.name')}</th>
                <th className={th}>{t('kiosk.token')}</th>
                <th className={th}>{t('kiosk.sources')}</th>
                <th className={th}>{t('kiosk.scope')}</th>
                <th className={th}>{t('kiosk.lastSeen')}</th>
                <th className={th} />
              </tr>
            </thead>
            <tbody>
              {q.data.items.map((s) => (
                <tr key={s.id} className={s.enabled ? undefined : 'opacity-60'}>
                  <td className={td}>
                    <span className="font-medium">{s.name}</span>
                    {!s.enabled && (
                      <span className="ms-1 text-xs text-ink-muted">({t('kiosk.disabled')})</span>
                    )}
                    <div className="text-xs text-ink-muted">
                      {s.widgets.map((w) => t(`kiosk.widget.${w}`)).join(' · ')}
                    </div>
                  </td>
                  <td className={td}>
                    <bdi dir="ltr" className="font-mono text-xs">
                      {s.token_prefix}…
                    </bdi>
                  </td>
                  <td className={td}>
                    <bdi dir="ltr" className="font-mono text-xs">
                      {s.allowed_sources.join(', ')}
                    </bdi>
                  </td>
                  <td className={td}>{s.scope_group_name ?? t('kiosk.everything')}</td>
                  <td className={td}>
                    {s.last_seen_at ? (
                      <>
                        {formatDateTime(s.last_seen_at)}
                        {s.last_seen_ip && (
                          <bdi dir="ltr" className="ms-1 text-xs text-ink-muted">
                            {s.last_seen_ip}
                          </bdi>
                        )}
                      </>
                    ) : (
                      '—'
                    )}
                  </td>
                  <td className={`${td} text-end`}>
                    <span className="inline-flex gap-1">
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={t('common.edit')}
                        onClick={() => setEditing(s)}
                      >
                        <EditIcon size={14} />
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={t('kiosk.regenerate')}
                        title={t('kiosk.regenerate')}
                        onClick={() => setRegen(s)}
                      >
                        <RefreshIcon size={14} />
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={t('common.delete')}
                        onClick={() => setDel(s)}
                      >
                        <TrashIcon size={14} />
                      </Button>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <ScreenModal
        open={editing !== undefined}
        screen={editing ?? null}
        onClose={() => setEditing(undefined)}
        onToken={setToken}
      />
      <ShowUrlDialog resp={token} onClose={() => setToken(null)} />
      <ConfirmDialog
        open={!!regen}
        title={t('kiosk.regenerate')}
        message={t('kiosk.regenerateConfirm', { name: regen?.name ?? '' })}
        confirmLabel={t('kiosk.regenerate')}
        onConfirm={() => regen && regenerate.mutate(regen.id)}
        onClose={() => setRegen(null)}
        busy={regenerate.isPending}
        error={regenerate.error}
      />
      <ConfirmDialog
        open={!!del}
        title={t('kiosk.deleteTitle')}
        message={t('kiosk.deleteConfirm', { name: del?.name ?? '' })}
        onConfirm={() => del && remove.mutate(del.id)}
        onClose={() => setDel(null)}
        busy={remove.isPending}
        error={remove.error}
      />
    </FeatureTab>
  );
}
