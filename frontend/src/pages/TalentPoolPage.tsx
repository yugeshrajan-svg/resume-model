import { useState } from 'react';
import type { FormEvent } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Plus, Search, UserRound } from 'lucide-react';
import { api } from '../api/client';
import type { Job, TalentResult, TalentSearch } from '../api/types';
import { useSession } from '../hooks/useSession';
import { date, label, shortId } from '../lib/format';
import { Empty, ErrorNotice, Spinner } from '../components/ui';
import ResumeDownload from '../components/ResumeDownload';
import UploadPanel from '../components/UploadPanel';
import SavedResumes from '../components/SavedResumes';

export default function TalentPoolPage() {
  const role = useSession()!.role;
  const [params, setParams] = useSearchParams();
  const canUpload = role !== 'hiring_manager';
  const requestedTab = params.get('tab');
  const tab =
    canUpload && (requestedTab === 'uploads' || requestedTab === 'library') ? requestedTab : 'search';
  const [query, setQuery] = useState('');
  const [jobId, setJobId] = useState('');
  const [searched, setSearched] = useState({ query: '', jobId: '' });
  const jobs = useQuery({
    queryKey: ['jobs'],
    queryFn: ({ signal }) => api.request<Job[]>('/jobs', { signal }),
  });
  const search = useMutation({
    mutationFn: (body: { query?: string; job_id?: string }) =>
      api.request<TalentSearch>('/talent-pool/search', { method: 'POST', body: { ...body, limit: 30 } }),
  });
  const text = query.trim();
  const ready = text.length >= 3 || !!jobId;
  function submit(event: FormEvent) {
    event.preventDefault();
    if (!ready) return;
    setSearched({ query: text, jobId });
    search.mutate({ ...(text.length >= 3 ? { query: text } : {}), ...(jobId ? { job_id: jobId } : {}) });
  }
  const data = search.data;
  return (
    <div className="page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Hiring</span>
          <h1>Talent pool.</h1>
          <p>
            Build your resume library and find people who fit a role. Upload resumes here without a job, then
            search and download matches whenever you need them.
          </p>
        </div>
      </div>
      {canUpload && (
        <div className="tabs" role="tablist" aria-label="Talent pool workspace">
          {[
            ['search', 'Search resumes', 'Find and download matches'],
            ['library', 'Saved resumes', 'Profiles, files and consent'],
            ['uploads', 'Upload resumes', 'Add local files or import from Drive'],
          ].map(([value, title, description]) => (
            <button
              key={value}
              role="tab"
              aria-label={title}
              aria-selected={tab === value}
              aria-controls={`talent-panel-${value}`}
              id={`talent-tab-${value}`}
              tabIndex={tab === value ? 0 : -1}
              className={tab === value ? 'selected' : ''}
              onClick={() => setParams({ tab: value })}
              onKeyDown={(event) => {
                if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
                event.preventDefault();
                const values = ['search', 'library', 'uploads'];
                const index = values.indexOf(value);
                const next =
                  values[
                    event.key === 'Home'
                      ? 0
                      : event.key === 'End'
                        ? 2
                        : (index + (event.key === 'ArrowLeft' ? 2 : 1)) % 3
                  ];
                setParams({ tab: next });
                document.getElementById(`talent-tab-${next}`)?.focus();
              }}
            >
              <span className="workflow-tab-copy">
                <strong>{title}</strong>
                <small>{description}</small>
              </span>
            </button>
          ))}
        </div>
      )}
      {canUpload && tab === 'library' && (
        <div role="tabpanel" id="talent-panel-library" aria-labelledby="talent-tab-library">
          <SavedResumes />
        </div>
      )}
      {canUpload && (
        <div
          role="tabpanel"
          id="talent-panel-uploads"
          aria-labelledby="talent-tab-uploads"
          hidden={tab !== 'uploads'}
        >
          <UploadPanel />
        </div>
      )}
      <div
        role={canUpload ? 'tabpanel' : undefined}
        id="talent-panel-search"
        aria-labelledby={canUpload ? 'talent-tab-search' : undefined}
        hidden={tab !== 'search'}
      >
        <section className="panel">
          <form className="talent-search" onSubmit={submit}>
            <label className="talent-query">
              Describe who you are looking for
              <input
                value={query}
                maxLength={1000}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="e.g. ML engineer with PyTorch and Kubernetes, Bengaluru"
              />
            </label>
            <label>
              Or match a job
              <select value={jobId} onChange={(event) => setJobId(event.target.value)}>
                <option value="">No job selected</option>
                {jobs.data?.map((job) => (
                  <option key={job.id} value={job.id}>
                    {job.title}
                  </option>
                ))}
              </select>
            </label>
            <button className="button primary" disabled={search.isPending || !ready} type="submit">
              <Search size={16} />
              {search.isPending ? 'Searching…' : 'Search'}
            </button>
          </form>
          <p className="field-hint">
            Enter at least 3 characters, choose a job, or both. With a job selected, candidates who already
            applied to it are marked.
          </p>
          <ErrorNotice error={jobs.error} retry={() => void jobs.refetch()} />
          <div className="search-examples" aria-label="Example candidate searches">
            <span className="muted">Try a search:</span>
            {['Python backend engineer', 'Data scientist with SQL', 'React frontend developer'].map(
              (example) => (
                <button key={example} className="text-button" onClick={() => setQuery(example)}>
                  {example}
                </button>
              ),
            )}
          </div>
          <p className="field-hint">
            Matches show similarity to your search, not a hiring score. Open a profile to review evidence and
            consent.
          </p>
        </section>
        {!data && !search.isPending && (
          <Empty
            title="Find the right resume"
            action={
              canUpload ? (
                <Link className="button secondary" to="?tab=library">
                  Browse saved profiles & consent
                </Link>
              ) : undefined
            }
          >
            Describe the skills or experience you need, or choose a job. You can open and download matching
            resumes from the results.
          </Empty>
        )}
        <ErrorNotice error={search.error} />
        {data && (searched.query !== text || searched.jobId !== jobId) && !search.isPending && (
          <p className="notice" role="status">
            Your search has changed. Select Search to update the matches below.
          </p>
        )}
        {search.isPending && <Spinner text="Searching your candidates…" />}
        {data && (
          <section className="panel">
            <div className="section-heading">
              <div>
                <h2>
                  Best matches <span className="count">{data.results.length}</span>
                </h2>
                <p>
                  Searched {data.indexed_candidates} candidates
                  {data.resumes_pending_index > 0 &&
                    ` · ${data.resumes_pending_index} resumes are still being indexed`}
                  .
                </p>
              </div>
            </div>
            {!data.results.length ? (
              <Empty title="No candidates found">
                {data.indexed_candidates
                  ? 'Try describing the role differently.'
                  : 'Upload resumes first. Each processed resume joins the talent pool automatically.'}
              </Empty>
            ) : (
              <ol className="talent-results">
                {data.results.map((result, index) => (
                  <TalentRow
                    key={result.candidate_id}
                    result={result}
                    rank={index + 1}
                    jobs={jobs.data || []}
                    defaultJob={jobId}
                    canAdd={role !== 'hiring_manager'}
                  />
                ))}
              </ol>
            )}
          </section>
        )}
      </div>
    </div>
  );
}
function TalentRow({
  result,
  rank,
  jobs,
  defaultJob,
  canAdd,
}: {
  result: TalentResult;
  rank: number;
  jobs: Job[];
  defaultJob: string;
  canAdd: boolean;
}) {
  const client = useQueryClient();
  const [target, setTarget] = useState(defaultJob);
  const add = useMutation({
    mutationFn: () =>
      api.request<{ created: boolean; stage: string }>(`/jobs/${target}/applications`, {
        method: 'POST',
        body: { candidate_id: result.candidate_id },
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['candidate', result.candidate_id] });
    },
  });
  const name = result.name || `Candidate ${shortId(result.candidate_id)}`;
  const match = Math.max(0, Math.min(100, Math.round(result.similarity * 100)));
  return (
    <li className="talent-row">
      <span className="rank">{rank}</span>
      <span className="candidate-avatar" aria-hidden="true">
        <UserRound size={18} />
      </span>
      <div className="talent-main">
        <Link className="candidate-name" to={`/candidates/${result.candidate_id}`}>
          {name}
        </Link>
        <Link className="text-button" to={`/candidates/${result.candidate_id}?from=library`}>
          Open profile &amp; consent
        </Link>
        <span className="candidate-subline">
          Resume v{result.matched_resume.version ?? '—'}
          {result.matched_resume.created_at && ` · ${date(result.matched_resume.created_at)}`} ·{' '}
          {result.applications} {result.applications === 1 ? 'application' : 'applications'}
          {result.in_job && ` · Already in this job (${label(result.in_job.stage)})`}
        </span>
        {canAdd && <ResumeDownload resumeId={result.matched_resume.resume_id} />}
      </div>
      <div className="talent-match" title="Semantic similarity to your search, not a screening score">
        <strong>{match}%</strong>
        <span>match</span>
        <div className="score-track" aria-hidden="true">
          <span style={{ width: `${match}%` }} />
        </div>
      </div>
      {canAdd && (
        <div className="talent-add">
          {add.isSuccess ? (
            <span className="success-message" role="status">
              <Check size={14} /> {add.data.created ? 'Added to job' : 'Already applied'}
            </span>
          ) : (
            <>
              <select
                aria-label={`Job to add ${name} to`}
                value={target}
                onChange={(event) => setTarget(event.target.value)}
              >
                <option value="">Choose a job</option>
                {jobs.map((job) => (
                  <option key={job.id} value={job.id}>
                    {job.title}
                  </option>
                ))}
              </select>
              <button
                className="button secondary compact"
                disabled={!target || add.isPending}
                onClick={() => add.mutate()}
              >
                <Plus size={14} />
                {add.isPending ? 'Adding…' : 'Add to job'}
              </button>
            </>
          )}
          <ErrorNotice error={add.error} />
        </div>
      )}
    </li>
  );
}
