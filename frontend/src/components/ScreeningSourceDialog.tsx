import { useState, useSyncExternalStore } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Database, Layers, Play, UploadCloud } from 'lucide-react';
import { api, ApiError } from '../api/client';
import type { ResumeSource, ScreeningSources } from '../api/types';
import { useSession } from '../hooks/useSession';
import { getUploadQueue } from '../lib/uploadQueue';
import ConfirmDialog from './ConfirmDialog';
import { ErrorNotice, Spinner } from './ui';

export default function ScreeningSourceDialog({
  jobId,
  initialSource,
  busy,
  error,
  onClose,
  onUpload,
  onStart,
}: {
  jobId: string;
  initialSource: ResumeSource | null;
  busy: boolean;
  error: unknown;
  onClose: () => void;
  onUpload: (source: ResumeSource) => void;
  onStart: (source: ResumeSource) => void;
}) {
  const current = useSession()!;
  const queue = getUploadQueue(current.company_id, jobId);
  const uploads = useSyncExternalStore(queue.subscribe, queue.get);
  const [source, setSource] = useState<ResumeSource | null>(initialSource);
  const sources = useQuery({
    queryKey: ['screening-sources', jobId],
    queryFn: ({ signal }) => api.request<ScreeningSources>(`/jobs/${jobId}/screening-sources`, { signal }),
    staleTime: 0,
    refetchOnMount: 'always',
    refetchInterval: (query) => (uploads.busy || (query.state.data?.pending_uploads ?? 0) > 0 ? 2000 : false),
  });
  const selected = source && sources.data ? sources.data[source] : null;
  const includesUploads = source === 'uploads' || source === 'both';
  const waitingFiles = uploads.entries.filter(
    (entry) => !entry.resume && entry.status !== 'failed' && !entry.error,
  ).length;
  const failedFiles = uploads.entries.filter(
    (entry) => !entry.resume && (entry.status === 'failed' || entry.error),
  ).length;
  const unfinishedUploads =
    includesUploads && (uploads.busy || waitingFiles > 0 || !!sources.data?.pending_uploads);
  const blocked =
    error instanceof ApiError ? (error.details as { blocked?: { reason: string }[] })?.blocked : [];
  return (
    <ConfirmDialog title="Choose resumes for screening" onClose={onClose} initialFocus="input[type=radio]">
      <p className="source-intro">Choose saved resumes, uploads for this job, or combine both.</p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (source && selected?.total && !unfinishedUploads && !busy && !sources.error) onStart(source);
        }}
      >
        <fieldset className="screening-source-options" disabled={busy}>
          <legend>Resume source</legend>
          {(
            [
              [
                'database',
                'Saved resumes',
                'Ready resumes in your workspace with talent-pool consent.',
                Database,
              ],
              [
                'uploads',
                'Uploaded resumes',
                'Only resumes uploaded to this job. Add new files below.',
                UploadCloud,
              ],
              [
                'both',
                'Both sources',
                'Combine saved resumes and job uploads. Each candidate appears once.',
                Layers,
              ],
            ] as const
          ).map(([value, title, description, Icon]) => (
            <label className={`screening-source-option ${source === value ? 'selected' : ''}`} key={value}>
              <input
                type="radio"
                name="resume-source"
                value={value}
                checked={source === value}
                onChange={() => setSource(value)}
              />
              <Icon size={21} aria-hidden="true" />
              <span className="screening-source-copy">
                <strong>{title}</strong>
                <small>{description}</small>
              </span>
              <span className="source-count">{sources.data ? sources.data[value].total : '…'}</span>
            </label>
          ))}
        </fieldset>
        {sources.isPending && <Spinner text="Checking available resumes…" />}
        <ErrorNotice
          error={sources.error}
          retry={() => {
            void sources.refetch();
          }}
        />
        {selected && (
          <p className="source-summary" role="status">
            <strong>{selected.screenable} ready to screen</strong>
            {selected.blocked > 0 && ` · ${selected.blocked} need consent or processing`}
            {selected.total === 0 && ' · Add resumes to this workspace or choose another source.'}
          </p>
        )}
        {!!sources.data?.database_excluded && source !== 'uploads' && (
          <p className="field-hint">
            {sources.data.database_excluded} saved candidates are unavailable until their resumes are ready
            and talent-pool consent is recorded.
          </p>
        )}
        {unfinishedUploads && (
          <p className="notice" role="status">
            {waitingFiles || uploads.busy
              ? 'Finish uploading your selected files first.'
              : 'Uploaded resumes are still processing. This count updates automatically.'}
          </p>
        )}
        {includesUploads && failedFiles > 0 && (
          <p className="field-hint">
            {failedFiles} files could not be registered and will be excluded from screening. You can retry
            them from the upload tab.
          </p>
        )}
        <p className="field-hint">Saved resumes come from your current company workspace.</p>
        <ErrorNotice error={error} />
        {!!blocked?.length && (
          <ul className="blocked-list">
            {blocked.map((item, index) => (
              <li key={index}>{item.reason}</li>
            ))}
          </ul>
        )}
        <div className="source-upload-action">
          <button
            className="button secondary"
            type="button"
            disabled={busy}
            onClick={() => onUpload(source === 'both' ? 'both' : 'uploads')}
          >
            <UploadCloud size={16} /> Upload new resumes
          </button>
          <small>PDF, DOCX or TXT · Large selections upload automatically in small batches</small>
        </div>
        <div className="form-actions">
          <button className="button secondary" type="button" disabled={busy} onClick={onClose}>
            Cancel
          </button>
          <button
            className="button primary"
            type="submit"
            disabled={busy || sources.isPending || !!sources.error || !selected?.total || unfinishedUploads}
          >
            <Play size={16} /> {busy ? 'Starting…' : 'Screen selected resumes'}
          </button>
        </div>
      </form>
    </ConfirmDialog>
  );
}
