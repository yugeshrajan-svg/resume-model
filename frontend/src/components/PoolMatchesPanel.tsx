import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, RefreshCw, UserPlus, Users } from 'lucide-react';
import { api } from '../api/client';
import type { PoolMatch, PoolMatches } from '../api/types';
import { useSession } from '../hooks/useSession';
import { label, shortId } from '../lib/format';
import { ErrorNotice, Spinner } from './ui';

/** Eligible talent-pool candidates for the job's confirmed requirements version (suggestions for HR). */
export default function PoolMatchesPanel({ jobId }: { jobId: string }) {
  const role = useSession()!.role;
  const canAdd = role !== 'hiring_manager';
  const client = useQueryClient();
  const [showIneligible, setShowIneligible] = useState(false);
  const matches = useQuery({
    queryKey: ['pool-matches', jobId],
    queryFn: ({ signal }) => api.request<PoolMatches>(`/jobs/${jobId}/pool-matches`, { signal }),
    refetchInterval: (query) => ((query.state.data?.progress.pending ?? 0) > 0 ? 2500 : false),
  });
  const refresh = useMutation({
    mutationFn: () => api.request(`/jobs/${jobId}/pool-matches/refresh`, { method: 'POST' }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['pool-matches', jobId] });
    },
  });
  const add = useMutation({
    mutationFn: (ids: string[]) =>
      api.request<{
        added: string[];
        already_applied: string[];
        failed: { candidate_id: string; error: string }[];
      }>(`/jobs/${jobId}/applications/bulk`, { method: 'POST', body: { candidate_ids: ids } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['pool-matches', jobId] });
      void client.invalidateQueries({ queryKey: ['runs', jobId] });
    },
  });
  const data = matches.data;
  const waiting = data?.eligible.filter((match) => !match.in_job) || [];
  return (
    <section className="panel pool-panel" aria-label="Eligible from your talent pool">
      <div className="section-heading">
        <div>
          <h2>
            Eligible from your talent pool {data && <span className="count">{data.eligible.length}</span>}
          </h2>
          <p>
            Candidates who agreed to be considered for future roles, checked against
            {data?.requirements_version
              ? ` requirements v${data.requirements_version}`
              : ' the confirmed requirements'}{' '}
            with the same eligibility rules as screening.
          </p>
        </div>
        <div className="button-row">
          {canAdd && waiting.length > 0 && (
            <button
              className="button primary compact"
              disabled={add.isPending}
              onClick={() => add.mutate(waiting.map((match) => match.candidate_id))}
            >
              <UserPlus size={15} />
              {add.isPending ? 'Adding…' : `Add all ${waiting.length} eligible`}
            </button>
          )}
          {role !== 'hiring_manager' && data?.confirmed && (
            <button
              className="button secondary compact"
              disabled={refresh.isPending}
              onClick={() => refresh.mutate()}
              title="Check candidates who joined the talent pool since"
            >
              <RefreshCw size={15} />
              Refresh
            </button>
          )}
        </div>
      </div>
      <ErrorNotice
        error={matches.error || refresh.error || add.error}
        retry={
          matches.error
            ? () => {
                void matches.refetch();
              }
            : undefined
        }
      />
      {add.data && (
        <p className="success-message" role="status">
          {add.data.added.length} added to this job
          {add.data.failed.length ? ` · ${add.data.failed.length} could not be added` : ''}. Start a screening
          to score them.
        </p>
      )}
      {matches.isPending ? (
        <Spinner text="Checking your talent pool…" />
      ) : (
        data &&
        (!data.confirmed ? (
          <p className="muted">Confirm the requirements to see eligible candidates from your talent pool.</p>
        ) : !data.pool_size ? (
          <p className="muted">
            <Users size={15} /> No candidates are in your talent pool yet. When uploading, select “They also
            agreed to be considered for future roles”.
          </p>
        ) : (
          <>
            {data.progress.pending > 0 && (
              <div className="pool-progress" role="status">
                <span>
                  Checked {data.progress.checked} of {data.progress.total} pool candidates…
                </span>
                <progress max={Math.max(1, data.progress.total)} value={data.progress.checked} />
              </div>
            )}
            {!data.eligible.length && !data.progress.pending ? (
              <p className="muted">
                None of the {data.progress.total} pool candidates checked meets every requirement of this
                version.
              </p>
            ) : (
              <ul className="pool-list">
                {data.eligible.map((match) => (
                  <PoolRow
                    key={match.match_id}
                    match={match}
                    canAdd={canAdd}
                    adding={add.isPending}
                    onAdd={() => add.mutate([match.candidate_id])}
                  />
                ))}
              </ul>
            )}
            {(data.ineligible.length > 0 || data.failed.length > 0) && (
              <button className="text-button" onClick={() => setShowIneligible(!showIneligible)}>
                {showIneligible ? 'Hide' : 'Show'} {data.ineligible.length} not eligible
                {data.failed.length ? ` and ${data.failed.length} unreadable` : ''}
              </button>
            )}
            {showIneligible && (
              <ul className="pool-list muted-list">
                {[...data.ineligible, ...data.failed].map((match) => (
                  <PoolRow
                    key={match.match_id}
                    match={match}
                    canAdd={false}
                    adding={false}
                    onAdd={() => {}}
                  />
                ))}
              </ul>
            )}
          </>
        ))
      )}
    </section>
  );
}
function PoolRow({
  match,
  canAdd,
  adding,
  onAdd,
}: {
  match: PoolMatch;
  canAdd: boolean;
  adding: boolean;
  onAdd: () => void;
}) {
  const name = match.name || `Candidate ${shortId(match.candidate_id)}`;
  const fit =
    match.similarity === null ? null : Math.max(0, Math.min(100, Math.round(match.similarity * 100)));
  return (
    <li className="pool-row">
      <div className="pool-main">
        <Link className="candidate-name" to={`/candidates/${match.candidate_id}`}>
          {name}
        </Link>
        {match.eligible ? (
          <span className="candidate-subline">
            Meets every requirement{match.warnings.length ? ` · ${match.warnings.join(' · ')}` : ''}
          </span>
        ) : (
          <span className="candidate-warning">
            {match.failure_reason || match.reasons.join(' · ') || 'Not checked yet'}
          </span>
        )}
      </div>
      {fit !== null && (
        <span className="pool-fit" title="Semantic closeness to the job description, not a screening score">
          {fit}% match
        </span>
      )}
      {match.in_job ? (
        <span className="badge green">
          <Check size={11} />
          In job · {label(match.in_job)}
        </span>
      ) : (
        canAdd && (
          <button className="button secondary compact" disabled={adding} onClick={onAdd}>
            <UserPlus size={14} />
            Add
          </button>
        )
      )}
    </li>
  );
}
