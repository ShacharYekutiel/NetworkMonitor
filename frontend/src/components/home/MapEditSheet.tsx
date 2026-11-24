import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { premium, qkPremium, type HomeMapMode, type HomeMapSettings } from '../../api/premium';
import { useTree } from '../../hooks/queries';
import { errorMessage } from '../../lib/errors';
import { allDevices } from '../../lib/tree';
import { ChevronIcon, XIcon } from '../icons';
import { useToast } from '../toast';
import {
  Button,
  ErrorBox,
  Field,
  Input,
  Loading,
  Modal,
  SegmentedControl,
  Select,
  Switch,
} from '../ui';

const DEFAULTS: HomeMapSettings = {
  mode: 'auto',
  show_internet: true,
  backbone_device_ids: [],
  max_groups: 12,
};

/** Admin sheet of the home map (GET/PUT /api/settings/home-map). */
export function MapEditSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({ queryKey: qkPremium.homeMap, queryFn: premium.homeMap, enabled: open });
  const tree = useTree();
  const [v, setV] = useState<HomeMapSettings>(DEFAULTS);
  useEffect(() => {
    if (q.data) setV(q.data);
  }, [q.data]);
  const save = useMutation({
    mutationFn: () => premium.setHomeMap(v),
    onSuccess: (r) => {
      qc.setQueryData(qkPremium.homeMap, r);
      void qc.invalidateQueries({ queryKey: ['overview-map'] });
      toast.push('success', t('home.map.saved'));
      onClose();
    },
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  const devices = tree.data ? allDevices(tree.data.groups).map((x) => x.device) : [];
  const name = (id: number) => devices.find((d) => d.id === id)?.name ?? `#${id}`;
  const ids = v.backbone_device_ids;
  const move = (i: number, d: -1 | 1) => {
    const next = [...ids];
    [next[i], next[i + d]] = [next[i + d], next[i]];
    setV({ ...v, backbone_device_ids: next });
  };
  return (
    <Modal
      open={open}
      title={
        <>
          {t('home.map.edit')}
          <span className="mt-1 block text-sm font-normal tracking-normal text-ink-muted">
            {t('home.map.editSub')}
          </span>
        </>
      }
      onClose={onClose}
      testId="home-map-edit"
      footer={
        <>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button variant="primary" busy={save.isPending} onClick={() => save.mutate()}>
            {t('common.save')}
          </Button>
        </>
      }
    >
      {q.isLoading ? (
        <Loading />
      ) : q.data === null ? (
        <ErrorBox error={new Error(t('errors.not_found'))} />
      ) : (
        <div className="grid gap-5">
          <div className="grid gap-1.5">
            <span className="text-[13.5px] text-ink-muted">{t('home.map.mode')}</span>
            <SegmentedControl<HomeMapMode>
              label={t('home.map.mode')}
              value={v.mode}
              onChange={(mode) => setV({ ...v, mode })}
              options={[
                { value: 'auto', label: t('home.map.mode_auto') },
                { value: 'backbone', label: t('home.map.mode_backbone') },
              ]}
            />
            <span className="text-[12.5px] text-ink-subtle">
              {t(`home.map.modeHint_${v.mode}`)}
            </span>
          </div>
          <div className="flex items-start gap-3.5">
            <span className="grid flex-1 gap-0.5">
              <b className="font-semibold">{t('home.map.showInternet')}</b>
              <span className="text-[13.5px] text-ink-muted">{t('home.map.showInternetHint')}</span>
            </span>
            <Switch
              label={t('home.map.showInternet')}
              checked={v.show_internet}
              onChange={(show_internet) => setV({ ...v, show_internet })}
            />
          </div>
          {v.mode === 'backbone' && (
            <div className="grid gap-1.5">
              <span className="text-[13.5px] text-ink-muted">{t('home.map.backbone')}</span>
              <ol className="m-0 grid list-none gap-1.5 p-0" data-testid="backbone-list">
                {ids.length === 0 && (
                  <li className="text-[13.5px] text-ink-muted">{t('home.map.noBackbone')}</li>
                )}
                {ids.map((id, i) => (
                  <li
                    key={id}
                    className="flex items-center gap-3 rounded-[14px] bg-chip px-3 py-2.5"
                  >
                    <span className="num grid h-6 w-6 place-items-center rounded-full bg-accent text-xs font-semibold text-accent-on">
                      {i + 1}
                    </span>
                    <bdi
                      dir="ltr"
                      className="num min-w-0 flex-1 truncate text-[14.5px] font-medium"
                    >
                      {name(id)}
                    </bdi>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="!h-7 !w-7 !px-0"
                      disabled={i === 0}
                      aria-label={t('home.map.up')}
                      onClick={() => move(i, -1)}
                    >
                      <ChevronIcon size={14} className="-rotate-90" />
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="!h-7 !w-7 !px-0"
                      disabled={i === ids.length - 1}
                      aria-label={t('home.map.down')}
                      onClick={() => move(i, 1)}
                    >
                      <ChevronIcon size={14} className="rotate-90" />
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="!h-7 !w-7 !px-0"
                      aria-label={t('home.map.remove')}
                      onClick={() =>
                        setV({ ...v, backbone_device_ids: ids.filter((x) => x !== id) })
                      }
                    >
                      <XIcon size={14} />
                    </Button>
                  </li>
                ))}
              </ol>
              {ids.length < 8 && (
                <Select
                  aria-label={t('home.map.addDevice')}
                  value=""
                  onChange={(e) => {
                    const id = Number(e.target.value);
                    if (id) setV({ ...v, backbone_device_ids: [...ids, id] });
                  }}
                >
                  <option value="">{t('home.map.addDevice')}</option>
                  {devices
                    .filter((d) => !ids.includes(d.id))
                    .map((d) => (
                      <option key={d.id} value={d.id}>
                        {d.name}
                      </option>
                    ))}
                </Select>
              )}
            </div>
          )}
          <Field label={t('home.map.maxGroups')}>
            <Input
              type="number"
              min={1}
              max={50}
              value={v.max_groups}
              onChange={(e) => setV({ ...v, max_groups: Number(e.target.value) || 1 })}
              className="w-28"
            />
          </Field>
          {save.error && <ErrorBox error={save.error} />}
        </div>
      )}
    </Modal>
  );
}
