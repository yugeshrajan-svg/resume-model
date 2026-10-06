import { useEffect, useState, useSyncExternalStore } from 'react';
import type { ChangeEvent, DragEvent } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import { ArrowUpRight, FileText, RefreshCw, Trash2, UploadCloud } from 'lucide-react';
import { api } from '../api/client';
import type { Resume } from '../api/types';
import { useSession } from '../hooks/useSession';
import { getUploadQueue, MAX_UPLOAD_BATCH } from '../lib/uploadQueue';
import type { UploadEntry, UploadQueue } from '../lib/uploadQueue';
import { Badge, ErrorNotice } from './ui';

const PAGE_SIZE = 25;

export default function UploadPanel({ jobId }: { jobId: string }) {
  const [params] = useSearchParams();
  const current = useSession()!;
  const queue = getUploadQueue(current.company_id, jobId);
  const { entries, busy } = useSyncExternalStore(queue.subscribe, queue.get);
  const [consented, setConsented] = useState(false);
  const [source, setSource] = useState('');
  const [purpose, setPurpose] = useState('Resume screening for this job');
  const [pool, setPool] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [page, setPage] = useState(0);
  const lastPage = Math.max(0, Math.ceil(entries.length / PAGE_SIZE) - 1);
  const currentPage = Math.min(page, lastPage);
  const visible = entries.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);
  const registered = entries.filter((entry) => entry.resume).length;
  const failedUploads = entries.filter((entry) => !entry.resume && entry.status === 'failed').length;
  const pending = entries.some((entry) => !entry.resume && queue.canRetry(entry.id));
  const canUpload = current.role !== 'hiring_manager';
  function add(files: FileList | null) {
    if (!files?.length) return;
    setError(null);
    try {
      queue.add(Array.from(files));
      setPage(Math.floor(entries.length / PAGE_SIZE));
    } catch (error) {
      setError(error);
    }
  }
  function selected(event: ChangeEvent<HTMLInputElement>) {
    add(event.target.files);
    event.target.value = '';
  }
  function dropped(event: DragEvent) {
    event.preventDefault();
    setDragging(false);
    if (!busy) add(event.dataTransfer.files);
  }
  function start(id?: string) {
    if (consented && (source.trim().length < 2 || purpose.trim().length < 2)) {
      setError(new Error('Enter the consent source and purpose.'));
      return;
    }
    setError(null);
    void queue.start(
      consented
        ? { source: source.trim(), purpose: purpose.trim(), scope: pool ? 'talent_pool' : 'job' }
        : null,
      id,
    );
  }
  return (
    <section className="panel uploads-panel">
      <div className="section-heading">
        <div>
          <h2>Add your candidates</h2>
          <p>Drop in your resumes. We’ll keep track of each file for you.</p>
        </div>
        <span className="subtle-chip">PDF · DOCX · TXT</span>
      </div>
      {canUpload ? (
        <>
          <div
            className={`dropzone ${dragging ? 'dragging' : ''} ${busy ? 'disabled' : ''}`}
            onDragOver={(event) => {
              event.preventDefault();
              if (!busy) setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={dropped}
          >
            <span className="dropzone-icon">
              <UploadCloud size={29} />
            </span>
            <h3>Drop resumes here</h3>
            <p>or select files from your computer</p>
            <label className="button secondary file-picker">
              Choose files
              <input
                aria-label="Choose resumes"
                type="file"
                accept=".pdf,.docx,.txt"
                multiple
                onChange={selected}
                disabled={busy}
              />
            </label>
            <small>PDF, DOCX or TXT · 1–{MAX_UPLOAD_BATCH} resumes per batch</small>
          </div>
          <fieldset disabled={busy} className="consent-box">
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={consented}
                onChange={(event) => setConsented(event.target.checked)}
              />
              These candidates have given consent for this purpose.
            </label>
            <p className="field-hint">
              Only select this when you have collected consent from every candidate in the new batch.
              Candidates without consent can be uploaded but cannot be screened.
            </p>
            {consented && (
              <div className="form-grid">
                <label>
                  Consent source
                  <input
                    value={source}
                    minLength={2}
                    maxLength={200}
                    onChange={(event) => setSource(event.target.value)}
                    placeholder="e.g. Application form"
                  />
                </label>
                <label>
                  Consent purpose
                  <input
                    value={purpose}
                    minLength={2}
                    maxLength={300}
                    onChange={(event) => setPurpose(event.target.value)}
                  />
                </label>
              </div>
            )}
            {consented && (
              <label className="checkbox-label">
                <input type="checkbox" checked={pool} onChange={(event) => setPool(event.target.checked)} />
                They also agreed to be considered for future roles (talent pool)
              </label>
            )}
          </fieldset>
          <ErrorNotice error={error} />
          {pending && (
            <div className="form-actions">
              <span className="muted">
                {busy ? 'Uploading and registering files… You can check another tab.' : 'Ready when you are.'}
              </span>
              <button className="button primary" disabled={busy} onClick={() => start()}>
                <UploadCloud size={17} />
                {busy ? 'Uploading…' : 'Upload and register'}
              </button>
            </div>
          )}
        </>
      ) : (
        <div className="notice">
          Resume uploads are available to admins and recruiters. Open screening results to review candidates.
        </div>
      )}
      {entries.length > 0 && (
        <div className="upload-list">
          <div className="section-heading">
            <h3>
              Files in this browser session <span className="count">{entries.length}</span>
            </h3>
            {canUpload && entries.some((entry) => !entry.resume) && (
              <button
                className="text-button"
                disabled={busy}
                onClick={() => {
                  queue.clearWaiting();
                  setError(null);
                  setPage(0);
                }}
              >
                <Trash2 size={14} />
                Clear waiting files
              </button>
            )}
          </div>
          <div className="upload-batch-summary" role="status" aria-live="polite" aria-atomic="true">
            <strong>
              {registered} of {entries.length} files registered
            </strong>
            <span className="muted">
              {failedUploads
                ? `${failedUploads} upload${failedUploads === 1 ? '' : 's'} need attention. `
                : ''}
              {busy
                ? 'Uploading in small batches. Keep this browser tab open.'
                : 'Registered resumes are processed in the background.'}
            </span>
            <progress aria-label="Files registered" value={registered} max={entries.length} />
          </div>
          {visible.map((entry) => (
            <UploadRow
              key={entry.id}
              entry={entry}
              queue={queue}
              busy={busy}
              canWrite={canUpload}
              retry={() => start(entry.id)}
            />
          ))}
          {lastPage > 0 && (
            <nav className="upload-pagination" aria-label="Upload pages">
              <span className="muted">
                Files {currentPage * PAGE_SIZE + 1}–{Math.min((currentPage + 1) * PAGE_SIZE, entries.length)}{' '}
                of {entries.length}
              </span>
              <div className="inline-actions">
                <button
                  className="button secondary"
                  disabled={currentPage === 0}
                  onClick={() => setPage(currentPage - 1)}
                >
                  Previous files
                </button>
                <span className="muted">
                  Page {currentPage + 1} of {lastPage + 1}
                </span>
                <button
                  className="button secondary"
                  disabled={currentPage === lastPage}
                  onClick={() => setPage(currentPage + 1)}
                >
                  Next files
                </button>
              </div>
            </nav>
          )}
        </div>
      )}
      <p className="field-hint upload-session-note">
        Registered files are saved in your workspace. This upload list is remembered in this browser tab;
        screening results remain available across devices.
      </p>
      <div className="form-actions">
        <Link
          className="button secondary"
          to={`?tab=results&source=${params.get('source') === 'both' ? 'both' : 'uploads'}`}
        >
          Continue to screening
          <ArrowUpRight size={16} />
        </Link>
      </div>
    </section>
  );
}
function UploadRow({
  entry,
  queue,
  busy,
  canWrite,
  retry,
}: {
  entry: UploadEntry;
  queue: UploadQueue;
  busy: boolean;
  canWrite: boolean;
  retry: () => void;
}) {
  const resume = useQuery({
    queryKey: ['resume', entry.resume?.id],
    queryFn: ({ signal }) => api.request<Resume>(`/resumes/${entry.resume!.id}`, { signal }),
    enabled: !!entry.resume && canWrite,
    refetchInterval: (query) =>
      !query.state.data || ['registered', 'processing'].includes(query.state.data.status) ? 5000 : false,
    refetchIntervalInBackground: false,
  });
  useEffect(() => {
    if (resume.data)
      queue.update(entry.id, {
        resume: resume.data,
        status: resume.data.status,
        error: resume.data.status_reason || undefined,
      });
  }, [resume.data, entry.id, queue]);
  const data = resume.data || entry.resume;
  const reprocess = useMutation({
    mutationFn: () => api.request<Resume>(`/resumes/${data!.id}/reprocess`, { method: 'POST' }),
    onSuccess: (result) => {
      queue.update(entry.id, { resume: result, status: result.status, error: undefined });
      void resume.refetch();
    },
  });
  const status = data?.status || entry.status;
  return (
    <div className="upload-row">
      <div className="file-icon">
        <FileText size={21} />
      </div>
      <div className="upload-row-main">
        <strong>{entry.name}</strong>
        <span>
          {entry.size < 1024 * 1024
            ? `${Math.max(1, Math.round(entry.size / 1024))} KB`
            : `${(entry.size / 1024 / 1024).toFixed(1)} MB`}
          {entry.created === false ? ' · Duplicate linked to this job' : ''}
        </span>
        {['uploading', 'uploaded', 'registering'].includes(status) && (
          <div className="progress-track">
            <span style={{ width: `${entry.percent}%` }} />
          </div>
        )}
        {(data?.status_reason || entry.error) && (
          <p className="file-error">{data?.status_reason || entry.error}</p>
        )}
        <ErrorNotice
          error={resume.error || reprocess.error}
          retry={
            resume.error
              ? () => {
                  void resume.refetch();
                }
              : undefined
          }
        />
      </div>
      <div className="upload-row-actions">
        <Badge value={status} />
        {data?.candidate_id && (
          <Link className="text-button" to={`/candidates/${data.candidate_id}`}>
            Profile
            <ArrowUpRight size={14} />
          </Link>
        )}
        {canWrite && data && ['failed', 'needs_ocr'].includes(status) && (
          <button className="text-button" disabled={reprocess.isPending} onClick={() => reprocess.mutate()}>
            <RefreshCw size={14} />
            {reprocess.isPending ? 'Retrying…' : 'Reprocess'}
          </button>
        )}
        {canWrite && !data && status === 'failed' && queue.canRetry(entry.id) && (
          <button className="text-button" disabled={busy} onClick={retry}>
            <RefreshCw size={14} />
            Retry
          </button>
        )}
        {!data && (
          <button
            className="icon-button"
            aria-label={`Remove ${entry.name}`}
            disabled={busy}
            onClick={() => queue.remove(entry.id)}
          >
            <Trash2 size={16} />
          </button>
        )}
      </div>
    </div>
  );
}
