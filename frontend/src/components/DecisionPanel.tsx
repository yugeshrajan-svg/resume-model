import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import type { DecisionChoice, DecisionHistory } from '../api/types';
import { label, shortId, timestamp } from '../lib/format';
import { Badge, ErrorNotice, Spinner } from './ui';
import JobTitle from './JobTitle';

export default function DecisionPanel({
  applicationId,
  jobId,
  candidateId,
  stage,
}: {
  applicationId: string;
  jobId: string;
  candidateId: string;
  stage: string;
}) {
  const client = useQueryClient();
  const [decision, setDecision] = useState<DecisionChoice>('review');
  const [reason, setReason] = useState('');
  const [saved, setSaved] = useState(false);
  const history = useQuery({
    queryKey: ['decisions', applicationId],
    queryFn: ({ signal }) =>
      api.request<DecisionHistory>(`/applications/${applicationId}/decisions`, { signal }),
  });
  const save = useMutation({
    mutationFn: () =>
      api.request(`/applications/${applicationId}/decision`, {
        method: 'POST',
        body: { decision, reason: reason.trim() || null },
      }),
    onSuccess: async () => {
      setReason('');
      setSaved(true);
      await Promise.all([
        client.invalidateQueries({ queryKey: ['decisions', applicationId] }),
        client.invalidateQueries({ queryKey: ['candidate', candidateId] }),
        client.invalidateQueries({ queryKey: ['ranked', jobId] }),
      ]);
    },
  });
  return (
    <section className="panel decision-panel" aria-label={`Decisions for job ${shortId(jobId)}`}>
      <div className="section-heading">
        <div>
          <h2>Hiring decision</h2>
          <p>
            <Link to={`/jobs/${jobId}?tab=results`}>
              <JobTitle id={jobId} />
            </Link>
          </p>
        </div>
        <Badge value={history.data?.stage || stage} />
      </div>
      <form
        className="hr-form"
        onSubmit={(event) => {
          event.preventDefault();
          setSaved(false);
          save.mutate();
        }}
      >
        <label>
          Decision
          <select
            value={decision}
            disabled={save.isPending}
            onChange={(event) => {
              setDecision(event.target.value as DecisionChoice);
              setSaved(false);
            }}
          >
            <option value="review">Further review</option>
            <option value="shortlist">Shortlist</option>
            <option value="reject">Reject</option>
          </select>
        </label>
        <label>
          Decision reason <span className="field-hint">(optional)</span>
          <textarea
            rows={3}
            maxLength={2000}
            value={reason}
            disabled={save.isPending}
            onChange={(event) => {
              setReason(event.target.value);
              setSaved(false);
            }}
          />
        </label>
        <p className="field-hint">
          Every decision records your reviewer account, time, reason, and stage change.
        </p>
        <ErrorNotice error={save.error} />
        {saved && (
          <p role="status" className="success-message">
            Decision recorded.
          </p>
        )}
        <button className="button primary" disabled={save.isPending}>
          {save.isPending ? 'Recording…' : 'Record decision'}
        </button>
      </form>
      <h3>Decision history</h3>
      <ErrorNotice
        error={history.error}
        retry={() => {
          void history.refetch();
        }}
      />
      {history.isPending ? (
        <Spinner text="Loading decisions…" />
      ) : (
        history.data &&
        (history.data.decisions.length ? (
          <ol className="history-list">
            {history.data.decisions.map((item) => (
              <li key={item.decision_id}>
                <div>
                  <strong>{label(item.decision)}</strong>
                  <Badge value={item.new_stage} />
                </div>
                <p>
                  {item.previous_stage ? label(item.previous_stage) : 'Earlier stage unavailable'} →{' '}
                  {label(item.new_stage)}
                </p>
                {item.reason && <p className="history-reason">{item.reason}</p>}
                <small>
                  {item.reviewer
                    ? `${item.reviewer.email || shortId(item.reviewer.user_id)} (${label(item.reviewer.role)})`
                    : 'Reviewer unavailable'}{' '}
                  · {timestamp(item.decided_at)}
                </small>
              </li>
            ))}
          </ol>
        ) : (
          <p className="muted">No decisions recorded yet.</p>
        ))
      )}
    </section>
  );
}
