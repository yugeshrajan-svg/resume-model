import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import type { RecruitingActivity } from '../api/recruiting';
import { label, timestamp } from '../lib/format';
import { Badge, Empty, ErrorNotice, Spinner } from './ui';

export default function CandidateActivityFeed({
  candidateId,
  canManage,
}: {
  candidateId: string;
  canManage: boolean;
}) {
  const [kind, setKind] = useState('');
  const [offset, setOffset] = useState(0);
  const records = useQuery({
    queryKey: ['recruiting-activities', 'profile', candidateId, kind, offset],
    queryFn: ({ signal }) =>
      api.request<{ total: number; items: RecruitingActivity[] }>(
        `/recruiting/activities?${new URLSearchParams({ candidate_id: candidateId, offset: String(offset), ...(kind ? { kind } : {}) })}`,
        { signal },
      ),
  });
  return (
    <section className="panel" aria-label="Candidate recruiting history">
      <div className="section-heading">
        <div>
          <h2>Recruiting history</h2>
          <p>
            Recruiting records across this candidate’s jobs. Stage changes appear in each application’s
            decision history.
          </p>
        </div>
        <button
          className="button secondary compact"
          disabled={records.isFetching}
          onClick={() => void records.refetch()}
        >
          Refresh history
        </button>
      </div>
      <label>
        Activity type
        <select
          value={kind}
          onChange={(event) => {
            setKind(event.target.value);
            setOffset(0);
          }}
        >
          <option value="">All activities</option>
          {['interview', 'feedback', 'task', 'note', ...(canManage ? ['offer'] : [])].map((value) => (
            <option key={value} value={value}>
              {label(value)}
            </option>
          ))}
        </select>
      </label>
      <ErrorNotice error={records.error} retry={() => void records.refetch()} />
      {records.isPending && <Spinner text="Loading recruiting history…" />}
      {records.data?.total === 0 && (
        <Empty title="No recruiting activity recorded yet">
          Use the Recruiting activities actions on this profile to record the next step.
        </Empty>
      )}
      <div className="library-list">
        {records.data?.items
          .filter((item) => canManage || item.kind !== 'offer')
          .map((item) => (
            <article className="ats-activity" key={item.id}>
              <div className="section-heading">
                <h3>{item.title}</h3>
                <Badge value={item.status} />
              </div>
              <p className="field-hint">
                {label(item.kind)} · Recorded {timestamp(item.created_at)}
                {item.updated_at !== item.created_at && ` · Updated ${timestamp(item.updated_at)}`}
              </p>
              <p>
                {item.job_id ? (
                  <Link to={`/jobs/${item.job_id}`}>{item.job_title || 'View linked job'}</Link>
                ) : (
                  'No job linked'
                )}
              </p>
              {item.due_at && (
                <p>
                  {item.kind === 'interview' ? 'Interview' : 'Due'}: {timestamp(item.due_at)}
                  {item.end_at && ` → ${timestamp(item.end_at)}`}
                </p>
              )}
              {item.details.location && <p>Location / meeting: {item.details.location}</p>}
              {item.details.round_name && (
                <p>
                  {item.details.round_name} · Plan version {item.details.plan_version}
                </p>
              )}
              {item.kind === 'feedback' && item.details.ratings && (
                <p>
                  {Object.entries(item.details.ratings)
                    .map(([criterion, rating]) => `${criterion}: ${rating}/5`)
                    .join(' · ')}
                </p>
              )}
              {item.kind === 'feedback' && (
                <p>
                  Skills {item.details.technical}/5 · Communication {item.details.communication}/5 ·
                  Experience {item.details.experience}/5 · Recommendation:{' '}
                  {label(item.details.recommendation || '')}
                </p>
              )}
              {item.kind === 'offer' && canManage && (
                <p>
                  {item.details.currency} {item.details.amount} / {item.details.pay_period} · Intended start:{' '}
                  {item.details.start_date}
                </p>
              )}
              {item.notes && <p className="ats-notes">{item.notes}</p>}
              <Link
                className="text-button"
                to={`/recruiting?tab=${item.kind}&candidate=${candidateId}${item.job_id ? `&job=${item.job_id}` : ''}`}
              >
                Open {label(item.kind).toLowerCase()} records
              </Link>
            </article>
          ))}
      </div>
      {records.data && records.data.total > 50 && (
        <div className="upload-pagination">
          <span>
            {offset + 1}–{Math.min(offset + 50, records.data.total)} of {records.data.total}
          </span>
          <button className="button secondary" disabled={!offset} onClick={() => setOffset(offset - 50)}>
            Previous activities
          </button>
          <button
            className="button secondary"
            disabled={offset + 50 >= records.data.total}
            onClick={() => setOffset(offset + 50)}
          >
            Next activities
          </button>
        </div>
      )}
    </section>
  );
}
