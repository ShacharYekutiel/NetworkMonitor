import { useQuery } from '@tanstack/react-query';
import { endpoints, qk } from '../api/endpoints';
import type { SensorType, SensorTypeDef } from '../api/types';

export function useTree() {
  return useQuery({ queryKey: qk.tree, queryFn: endpoints.tree, staleTime: 60_000 });
}

export function useSensorTypes() {
  return useQuery({
    queryKey: qk.sensorTypes,
    queryFn: endpoints.sensorTypes,
    staleTime: Infinity,
    select: (d) => d.items,
  });
}

export function useSensorTypeDef(type: SensorType | undefined): SensorTypeDef | undefined {
  const { data } = useSensorTypes();
  return data?.find((x) => x.type === type);
}
