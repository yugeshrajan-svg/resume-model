import { useState } from 'react';
import type { FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowRight, BriefcaseBusiness, CalendarDays, CircleHelp, Plus, Search, X } from 'lucide-react';
import { api } from '../api/client';
import type { Job } from '../api/types';
import { useSession } from '../hooks/useSession';
import { date } from '../lib/format';
import { Empty, ErrorNotice, Spinner } from '../components/ui';
import ConfirmDialog from '../components/ConfirmDialog';

export default function JobsPage() {
  const current = useSession()!;
  const canWrite = current.role !== 'hiring_manager';
  const navigate = useNavigate();
  const client = useQueryClient();
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState('newest');
  const [weekStart] = useState(() => Date.now() - 7 * 86400000);
  const [creating, setCreating] = useState(false);
  const jobs = useQuery({
    queryKey: ['jobs'],
    queryFn: ({ signal }) => api.request<Job[]>('/jobs', { signal }),
  });
  const create = useMutation({
    mutationFn: (body: { title: string; jd_text: string }) =>
      api.request<Job>('/jobs', { method: 'POST', body }),
    onSuccess: (job) => {
      void client.invalidateQueries({ queryKey: ['jobs'] });
      navigate(`/jobs/${job.id}`);
    },
  });
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (create.isPending) return;
    const form = new FormData(event.currentTarget);
    create.mutate({
      title: String(form.get('title')).trim(),
      jd_text: String(form.get('description')).trim(),
    });
  }
  const filtered = [...(jobs.data || [])]
    .filter((job) => job.title.toLowerCase().includes(search.toLowerCase()))
    .sort((a, b) =>
      sort === 'name' ? a.title.localeCompare(b.title) : Date.parse(b.created_at) - Date.parse(a.created_at),
    );
  const recent = jobs.data?.filter((job) => Date.parse(job.created_at) >= weekStart).length;
  const openCreate = () => {
    create.reset();
    setCreating(true);
  };
  return (
    <div className="page jobs-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Hiring</span>
          <h1>
            Your jobs<span className="heading-dot">.</span>
          </h1>
          <p>Open a job to set its requirements, add resumes and review the ranked shortlist.</p>
        </div>
        {canWrite && (
          <button className="button primary" onClick={openCreate}>
            <Plus size={18} />
            Create job
          </button>
        )}
      </div>
      {creating && (
        <ConfirmDialog
          title="Create a job"
          initialFocus='[name="title"]'
          onClose={() => {
            if (!create.isPending) setCreating(false);
          }}
        >
          <div className="create-dialog-intro">
            <p>Tell us about the role. You can review the requirements next.</p>
            <button
              className="icon-button"
              aria-label="Close new job form"
              onClick={() => setCreating(false)}
              disabled={create.isPending}
            >
              <X size={20} />
            </button>
          </div>
          <form onSubmit={submit}>
            <fieldset disabled={create.isPending}>
              <label>
                Job title
                <input
                  name="title"
                  required
                  minLength={2}
                  maxLength={300}
                  placeholder="e.g. Senior backend engineer"
                />
              </label>
              <label>
                Job description
                <textarea
                  name="description"
                  required
                  minLength={20}
                  maxLength={50_000}
                  rows={8}
                  placeholder="Include responsibilities, required skills, experience and location…"
                />
              </label>
              <ErrorNotice error={create.error} />
              <div className="form-actions">
                <button className="button secondary" type="button" onClick={() => setCreating(false)}>
                  Cancel
                </button>
                <button className="button primary" type="submit">
                  {create.isPending ? 'Creating…' : 'Create and review requirements'}
                  <ArrowRight size={16} />
                </button>
              </div>
            </fieldset>
          </form>
        </ConfirmDialog>
      )}
      <div className="stat-grid" aria-label="Workspace summary">
        <div className="stat-card">
          <span className="stat-icon">
            <BriefcaseBusiness size={18} />
          </span>
          <div>
            <strong>{jobs.data?.length ?? '—'}</strong>
            <span>Jobs in your workspace</span>
          </div>
        </div>
        <div className="stat-card">
          <span className="stat-icon">
            <CalendarDays size={18} />
          </span>
          <div>
            <strong>{recent ?? '—'}</strong>
            <span>Added in the last 7 days</span>
          </div>
        </div>
        <Link className="stat-card stat-link" to="/guide">
          <span className="stat-icon">
            <CircleHelp size={18} />
          </span>
          <div>
            <strong>3 steps</strong>
            <span>Requirements → Resumes → Shortlist</span>
          </div>
          <ArrowRight size={16} />
        </Link>
      </div>
      <section className="panel jobs-panel">
        <div className="list-toolbar">
          <h2>
            All jobs <span className="count">{filtered.length}</span>
          </h2>
          <div className="job-list-controls">
            <label className="search-box">
              <Search size={17} />
              <input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                aria-label="Search jobs"
                placeholder="Search jobs…"
              />
              {search && (
                <button
                  type="button"
                  className="icon-button clear-search"
                  aria-label="Clear job search"
                  onClick={() => setSearch('')}
                >
                  <X size={15} />
                </button>
              )}
            </label>
            <select
              className="job-sort"
              aria-label="Sort jobs"
              value={sort}
              onChange={(event) => setSort(event.target.value)}
            >
              <option value="newest">Newest first</option>
              <option value="name">Job title A–Z</option>
            </select>
          </div>
        </div>
        <ErrorNotice
          error={jobs.error}
          retry={() => {
            void jobs.refetch();
          }}
        />
        {jobs.isPending ? (
          <Spinner text="Loading your jobs…" />
        ) : jobs.error && !jobs.data ? null : !filtered.length ? (
          <Empty
            title={search ? 'No matching jobs' : 'Your next hire starts here'}
            action={
              !search && canWrite ? (
                <button className="button primary" onClick={openCreate}>
                  <Plus size={16} />
                  Create your first job
                </button>
              ) : undefined
            }
          >
            {search
              ? 'Try searching for another title.'
              : 'Create a job, review its requirements, and bring in your candidates.'}
          </Empty>
        ) : (
          <div className="job-list">
            {filtered.map((job, index) => (
              <Link className="job-row" key={job.id} to={`/jobs/${job.id}`}>
                <div className={`job-avatar tone-${index % 3}`}>
                  <BriefcaseBusiness size={22} />
                </div>
                <div className="job-row-main">
                  <h3>{job.title}</h3>
                  <p className="job-description-preview">
                    {job.jd_text || 'Open this job to review its requirements and candidates.'}
                  </p>
                  <span className="job-card-date">
                    <CalendarDays size={13} /> Created {date(job.created_at)}
                  </span>
                </div>
                <span className="job-row-action">
                  Open job
                  <ArrowRight size={17} />
                </span>
              </Link>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
