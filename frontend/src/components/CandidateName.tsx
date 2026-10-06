import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import type { CandidateProfile } from '../api/types';
import { useSession } from '../hooks/useSession';
import { shortId } from '../lib/format';

export default function CandidateName({ id }: { id: string }) {
  const canReadContacts = useSession()!.role !== 'hiring_manager';
  const profile = useQuery({
    queryKey: ['candidate', id],
    queryFn: ({ signal }) => api.request<CandidateProfile>(`/candidates/${id}`, { signal }),
    enabled: canReadContacts,
    staleTime: 60_000,
  });
  return <>{(canReadContacts && profile.data?.contact?.name) || `Candidate ${shortId(id)}`}</>;
}
