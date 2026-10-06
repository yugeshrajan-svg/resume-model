import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Download, Trash2 } from 'lucide-react';
import { api } from '../api/client';
import { useSession } from '../hooks/useSession';
import { removeCandidateUploads } from '../lib/uploadQueue';
import { ErrorNotice } from './ui';
import ConfirmDialog from './ConfirmDialog';

export default function PrivacyPanel({
  candidateId,
  resumeIds,
  onDeleted,
}: {
  candidateId: string;
  resumeIds: string[];
  onDeleted: () => void;
}) {
  const company = useSession()!.company_id;
  const client = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [exported, setExported] = useState(false);
  const download = useMutation({
    mutationFn: async () => {
      const data = await api.request(`/candidates/${candidateId}/export`);
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }),
      );
      const link = document.createElement('a');
      link.href = url;
      link.download = `candidate-${candidateId}.json`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    },
    onSuccess: () => setExported(true),
  });
  const erase = useMutation({
    mutationFn: () => api.request<{ deleted: boolean }>(`/candidates/${candidateId}`, { method: 'DELETE' }),
    onSuccess: async () => {
      // Cancel in-flight reads before removing erased data, so late responses cannot repopulate it.
      const keys = [
        'candidate',
        'consents',
        'decisions',
        'compare',
        'ranked',
        'runs',
        'run-inputs',
        'resume',
      ];
      await client.cancelQueries({ predicate: (query) => keys.includes(String(query.queryKey[0])) });
      client.removeQueries({
        predicate: (query) =>
          (['candidate', 'consents'].includes(String(query.queryKey[0])) &&
            query.queryKey[1] === candidateId) ||
          ['decisions', 'compare', 'ranked', 'run-inputs'].includes(String(query.queryKey[0])) ||
          (query.queryKey[0] === 'resume' && resumeIds.includes(String(query.queryKey[1]))),
      });
      removeCandidateUploads(company, candidateId, resumeIds);
      onDeleted();
      await Promise.all(['runs', 'company'].map((key) => client.invalidateQueries({ queryKey: [key] })));
    },
  });
  return (
    <section className="panel privacy-panel">
      <h3>Candidate data</h3>
      <p className="field-hint">
        Export a complete record or permanently erase this candidate from every job.
      </p>
      <ErrorNotice error={download.error} />
      {exported && (
        <p role="status" className="success-message">
          Export download started.
        </p>
      )}
      <div className="hr-actions">
        <button
          className="button secondary"
          disabled={download.isPending || erase.isPending}
          onClick={() => {
            setExported(false);
            download.mutate();
          }}
        >
          <Download size={16} />
          {download.isPending ? 'Preparing export…' : 'Export candidate data'}
        </button>
        <button
          className="button danger secondary"
          disabled={download.isPending || erase.isPending}
          onClick={() => {
            erase.reset();
            setAcknowledged(false);
            setConfirm(true);
          }}
        >
          <Trash2 size={16} />
          Delete candidate
        </button>
      </div>
      {confirm && (
        <ConfirmDialog
          title="Permanently delete this candidate?"
          onClose={() => {
            if (!erase.isPending) setConfirm(false);
          }}
        >
          <p>
            This removes their resumes, applications, decisions, consent, and screening results across every
            job in this company. This action cannot be undone.
          </p>
          <p>Download an export first if the candidate requested a copy.</p>
          <label className="check-label">
            <input
              type="checkbox"
              checked={acknowledged}
              disabled={erase.isPending}
              onChange={(event) => setAcknowledged(event.target.checked)}
            />
            I understand this permanently erases the candidate
          </label>
          <ErrorNotice error={erase.error} />
          <div className="dialog-actions">
            <button className="button secondary" disabled={erase.isPending} onClick={() => setConfirm(false)}>
              Cancel
            </button>
            <button
              className="button danger"
              disabled={!acknowledged || erase.isPending}
              onClick={() => erase.mutate()}
            >
              {erase.isPending ? 'Deleting…' : 'Permanently delete'}
            </button>
          </div>
        </ConfirmDialog>
      )}
    </section>
  );
}
