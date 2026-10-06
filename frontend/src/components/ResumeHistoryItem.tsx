import { useMutation, useQueryClient } from '@tanstack/react-query';
import { FileText, RefreshCw } from 'lucide-react';
import { api } from '../api/client';
import type { CandidateProfile, Resume } from '../api/types';
import { useSession } from '../hooks/useSession';
import { date } from '../lib/format';
import { Badge, ErrorNotice } from './ui';

export default function ResumeHistoryItem({
  candidateId,
  resume,
}: {
  candidateId: string;
  resume: CandidateProfile['resumes'][number];
}) {
  const canWrite = useSession()!.role !== 'hiring_manager';
  const client = useQueryClient();
  const reprocess = useMutation({
    mutationFn: () => api.request<Resume>(`/resumes/${resume.resume_id}/reprocess`, { method: 'POST' }),
    onSuccess: async (data) => {
      client.setQueryData(['resume', resume.resume_id], data);
      await client.invalidateQueries({ queryKey: ['candidate', candidateId] });
    },
  });
  return (
    <div>
      <FileText size={20} />
      <div>
        <strong>{canWrite && resume.filename ? resume.filename : 'Filename hidden for your role'}</strong>
        <small>
          Version {resume.version} · {date(resume.created_at)}
        </small>
        <Badge value={resume.status} />
        {canWrite && ['failed', 'needs_ocr'].includes(resume.status) && (
          <button className="text-button" disabled={reprocess.isPending} onClick={() => reprocess.mutate()}>
            <RefreshCw size={14} />
            {reprocess.isPending ? 'Reprocessing…' : `Reprocess version ${resume.version}`}
          </button>
        )}
        <ErrorNotice error={reprocess.error} />
      </div>
    </div>
  );
}
