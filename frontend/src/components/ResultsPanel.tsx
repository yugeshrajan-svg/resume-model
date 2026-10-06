import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ArrowRight,
  ChevronLeft,
  ChevronRight,
  Play,
  RefreshCw,
  SlidersHorizontal,
  UserRound,
  X,
} from 'lucide-react';
import { api, ApiError } from '../api/client';
import type { CandidatesPage, RequirementsVersion, ResumeSource, Run } from '../api/types';
import { useSession } from '../hooks/useSession';
import { date, scoreTier, shortId } from '../lib/format';
import { Badge, Empty, ErrorNotice, Spinner } from './ui';
import CandidateName from './CandidateName';
import ComparePanel from './ComparePanel';
import RunRoster from './RunRoster';
import ScreeningSourceDialog from './ScreeningSourceDialog';

export default function ResultsPanel({
  jobId,
  confirmed,
  requirementsLoading,
}: {
  jobId: string;
  confirmed?: RequirementsVersion;
  requirementsLoading: boolean;
}) {
  const canRun = useSession()!.role !== 'hiring_manager';
  const client = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [eligible, setEligible] = useState('all');
  const [stage, setStage] = useState('all');
  const [minimum, setMinimum] = useState('');
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [offset, setOffset] = useState(0);
  const [selection, setSelection] = useState<{ runId: string; ids: string[] }>({ runId: '', ids: [] });
  const [compareRun, setCompareRun] = useState<string | null>(null);
  const [choosingSource, setChoosingSource] = useState(false);
  const runs = useQuery({
    queryKey: ['runs', jobId],
    queryFn: ({ signal }) => api.request<Run[]>(`/jobs/${jobId}/screening-runs`, { signal }),
    refetchInterval: (query) =>
      query.state.data?.some((run) => ['queued', 'running'].includes(run.status)) ? 2000 : false,
  });
  const requested = params.get('run');
  const active = requested ? runs.data?.find((run) => run.id === requested) : runs.data?.[0];
  const selected = selection.runId === active?.id ? selection.ids : [];
  function select(run: Run) {
    setOffset(0);
    setSelection({ runId: run.id, ids: [] });
    setCompareRun(null);
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set('run', run.id);
      return next;
    });
  }
  function created(run: Run) {
    setChoosingSource(false);
    client.setQueryData<Run[]>(['runs', jobId], (previous) => [
      run,
      ...(previous || []).filter((item) => item.id !== run.id),
    ]);
    select(run);
  }
  const start = useMutation({
    mutationFn: (source: ResumeSource) =>
      api.request<Run>(`/jobs/${jobId}/screening-runs`, { method: 'POST', body: { source } }),
    onSuccess: created,
  });
  const retry = useMutation({
    mutationFn: () => api.request<Run>(`/screening-runs/${active!.id}/retry`, { method: 'POST' }),
    onSuccess: created,
  });
  const filters = new URLSearchParams({ run_id: active?.id || '', limit: '20', offset: String(offset) });
  if (eligible !== 'all') filters.set('eligible', eligible);
  if (stage !== 'all') filters.set('stage', stage);
  const validMinimum =
    minimum === '' || (Number(minimum) >= 0 && Number(minimum) <= 100 && Number.isFinite(Number(minimum)));
  if (minimum !== '') filters.set('score_min', minimum);
  const candidates = useQuery({
    queryKey: ['ranked', jobId, active?.id, active?.done, active?.status, eligible, stage, minimum, offset],
    queryFn: ({ signal }) => api.request<CandidatesPage>(`/jobs/${jobId}/candidates?${filters}`, { signal }),
    enabled: !!active && validMinimum,
    refetchInterval: active && ['queued', 'running'].includes(active.status) ? 2000 : false,
  });
  // Highest-ranked eligible candidate and the eligible count, independent of the table filters.
  const eligibleTop = useQuery({
    queryKey: ['ranked', jobId, 'eligible-summary', active?.id, active?.done, active?.status],
    queryFn: ({ signal }) =>
      api.request<CandidatesPage>(
        `/jobs/${jobId}/candidates?${new URLSearchParams({ run_id: active!.id, eligible: 'true', limit: '1' })}`,
        { signal },
      ),
    enabled: !!active,
  });
  const actionError = choosingSource ? retry.error : start.error || retry.error;
  const blocked =
    actionError instanceof ApiError
      ? (actionError.details as { blocked?: { reason: string; candidate_id?: string }[] } | undefined)
          ?.blocked
      : undefined;
  const busy = start.isPending || retry.isPending;
  const filterCount = Number(eligible !== 'all') + Number(stage !== 'all') + Number(minimum !== '');
  function clearFilters() {
    setEligible('all');
    setStage('all');
    setMinimum('');
    setOffset(0);
  }
  return (
    <section className="panel results-panel">
      <div className="section-heading">
        <div>
          <h2>Your candidate shortlist</h2>
          <p>Review the rankings. Open a candidate to see what supports their score.</p>
        </div>
        {canRun && (
          <button
            className="button primary"
            disabled={busy || !confirmed || requirementsLoading}
            onClick={() => {
              retry.reset();
              start.reset();
              setChoosingSource(true);
            }}
          >
            <Play size={16} />
            {start.isPending ? 'Starting…' : 'Start screening'}
          </button>
        )}
      </div>
      {!confirmed && !requirementsLoading && (
        <div className="notice">
          Confirm requirements before starting a screening.{' '}
          <Link to="?tab=requirements">
            Review requirements
            <ArrowRight size={14} />
          </Link>
        </div>
      )}
      <ErrorNotice error={actionError} />
      {blocked?.length ? <BlockedList items={blocked} /> : null}
      <ErrorNotice
        error={runs.error}
        retry={() => {
          void runs.refetch();
        }}
      />
      {runs.isPending ? (
        <Spinner text="Loading screening runs…" />
      ) : !runs.data?.length ? (
        <Empty title="Your first screening is ahead">
          Confirm requirements, then choose saved resumes, new uploads, or both to start screening.
        </Empty>
      ) : (
        <>
          <div className="screening-overview">
            <label className="run-picker">
              Screening run
              <select
                aria-label="Screening run"
                value={active?.id || ''}
                onChange={(event) => {
                  const run = runs.data?.find((item) => item.id === event.target.value);
                  if (run) {
                    start.reset();
                    retry.reset();
                    select(run);
                  }
                }}
              >
                <option value="" disabled>
                  Select a run
                </option>
                {runs.data.map((run) => (
                  <option key={run.id} value={run.id}>
                    {date(run.created_at)} · {shortId(run.id)} · Requirements v{run.requirements_version}
                    {run.retry_of_run_id ? ' · Retry' : ''}
                  </option>
                ))}
              </select>
            </label>
            {!active ? (
              <div className="notice error">
                The selected run is unavailable for this job. Choose another screening run.
              </div>
            ) : (
              <>
                <div className="run-progress">
                  <div className="run-progress-top">
                    <div>
                      <Badge value={active.status} />
                      <strong>
                        {active.done} of {active.total} candidates processed
                      </strong>
                    </div>
                  </div>
                  <progress
                    max={Math.max(1, active.total)}
                    value={active.total === 0 && active.status === 'completed' ? 1 : active.done}
                    aria-label="Screening progress"
                  />
                  {['queued', 'running'].includes(active.status) && (
                    <p className="field-hint" role="status">
                      Updates automatically while screening is in progress.
                    </p>
                  )}
                </div>
              </>
            )}
          </div>
          {active && (
            <>
              <div className="stat-grid results-summary" aria-label="Run summary">
                <div className="stat-card">
                  <div>
                    <strong>{active.total}</strong>
                    <span>Candidates in this run</span>
                  </div>
                </div>
                <div className="stat-card tone-good">
                  <div>
                    <strong>{eligibleTop.data?.total ?? '—'}</strong>
                    <span>Eligible</span>
                  </div>
                </div>
                <div className="stat-card">
                  <div>
                    <strong>{eligibleTop.data?.items[0]?.score?.toFixed(1) ?? '—'}</strong>
                    <span>Top eligible score</span>
                  </div>
                </div>
                <div className={`stat-card ${active.blocked.length ? 'tone-warn' : ''}`}>
                  <div>
                    <strong>{active.blocked.length}</strong>
                    <span>Excluded before screening</span>
                  </div>
                </div>
              </div>
              <details className="screening-run-details">
                <summary>Run details</summary>
                <div className="run-meta">
                  <span>Requirements v{active.requirements_version}</span>
                  <span>Reference date {active.reference_date || 'Unavailable'}</span>
                  <span>Run {shortId(active.id)}</span>
                  {active.snapshot?.resume_source && (
                    <span>
                      Resumes:{' '}
                      {
                        { database: 'Saved resumes', uploads: 'Uploaded resumes', both: 'Both sources' }[
                          active.snapshot.resume_source
                        ]
                      }
                    </span>
                  )}
                </div>
                {canRun && active.status === 'completed' && (
                  <button
                    className="text-button"
                    disabled={busy}
                    onClick={() => {
                      start.reset();
                      retry.mutate();
                    }}
                  >
                    <RefreshCw size={15} />
                    {retry.isPending ? 'Retrying…' : 'Retry failed candidates'}
                  </button>
                )}
              </details>
              {active.blocked.length > 0 && (
                <details className="blocked-details">
                  <summary>{active.blocked.length} candidates excluded from this run</summary>
                  <BlockedList items={active.blocked} />
                </details>
              )}
              <div className="results-toolbar">
                <h3>
                  Candidates <span className="count">{candidates.data?.total ?? '—'}</span>
                </h3>
                <div className="results-toolbar-actions">
                  {filterCount > 0 && (
                    <button className="text-button" onClick={clearFilters}>
                      <X size={14} />
                      Clear filters
                    </button>
                  )}
                  <button
                    className="button secondary compact"
                    aria-expanded={filtersOpen}
                    aria-controls="candidate-filters"
                    onClick={() => setFiltersOpen(!filtersOpen)}
                  >
                    <SlidersHorizontal size={15} />
                    Filters{filterCount > 0 && <span className="count">{filterCount}</span>}
                  </button>
                  <button
                    className="icon-button"
                    aria-label="Refresh results"
                    onClick={() => {
                      void runs.refetch();
                      void candidates.refetch();
                    }}
                  >
                    <RefreshCw size={17} />
                  </button>
                </div>
              </div>
              {filtersOpen && (
                <div className="candidate-filters" id="candidate-filters">
                  <div>
                    <label>
                      Eligibility
                      <select
                        aria-label="Eligibility"
                        value={eligible}
                        onChange={(event) => {
                          setEligible(event.target.value);
                          setOffset(0);
                        }}
                      >
                        <option value="all">All candidates</option>
                        <option value="true">Eligible</option>
                        <option value="false">Ineligible</option>
                      </select>
                    </label>
                    <label>
                      Stage
                      <select
                        aria-label="Stage"
                        value={stage}
                        onChange={(event) => {
                          setStage(event.target.value);
                          setOffset(0);
                        }}
                      >
                        <option value="all">All stages</option>
                        {['applied', 'screened', 'shortlisted', 'review', 'rejected'].map((value) => (
                          <option key={value} value={value}>
                            {value[0].toUpperCase() + value.slice(1)}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      Minimum score
                      <input
                        type="number"
                        min={0}
                        max={100}
                        value={minimum}
                        onChange={(event) => {
                          setMinimum(event.target.value);
                          setOffset(0);
                        }}
                        placeholder="0–100"
                      />
                    </label>
                  </div>
                </div>
              )}
              {!validMinimum && (
                <div className="notice error" role="alert">
                  Enter a minimum score between 0 and 100.
                </div>
              )}
              <div className="compare-toolbar">
                <span>Select 2–5 candidates to compare · {selected.length} selected</span>
                <div>
                  <button
                    className="button secondary compact"
                    disabled={selected.length < 2}
                    onClick={() => setCompareRun(active.id)}
                  >
                    Compare selected
                  </button>
                  {selected.length > 0 && (
                    <button
                      className="text-button"
                      onClick={() => {
                        setSelection({ runId: active.id, ids: [] });
                        setCompareRun(null);
                      }}
                    >
                      Clear selection
                    </button>
                  )}
                </div>
              </div>
              {compareRun === active.id && selected.length >= 2 && (
                <ComparePanel jobId={jobId} runId={active.id} candidateIds={selected} />
              )}
              <ErrorNotice
                error={candidates.error}
                retry={() => {
                  void candidates.refetch();
                }}
              />
              {candidates.isPending && validMinimum ? (
                <Spinner text="Loading candidate results…" />
              ) : (
                candidates.data && (
                  <>
                    {!candidates.data.items.length ? (
                      <Empty
                        title={
                          active.status === 'completed'
                            ? 'No candidates match these filters'
                            : 'Results will appear here'
                        }
                      >
                        {active.status === 'completed'
                          ? 'Adjust the filters to see more candidates.'
                          : 'Candidates appear as their screening finishes.'}
                      </Empty>
                    ) : (
                      <div
                        className="table-scroll"
                        tabIndex={0}
                        role="region"
                        aria-label="Candidate rankings"
                      >
                        <table className="candidate-table">
                          <thead>
                            <tr>
                              <th scope="col">Compare</th>
                              <th>Rank</th>
                              <th>Candidate</th>
                              <th>Score</th>
                              <th>Eligibility</th>
                              <th>Stage</th>
                              <th>Review</th>
                            </tr>
                          </thead>
                          <tbody>
                            {candidates.data.items.map((candidate) => (
                              <tr key={candidate.application_id}>
                                <td>
                                  <input
                                    type="checkbox"
                                    aria-label={`Select candidate ${shortId(candidate.candidate_id)} for comparison`}
                                    checked={selected.includes(candidate.candidate_id)}
                                    disabled={
                                      !selected.includes(candidate.candidate_id) && selected.length >= 5
                                    }
                                    onChange={(event) =>
                                      setSelection({
                                        runId: active.id,
                                        ids: event.target.checked
                                          ? [...selected, candidate.candidate_id]
                                          : selected.filter((id) => id !== candidate.candidate_id),
                                      })
                                    }
                                  />
                                </td>
                                <td>
                                  <span className="rank">{candidate.rank ?? '—'}</span>
                                </td>
                                <td>
                                  <div className="candidate-identity">
                                    <span className="candidate-avatar" aria-hidden="true">
                                      <UserRound size={18} />
                                    </span>
                                    <div>
                                      <Link
                                        className="candidate-name"
                                        to={`/candidates/${candidate.candidate_id}?job=${jobId}&run=${active.id}`}
                                      >
                                        <CandidateName id={candidate.candidate_id} />
                                      </Link>
                                      <span className="candidate-subline">
                                        {candidate.status === 'needs_reprocess'
                                          ? candidate.reprocess_reason || 'Needs reprocessing'
                                          : candidate.explanation?.strengths?.[0]?.text ||
                                            'Open profile to review evidence'}
                                      </span>
                                      {candidate.knockout_reasons.length > 0 && (
                                        <span className="candidate-warning">
                                          {candidate.knockout_reasons.join(' · ')}
                                        </span>
                                      )}
                                    </div>
                                  </div>
                                </td>
                                <td>
                                  <span className={`score ${scoreTier(candidate.score, candidate.eligible)}`}>
                                    {candidate.score === null ? '—' : candidate.score.toFixed(1)}
                                  </span>
                                  {candidate.score !== null && (
                                    <span className="score-denominator">/100</span>
                                  )}
                                  {candidate.score !== null && (
                                    <div
                                      className={`score-track ${scoreTier(candidate.score, candidate.eligible)}`}
                                      aria-hidden="true"
                                    >
                                      <span
                                        style={{ width: `${Math.max(0, Math.min(100, candidate.score))}%` }}
                                      />
                                    </div>
                                  )}
                                </td>
                                <td>
                                  <Badge
                                    value={
                                      candidate.eligible === null
                                        ? candidate.status
                                        : candidate.eligible
                                          ? 'eligible'
                                          : 'ineligible'
                                    }
                                  />
                                </td>
                                <td>
                                  <Badge value={candidate.stage} />
                                </td>
                                <td>
                                  <Link
                                    className="review-link"
                                    aria-label={`Review candidate ${shortId(candidate.candidate_id)}`}
                                    to={`/candidates/${candidate.candidate_id}?job=${jobId}&run=${active.id}`}
                                  >
                                    <span>Review</span>
                                    <ArrowRight size={15} />
                                  </Link>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                    <div className="pagination">
                      <span>
                        {candidates.data.total
                          ? `${offset + 1}–${Math.min(offset + candidates.data.items.length, candidates.data.total)} of ${candidates.data.total} candidates`
                          : '0 candidates'}
                      </span>
                      <div>
                        <button
                          className="button secondary compact"
                          disabled={offset === 0}
                          onClick={() => setOffset(Math.max(0, offset - 20))}
                        >
                          <ChevronLeft size={16} />
                          Previous
                        </button>
                        <button
                          className="button secondary compact"
                          disabled={offset + 20 >= candidates.data.total}
                          onClick={() => setOffset(offset + 20)}
                        >
                          Next
                          <ChevronRight size={16} />
                        </button>
                      </div>
                    </div>
                  </>
                )
              )}
              <RunRoster
                key={active.id}
                run={active}
                selected={selected}
                onSelect={(id, checked) =>
                  setSelection({
                    runId: active.id,
                    ids: checked ? [...selected, id] : selected.filter((value) => value !== id),
                  })
                }
              />
            </>
          )}
        </>
      )}
      {choosingSource && (
        <ScreeningSourceDialog
          jobId={jobId}
          initialSource={
            ['database', 'uploads', 'both'].includes(params.get('source') || '')
              ? (params.get('source') as ResumeSource)
              : null
          }
          busy={start.isPending}
          error={start.error}
          onClose={() => {
            if (!start.isPending) setChoosingSource(false);
          }}
          onUpload={(source) => {
            setChoosingSource(false);
            setParams((previous) => {
              const next = new URLSearchParams(previous);
              next.set('tab', 'resumes');
              next.set('source', source);
              return next;
            });
          }}
          onStart={(source) => start.mutate(source)}
        />
      )}
    </section>
  );
}
function BlockedList({ items }: { items: { reason?: string; candidate_id?: string }[] }) {
  return (
    <ul className="blocked-list">
      {items.map((item, index) => (
        <li key={index}>
          {item.candidate_id && <strong>Candidate {shortId(item.candidate_id)}: </strong>}
          {item.reason || 'Not ready for screening'}
        </li>
      ))}
    </ul>
  );
}
