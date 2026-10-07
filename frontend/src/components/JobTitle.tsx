import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import type { Job } from '../api/types';
import { shortId } from '../lib/format';

/** A job's title (shared cache with the job page); its short ID while loading or if unavailable. */
export default function JobTitle({ id }: { id: string }) {
  const job = useQuery({
    queryKey: ['job', id],
    queryFn: ({ signal }) => api.request<Job>(`/jobs/${id}`, { signal }),
    staleTime: 60_000,
  });
  return <>{job.data?.title || `Job ${shortId(id)}`}</>;
}
