import { useMutation } from '@tanstack/react-query';
import { Download } from 'lucide-react';
import { api } from '../api/client';
import { ErrorNotice } from './ui';

export default function ResumeDownload({
  resumeId,
  filename = 'resume',
}: {
  resumeId: string;
  filename?: string;
}) {
  const download = useMutation({
    mutationFn: async () => {
      const blob = await api.request<Blob>(`/resumes/${resumeId}/download`, { blob: true });
      const extension = blob.type.includes('pdf') ? '.pdf' : blob.type.includes('word') ? '.docx' : '.txt';
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = /\.(pdf|docx|txt)$/i.test(filename) ? filename : filename + extension;
      anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
    },
  });
  return (
    <div>
      <button className="text-button" disabled={download.isPending} onClick={() => download.mutate()}>
        <Download size={14} />
        {download.isPending ? 'Downloading…' : 'Download resume'}
      </button>
      <ErrorNotice error={download.error} />
    </div>
  );
}
