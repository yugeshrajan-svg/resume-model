import { useState } from 'react';
import type { FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Plus, Search, UserRound } from 'lucide-react';
import { api } from '../api/client';
import type { Job, TalentResult, TalentSearch } from '../api/types';
import { useSession } from '../hooks/useSession';
import { date, label, shortId } from '../lib/format';
import { Empty, ErrorNotice, Spinner } from '../components/ui';

export default function TalentPoolPage() {
  const role = useSession()!.role;
  const [query, setQuery] = useState('');
  const [jobId, setJobId] = useState('');
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
            Find people from earlier uploads who fit a role, by meaning rather than exact keywords. Results
            are suggestions: add a candidate to a job and screen them to get a score.
          </p>
        </div>
      </div>
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
      </section>
      <ErrorNotice error={search.error} />
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
        <span className="candidate-subline">
          Resume v{result.matched_resume.version ?? '—'}
          {result.matched_resume.created_at && ` · ${date(result.matched_resume.created_at)}`} ·{' '}
          {result.applications} {result.applications === 1 ? 'application' : 'applications'}
          {result.in_job && ` · Already in this job (${label(result.in_job.stage)})`}
        </span>
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
