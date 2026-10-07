import { useEffect, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api, message } from '../api/client';
import { session } from '../api/session';
import { ErrorNotice } from './ui';
import { Link } from 'react-router-dom';

type DriveFile = { id: string; filename: string };
type Consent = { source: string; purpose: string; scope: 'job' | 'talent_pool' } | null;
const PAGE_SIZE = 25;
const BATCH_SIZE = 500;

export default function DriveImport({
  jobId,
  consent,
  disabled,
  consentValid,
}: {
  jobId: string | null;
  consent: Consent;
  disabled: boolean;
  consentValid: boolean;
}) {
  const client = useQueryClient();
  const [link, setLink] = useState('');
  const [files, setFiles] = useState<DriveFile[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [outcomes, setOutcomes] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [page, setPage] = useState(0);
  const [progress, setProgress] = useState({ done: 0, total: 0 });
  const [pausing, setPausing] = useState(false);
  const stopped = useRef(false);
  useEffect(
    () => () => {
      stopped.current = true;
    },
    [],
  );
  const lastPage = Math.max(0, Math.ceil(files.length / PAGE_SIZE) - 1);
  const currentPage = Math.min(page, lastPage);
  const visible = files.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);
  const waiting = files.filter((file) => selected.has(file.id) && !outcomes[file.id]?.startsWith('Imported'));
  const preview = useMutation({
    mutationFn: () =>
      api.request<{ files: DriveFile[] }>('/drive/preview', { method: 'POST', body: { link } }),
    onSuccess: (data) => {
      setFiles(data.files);
      setSelected(new Set(data.files.map((file) => file.id)));
      setOutcomes({});
      setPage(0);
      setProgress({ done: 0, total: 0 });
    },
  });
  async function start() {
    const epoch = session.epoch;
    stopped.current = false;
    setPausing(false);
    setProgress({ done: 0, total: waiting.length });
    setBusy(true);
    try {
      for (const file of waiting) {
        if (stopped.current || session.epoch !== epoch) break;
        setOutcomes((previous) => ({ ...previous, [file.id]: 'Importing…' }));
        try {
          const result = await api.request<{ created: boolean }>('/drive/import', {
            method: 'POST',
            body: { file_id: file.id, filename: file.filename, job_id: jobId, consent },
          });
          setOutcomes((previous) => ({
            ...previous,
            [file.id]: result.created ? 'Imported' : 'Imported · already saved',
          }));
        } catch (error) {
          setOutcomes((previous) => ({ ...previous, [file.id]: message(error) }));
        }
        setProgress((previous) => ({ ...previous, done: previous.done + 1 }));
      }
    } finally {
      setBusy(false);
      if (jobId) void client.invalidateQueries({ queryKey: ['job-resumes', jobId] });
      void client.invalidateQueries({
        queryKey: jobId ? ['screening-sources', jobId] : ['screening-sources'],
      });
    }
  }
  return (
    <div className="consent-box">
      <h3>Import from Google Drive</h3>
      <p className="field-hint">
        Paste a file or folder link shared with “Anyone with the link”. PDF, DOCX and TXT files directly in
        the folder are supported. Large folders import automatically in batches. Keep this tab open until
        import finishes.
      </p>
      <label>
        Google Drive link
        <input
          value={link}
          disabled={busy || disabled || preview.isPending}
          onChange={(event) => {
            setLink(event.target.value);
            setFiles([]);
            preview.reset();
          }}
          placeholder="https://drive.google.com/drive/folders/…"
        />
      </label>
      <button
        className="button secondary"
        disabled={!link.trim() || busy || disabled || preview.isPending}
        onClick={() => preview.mutate()}
      >
        {preview.isPending ? 'Reading Drive…' : 'Preview files'}
      </button>
      <ErrorNotice error={preview.error} />
      {files.length > 0 && (
        <>
          <p>
            {files.length} resumes found · {selected.size} selected ·{' '}
            {Object.values(outcomes).filter((value) => value.startsWith('Imported')).length} imported
          </p>
          {files.length > BATCH_SIZE && (
            <p className="field-hint">
              All {files.length} resumes are available. Selected files will continue automatically across{' '}
              {Math.ceil(selected.size / BATCH_SIZE)} batches of up to {BATCH_SIZE}.
            </p>
          )}
          <label className="checkbox-label">
            <input
              type="checkbox"
              disabled={busy || disabled}
              checked={selected.size === files.length}
              onChange={(event) =>
                setSelected(event.target.checked ? new Set(files.map((file) => file.id)) : new Set())
              }
            />
            Select all {files.length} files
          </label>
          <div style={{ maxHeight: 240, overflowY: 'auto' }}>
            {visible.map((file) => (
              <label className="checkbox-label" key={file.id}>
                <input
                  type="checkbox"
                  checked={selected.has(file.id)}
                  disabled={busy || disabled}
                  onChange={(event) =>
                    setSelected((previous) => {
                      const next = new Set(previous);
                      if (event.target.checked) next.add(file.id);
                      else next.delete(file.id);
                      return next;
                    })
                  }
                />
                {file.filename} {outcomes[file.id] && <span role="status"> · {outcomes[file.id]}</span>}
              </label>
            ))}
          </div>
          {lastPage > 0 && (
            <nav className="upload-pagination" aria-label="Drive file pages">
              <span className="muted">
                Files {currentPage * PAGE_SIZE + 1}–{Math.min((currentPage + 1) * PAGE_SIZE, files.length)} of{' '}
                {files.length}
              </span>
              <div className="inline-actions">
                <button
                  className="button secondary"
                  disabled={currentPage === 0}
                  onClick={() => setPage(currentPage - 1)}
                >
                  Previous Drive files
                </button>
                <span className="muted">
                  Page {currentPage + 1} of {lastPage + 1}
                </span>
                <button
                  className="button secondary"
                  disabled={currentPage === lastPage}
                  onClick={() => setPage(currentPage + 1)}
                >
                  Next Drive files
                </button>
              </div>
            </nav>
          )}
          <p className="field-hint">
            Uses the consent settings above. Without consent, imported candidates cannot be screened.
          </p>
          {progress.total > 0 && (
            <p role="status">
              {progress.done} of {progress.total} files attempted
              {busy &&
                ` · Batch ${Math.min(Math.floor(progress.done / BATCH_SIZE) + 1, Math.ceil(progress.total / BATCH_SIZE))} of ${Math.ceil(progress.total / BATCH_SIZE)}`}
              {pausing && busy && ' · Pausing after the current file…'}
            </p>
          )}
          <button
            className="button primary"
            disabled={busy || disabled || !waiting.length || !consentValid}
            onClick={() => void start()}
          >
            {busy ? 'Importing resumes…' : 'Import selected resumes / retry failed'}
          </button>
          {!busy && files.length > 0 && !waiting.length && (
            <div className="notice success" role="status">
              All selected files are saved. Background processing will make them searchable.{' '}
              <Link className="text-button" to="/talent-pool?tab=library">
                View saved resumes
              </Link>
            </div>
          )}
          {busy && (
            <button
              className="button secondary"
              disabled={pausing}
              onClick={() => {
                stopped.current = true;
                setPausing(true);
              }}
            >
              Pause import
            </button>
          )}
        </>
      )}
    </div>
  );
}
