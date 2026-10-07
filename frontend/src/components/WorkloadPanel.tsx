import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import type { RecruitingActivity } from '../api/recruiting';
import { label, timestamp } from '../lib/format';
import { Empty, ErrorNotice, Spinner } from './ui';
export default function WorkloadPanel() {
  const [mine, setMine] = useState(true);
  const data = useQuery({
    queryKey: ['recruiting-workload', mine],
    queryFn: ({ signal }) =>
      api.request<{
        by_owner: { owner_id: string | null; kind: string; count: number }[];
        items: (RecruitingActivity & { overdue: boolean; due_soon: boolean })[];
      }>('/recruiting/workload?mine=' + mine, { signal }),
    refetchInterval: 30000,
  });
  const team = useQuery({
    queryKey: ['recruiting-team'],
    queryFn: ({ signal }) => api.request<{ id: string; label: string }[]>('/recruiting/team', { signal }),
  });
  return (
    <section className="panel">
      <div className="section-heading">
        <div>
          <h2>Follow-ups and workload</h2>
          <p>
            Open tasks and interviews, with deadlines and owner totals. Reminders appear here while the app is
            open.
          </p>
        </div>
        <button className="button secondary compact" onClick={() => void data.refetch()}>
          Refresh workload
        </button>
      </div>
      <div className="inline-actions">
        <button
          className={'button compact ' + (mine ? 'primary' : 'secondary')}
          onClick={() => setMine(true)}
        >
          My work
        </button>
        <button
          className={'button compact ' + (!mine ? 'primary' : 'secondary')}
          onClick={() => setMine(false)}
        >
          Team workload
        </button>
      </div>
      <ErrorNotice error={data.error || team.error} retry={() => void data.refetch()} />
      {data.isPending && <Spinner text="Loading follow-ups…" />}
      {data.data?.items.length === 0 && (
        <Empty title="No open follow-ups">Assigned tasks and interviews appear here.</Empty>
      )}
      <div className="library-summary">
        {data.data?.by_owner.map((row) => (
          <span className="subtle-chip" key={(row.owner_id || 'none') + row.kind}>
            {team.data?.find((user) => user.id === row.owner_id)?.label || 'Unassigned'} · {label(row.kind)}{' '}
            {row.count}
          </span>
        ))}
      </div>
      <div className="library-list">
        {data.data?.items.map((row) => (
          <article className="ats-activity" key={row.id}>
            <h3>{row.title}</h3>
            <p>
              {row.due_at ? timestamp(row.due_at) : 'No deadline'}
              {row.overdue && <span className="subtle-chip">Overdue</span>}
              {row.due_soon && <span className="subtle-chip">Due within 7 days</span>}
            </p>
            <Link
              className="text-button"
              to={
                '/recruiting?tab=' +
                row.kind +
                (row.candidate_id ? '&candidate=' + row.candidate_id : '') +
                (row.job_id ? '&job=' + row.job_id : '')
              }
            >
              Open {label(row.kind).toLowerCase()} records
            </Link>
          </article>
        ))}
      </div>
      {data.data && data.data.items.length === 50 && (
        <p className="field-hint">
          Showing the next 50 records by deadline. Use Tasks or Interviews to search and page through every
          record.
        </p>
      )}
    </section>
  );
}
