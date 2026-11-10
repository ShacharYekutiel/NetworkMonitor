import { useQuery } from '@tanstack/react-query';
import { endpoints, qk } from '../../api/endpoints';
import { useFeatures } from '../../hooks/features';
import { ingestOn } from '../../lib/features';

/** [2b-3] Event rules (viewer read) — for rule names of matched records and rule filters. */
export function useEventRules() {
  const { features } = useFeatures();
  return useQuery({
    queryKey: qk.eventRules(),
    queryFn: () => endpoints.eventRules(),
    enabled: ingestOn(features),
    staleTime: 60_000,
    select: (d) => d.items,
  });
}

export function useRuleName(): (id: number) => string {
  const { data } = useEventRules();
  return (id: number) => data?.find((r) => r.id === id)?.name ?? `#${id}`;
}
