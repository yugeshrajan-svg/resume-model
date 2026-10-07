import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { RefreshCw } from 'lucide-react';
import { api } from '../api/client';
import { date, shortId } from '../lib/format';
import { Badge, Empty, ErrorNotice, Spinner } from './ui';
import ResumeDownload from './ResumeDownload';

type Library = {
  total: number;
  summary: { total: number; processing: number; attention: number; needs_consent: number };
  items: {
    resume_id: string;
    candidate_id: string | null;
    filename: string;
    name: string | null;
    status: string;
    version: number | null;
    created_at: string;
    has_consent: boolean;
    has_pool_consent: boolean;
  }[];
};
export default function SavedResumes() {
  const [q, setQ] = useState('');
  const [view, setView] = useState('all');
  const [offset, setOffset] = useState(0);
  const library = useQuery({
    queryKey: ['resume-library', q, view, offset],
    queryFn: ({ signal }) =>
      api.request<Library>(
        `/talent-pool/resumes?${new URLSearchParams({ q, view, offset: String(offset), limit: '25' })}`,
        { signal },
      ),
    refetchInterval: (query) => ((query.state.data?.summary.processing ?? 0) > 0 ? 5000 : false),
  });
  const data = library.data;
  return (
    <section className="panel">
      <div className="section-heading">
        <div>
          <h2>Saved resumes</h2>
          <p>Open any profile here, including candidates who haven’t been screened yet.</p>
        </div>
        <button
          className="button secondary compact"
          disabled={library.isFetching}
          onClick={() => void library.refetch()}
        >
          <RefreshCw size={14} />
          Refresh resumes
        </button>
      </div>
      {data && (
        <div className="library-summary" aria-label="Resume library summary">
          {[
            ['all', `${data.summary.total} saved files`],
            ['processing', `${data.summary.processing} processing`],
            ['consent', `${data.summary.needs_consent} need future-role consent`],
            ['attention', `${data.summary.attention} need attention`],
          ].map(([value, text]) => (
            <button
              key={value}
              className={`button ${view === value ? 'primary' : 'secondary'} compact`}
              aria-pressed={view === value}
              onClick={() => {
                setView(value);
                setOffset(0);
              }}
            >
              {text}
            </button>
          ))}
        </div>
      )}
      <label className="library-search">
        Find a saved resume
        <input
          value={q}
          onChange={(event) => {
            setQ(event.target.value);
            setOffset(0);
          }}
          maxLength={300}
          placeholder="Candidate name or filename"
        />
      </label>
      <ErrorNotice error={library.error} retry={() => void library.refetch()} />
      {library.isPending && <Spinner text="Loading saved resumes…" />}
      {data && !data.items.length && (
        <Empty
          title={data.summary.total ? 'No files match these filters' : 'Your resume library is empty'}
          action={
            data.summary.total ? (
              <button
                className="button secondary"
                onClick={() => {
                  setQ('');
                  setView('all');
                  setOffset(0);
                }}
              >
                Reset filters
              </button>
            ) : (
              <Link className="button primary" to="?tab=uploads">
                Upload resumes
              </Link>
            )
          }
        >
          {data.summary.total
            ? 'Try a different name or choose All saved files.'
            : 'Add local files or import a Drive folder to start building your library.'}
        </Empty>
      )}
      <div className="library-list">
        {data?.items.map((item) => (
          <article key={item.resume_id} className="library-row">
            <div className="library-identity">
              <strong>
                {item.name ||
                  (item.candidate_id ? `Candidate ${shortId(item.candidate_id)}` : 'Processing candidate')}
              </strong>
              <span className="candidate-subline">{item.filename}</span>
              <small>
                {date(item.created_at)}
                {item.version && ` · Version ${item.version}`}
              </small>
            </div>
            <div className="library-status">
              <Badge value={item.status} />
              <span className="field-hint">
                {item.has_pool_consent
                  ? 'Future-role consent recorded'
                  : item.has_consent
                    ? 'Job consent only · future-role consent needed'
                    : item.candidate_id
                      ? 'Consent needed before screening'
                      : 'Profile available after processing'}
              </span>
              {['needs_ocr', 'failed'].includes(item.status) && (
                <span className="field-hint">Open the profile to retry processing.</span>
              )}
            </div>
            <div className="library-actions">
              {item.candidate_id && (
                <Link
                  className="button secondary compact"
                  to={`/candidates/${item.candidate_id}?from=library`}
                >
                  {item.has_pool_consent ? 'Open profile' : 'Profile & consent'}
                </Link>
              )}
              <ResumeDownload resumeId={item.resume_id} filename={item.filename} />
            </div>
          </article>
        ))}
      </div>
      {data && data.total > 25 && (
        <nav className="upload-pagination" aria-label="Saved resume pages">
          <span className="muted">
            {offset + 1}–{Math.min(offset + 25, data.total)} of {data.total} files
          </span>
          <div className="inline-actions">
            <button className="button secondary" disabled={!offset} onClick={() => setOffset(offset - 25)}>
              Previous resumes
            </button>
            <button
              className="button secondary"
              disabled={offset + 25 >= data.total}
              onClick={() => setOffset(offset + 25)}
            >
              Next resumes
            </button>
          </div>
        </nav>
      )}
      <p className="field-hint">
        Resume versions are listed separately. Consent must reflect what each candidate actually agreed to.
      </p>
    </section>
  );
}
