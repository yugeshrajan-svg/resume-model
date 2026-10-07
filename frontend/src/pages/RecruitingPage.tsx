import { useState } from 'react';
import type { FormEvent } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CalendarDays, Plus, Download } from 'lucide-react';
import { api } from '../api/client';
import type { Job } from '../api/types';
import type { RecruitingActivity as Activity } from '../api/recruiting';
import { useSession } from '../hooks/useSession';
import { label, shortId, timestamp } from '../lib/format';
import { Badge, Empty, ErrorNotice, Spinner } from '../components/ui';
import ConfirmDialog from '../components/ConfirmDialog';
import HiringPlanEditor from '../components/HiringPlanEditor';
import WorkloadPanel from '../components/WorkloadPanel';
import DuplicateReviewPanel from '../components/DuplicateReviewPanel';
import type { HiringPlan } from '../components/HiringPlanEditor';

const hiringStages = [
  'applied',
  'screened',
  'review',
  'shortlisted',
  'interview',
  'offer',
  'hired',
  'rejected',
  'withdrawn',
];
type Application = {
  id: string;
  candidate_id: string;
  name: string | null;
  stage: string;
  created_at: string;
};
type Pipeline = { total: number; counts: Record<string, number>; items: Application[] };
type Report = {
  monthly_cohorts?: {
    month: string;
    applications: number;
    interview: number;
    offer: number;
    hired: number;
  }[];
  sources?: Record<string, number>;
  reached_stages?: Record<string, number>;
  stages: Record<string, number>;
  applications: number;
  jobs: number;
  open_tasks: number;
  open_interviews: number;
  open_offers: number | null;
  overdue_tasks: number;
  hires_last_30_days: number;
  average_days_to_hire: number | null;
  by_job: { id: string; title: string; stages: Record<string, number> }[];
};
const sections = [
  ['overview', 'Overview'],
  ['pipeline', 'Hiring pipeline'],
  ['interview', 'Interviews'],
  ['feedback', 'Feedback'],
  ['task', 'Tasks'],
  ['offer', 'Offers'],
  ['note', 'Notes'],
  ['reports', 'Reports'],
  ['setup', 'Hiring setup'],
  ['duplicates', 'Duplicate review'],
];
const kindTitles: Record<string, string> = {
  task: 'Recruiter task',
  interview: 'Interview',
  feedback: 'Interview feedback',
  offer: 'Offer record',
  note: 'Candidate note',
};

function saveFile(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

function localDateTime(value: string | null | undefined) {
  if (!value) return '';
  const date = new Date(value);
  const pad = (part: number) => String(part).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export default function RecruitingPage() {
  const role = useSession()!.role;
  const canManage = role !== 'hiring_manager';
  const [params, setParams] = useSearchParams();
  const requested = params.get('tab') || 'overview';
  const tab =
    sections.some(([id]) => id === requested) && (canManage || requested !== 'offer')
      ? requested
      : 'overview';
  const candidateId = params.get('candidate') || '';
  const jobId = params.get('job') || '';
  function setJobId(value: string) {
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      if (value) next.set('job', value);
      else next.delete('job');
      return next;
    });
  }
  const [referenceTime] = useState(() => Date.now());
  const [stage, setStage] = useState('');
  const [pipelineSearch, setPipelineSearch] = useState('');
  const [candidateSearch, setCandidateSearch] = useState('');
  const [candidateOffset, setCandidateOffset] = useState(0);
  const [offset, setOffset] = useState(0);
  const [activityOffset, setActivityOffset] = useState(0);
  const [activitySearch, setActivitySearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [ownerFilter, setOwnerFilter] = useState('');
  const [overdueOnly, setOverdueOnly] = useState(false);
  const [adding, setAdding] = useState(params.get('new') === '1');
  const [editing, setEditing] = useState<Activity | null>(null);
  const [moving, setMoving] = useState<Application | null>(null);
  const [notice, setNotice] = useState('');
  const [reportFrom, setReportFrom] = useState('');
  const [reportTo, setReportTo] = useState('');
  const [roundId, setRoundId] = useState('');
  const [letterOffer, setLetterOffer] = useState<Activity | null>(null);
  const [templateId, setTemplateId] = useState('');
  const [templateOffset, setTemplateOffset] = useState(0);
  const client = useQueryClient();
  const jobs = useQuery({
    queryKey: ['jobs'],
    queryFn: ({ signal }) => api.request<Job[]>('/jobs', { signal }),
  });
  const hiringPlan = useQuery({
    queryKey: ['hiring-plan', jobId],
    queryFn: ({ signal }) => api.request<HiringPlan>('/jobs/' + jobId + '/hiring-plan', { signal }),
    enabled: adding && !!jobId && ['interview', 'feedback'].includes(tab),
  });
  const selectedRound = hiringPlan.data?.rounds.find((row) => row.id === roundId);
  const templates = useQuery({
    queryKey: ['hr-records', 'letter-templates', templateOffset],
    queryFn: ({ signal }) =>
      api.request<{ total: number; items: { id: string; title: string; data: { content: string } }[] }>(
        '/hr/records?' +
          new URLSearchParams({ kind: 'offer_template', status: 'open', offset: String(templateOffset) }),
        { signal },
      ),
    enabled: !!letterOffer,
  });
  const generateLetter = useMutation({
    mutationFn: async (reviewed: boolean) =>
      saveFile(
        await api.request<Blob>('/recruiting/offers/' + letterOffer!.id + '/letter', {
          method: 'POST',
          blob: true,
          body: { template_id: templateId, reviewed, expected_updated_at: letterOffer!.updated_at },
        }),
        'offer-letter.html',
      ),
    onSuccess: () => {
      setLetterOffer(null);
      setNotice('Reviewed letter downloaded. It has not been sent or signed.');
    },
  });
  const team = useQuery({
    queryKey: ['recruiting-team'],
    queryFn: ({ signal }) => api.request<{ id: string; label: string }[]>('/recruiting/team', { signal }),
  });
  const reports = useQuery({
    queryKey: ['recruiting-reports', tab === 'reports' ? reportFrom : '', tab === 'reports' ? reportTo : ''],
    queryFn: ({ signal }) =>
      api.request<Report>(
        '/recruiting/reports?' +
          new URLSearchParams(
            tab === 'reports'
              ? {
                  ...(reportFrom ? { from_date: reportFrom } : {}),
                  ...(reportTo ? { to_date: reportTo } : {}),
                }
              : {},
          ),
        { signal },
      ),
  });
  const pipeline = useQuery({
    queryKey: ['pipeline', jobId, stage, offset, pipelineSearch],
    queryFn: ({ signal }) =>
      api.request<Pipeline>(
        `/jobs/${jobId}/pipeline?${new URLSearchParams({ offset: String(offset), q: pipelineSearch, ...(stage ? { stage } : {}) })}`,
        { signal },
      ),
    enabled: !!jobId,
  });
  const candidateOptions = useQuery({
    queryKey: ['pipeline', 'picker', jobId, candidateSearch, candidateOffset],
    queryFn: ({ signal }) =>
      api.request<Pipeline>(
        `/jobs/${jobId}/pipeline?${new URLSearchParams({ offset: String(candidateOffset), q: candidateSearch })}`,
        { signal },
      ),
    enabled: adding && !!jobId && !candidateId,
  });
  const activities = useQuery({
    queryKey: [
      'recruiting-activities',
      tab,
      jobId,
      candidateId,
      activityOffset,
      activitySearch,
      statusFilter,
      ownerFilter,
      overdueOnly,
    ],
    queryFn: ({ signal }) =>
      api.request<{ total: number; items: Activity[] }>(
        `/recruiting/activities?${new URLSearchParams({ kind: tab, offset: String(activityOffset), q: activitySearch, ...(jobId ? { job_id: jobId } : {}), ...(candidateId ? { candidate_id: candidateId } : {}), ...(statusFilter ? { status: statusFilter } : {}), ...(ownerFilter ? { assigned_to: ownerFilter } : {}), ...(overdueOnly && tab === 'task' ? { overdue: 'true' } : {}) })}`,
        { signal },
      ),
    enabled: !!kindTitles[tab],
  });
  async function refresh() {
    await Promise.all(
      ['recruiting-reports', 'recruiting-activities', 'pipeline', 'candidate', 'decisions', 'ranked'].map(
        (key) => client.invalidateQueries({ queryKey: [key] }),
      ),
    );
  }
  const create = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api.request('/recruiting/' + (tab === 'feedback' ? 'feedback' : 'activities'), {
        method: 'POST',
        body,
      }),
    onSuccess: async () => {
      setAdding(false);
      setNotice(`${kindTitles[tab]} saved.`);
      await refresh();
    },
  });
  const move = useMutation({
    mutationFn: (body: { stage: string; reason: string }) =>
      api.request(`/applications/${moving!.id}/stage`, {
        method: 'PATCH',
        body: { ...body, expected_stage: moving!.stage },
      }),
    onSuccess: async () => {
      setMoving(null);
      setNotice('Hiring stage updated. The change is recorded in decision history.');
      await refresh();
    },
  });
  const update = useMutation({
    mutationFn: ({
      id,
      status,
      expected_updated_at,
    }: {
      id: string;
      status: string;
      expected_updated_at: string;
    }) =>
      api.request(`/recruiting/activities/${id}`, { method: 'PATCH', body: { status, expected_updated_at } }),
    onSuccess: refresh,
  });
  const edit = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) =>
      api.request(`/recruiting/activities/${id}/details`, { method: 'PATCH', body }),
    onSuccess: async () => {
      setEditing(null);
      setNotice('Changes saved. Download a new calendar event after rescheduling an interview.');
      await refresh();
    },
  });
  const calendar = useMutation({
    mutationFn: async (id: string) =>
      saveFile(
        await api.request<Blob>(`/recruiting/activities/${id}/calendar`, { blob: true }),
        'interview.ics',
      ),
  });
  function selectTab(value: string) {
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set('tab', value);
      next.delete('new');
      return next;
    });
    setActivityOffset(0);
    setStatusFilter('');
    setOverdueOnly(false);
    setNotice('');
    setAdding(false);
    setEditing(null);
    create.reset();
  }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const body: Record<string, unknown> = {
      kind: tab,
      title: form.get('title'),
      notes: form.get('notes') || '',
      job_id: jobId || null,
      candidate_id: candidateId || null,
      application_id: form.get('application') || null,
      assigned_to: form.get('owner') || null,
    };
    for (const field of ['due_at', 'end_at']) {
      const value = String(form.get(field) || '');
      if (value) body[field] = new Date(value).toISOString();
      else if (tab === 'task' && field === 'due_at') body[field] = null;
    }
    if (tab === 'interview') body.location = form.get('location') || '';
    if (!editing && selectedRound) {
      body.round_id = selectedRound.id;
      body.plan_version = hiringPlan.data!.version;
      if (tab === 'feedback')
        body.ratings = Object.fromEntries(
          selectedRound.criteria.map((criterion, index) => [
            criterion,
            Number(form.get('criterion-' + index)),
          ]),
        );
    }
    if (tab === 'feedback') {
      for (const field of ['technical', 'communication', 'experience']) body[field] = Number(form.get(field));
      body.recommendation = form.get('recommendation');
    }
    if (tab === 'offer') {
      for (const field of ['amount', 'currency', 'pay_period', 'start_date']) body[field] = form.get(field);
    }
    if (editing) {
      delete body.kind;
      delete body.job_id;
      delete body.candidate_id;
      delete body.application_id;
      body.expected_updated_at = editing.updated_at;
      edit.mutate({ id: editing.id, body });
    } else create.mutate(body);
  }
  function exportReport() {
    if (!reports.data) return;
    const csv = (value: string) => `"${value.replaceAll('"', '""')}"`;
    const rows = [
      ['Job', ...hiringStages],
      ...reports.data.by_job.map((job) => [
        /^\s*[=+@-]/.test(job.title) ? "'" + job.title : job.title,
        ...hiringStages.map((stage) => String(job.stages[stage] || 0)),
      ]),
    ];
    saveFile(
      new Blob([rows.map((row) => row.map(csv).join(',')).join('\r\n')], { type: 'text/csv;charset=utf-8' }),
      'hiring-report.csv',
    );
  }
  const data = reports.data;
  return (
    <div className="page recruiting-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Hiring operations</span>
          <h1>Recruiting workspace.</h1>
          <p>
            Track candidates from application to hire, coordinate interviews, and keep the team’s next steps
            in one place.
          </p>
        </div>
      </div>
      <nav className="recruiting-sections" aria-label="Recruiting sections">
        <button
          className="button secondary compact"
          disabled={reports.isFetching}
          onClick={() => void refresh()}
        >
          Refresh workspace
        </button>
        {sections
          .filter(([id]) => canManage || !['offer', 'duplicates'].includes(id))
          .map(([id, title]) => (
            <button
              key={id}
              aria-current={id === tab ? 'page' : undefined}
              className={`button ${id === tab ? 'primary' : 'secondary'} compact`}
              onClick={() => selectTab(id)}
            >
              {title}
            </button>
          ))}
      </nav>
      {notice && (
        <p className="notice success" role="status">
          {notice}
        </p>
      )}
      <ErrorNotice error={jobs.error || team.error || reports.error} />
      {data && (
        <div className="stat-grid ats-stats">
          {[
            ['Applications', data.applications],
            ['Open tasks', data.open_tasks],
            ['Overdue tasks', data.overdue_tasks],
            ['Open interviews', data.open_interviews],
            ['Hires · last 30 days', data.hires_last_30_days],
          ].map(([title, value]) => (
            <div className="stat-card" key={title}>
              <div>
                <strong>{value}</strong>
                <span>{title}</span>
              </div>
            </div>
          ))}
        </div>
      )}
      {tab === 'overview' && <WorkloadPanel />}
      {tab === 'duplicates' && canManage && <DuplicateReviewPanel />}
      {tab === 'overview' && (
        <section className="panel">
          <h2>Your hiring workflow</h2>
          <p>
            Start with a job, add resumes, and screen the candidates. Then use the pipeline to record
            interview, offer and hiring stages.
          </p>
          <div className="guide-grid">
            {[
              ['pipeline', 'Move candidates through hiring'],
              ['interview', 'Schedule the next interview'],
              ['task', 'Assign recruiter follow-ups'],
            ].map(([id, title]) => (
              <button className="button secondary" key={id} onClick={() => selectTab(id)}>
                {title}
              </button>
            ))}
          </div>
          <p className="field-hint">
            Records are saved in your workspace. Calendar exports let you add interviews to your calendar;
            this app does not send invitations or offer letters automatically.
          </p>
        </section>
      )}
      {tab !== 'overview' && tab !== 'reports' && tab !== 'duplicates' && (
        <section className="panel">
          <div className="section-heading">
            <label>
              Filter by job
              <select
                value={jobId}
                onChange={(event) => {
                  setJobId(event.target.value);
                  setOffset(0);
                  setActivityOffset(0);
                  setCandidateOffset(0);
                }}
              >
                <option value="">
                  {tab === 'pipeline' ? 'Choose a job to see its pipeline' : 'All jobs'}
                </option>
                {jobs.data?.map((job) => (
                  <option key={job.id} value={job.id}>
                    {job.title}
                  </option>
                ))}
              </select>
            </label>
            {kindTitles[tab] && (canManage || tab === 'feedback') && (
              <button
                className="button primary"
                onClick={() => {
                  create.reset();
                  setCandidateSearch('');
                  setCandidateOffset(0);
                  setAdding(true);
                }}
              >
                <Plus size={16} />
                Add {kindTitles[tab].toLowerCase()}
              </button>
            )}
          </div>
          {candidateId && (
            <p className="notice">
              Showing records for the selected candidate.{' '}
              <Link to={`/candidates/${candidateId}`}>Open profile</Link> ·{' '}
              <Link to={`/recruiting?tab=${tab}`}>Show all candidates</Link>
            </p>
          )}
          {tab === 'setup' &&
            (jobId ? (
              <HiringPlanEditor key={jobId} jobId={jobId} canManage={canManage} />
            ) : (
              <Empty title="Choose a job to configure interviews">
                Each job can have its own rounds and feedback criteria.
              </Empty>
            ))}
          {tab === 'pipeline' && (
            <>
              {!jobId && (
                <Empty title="Choose a job to start">
                  Every application is visible here, including candidates who have not been screened.
                </Empty>
              )}
              {jobId && (
                <>
                  <label>
                    Search pipeline candidates
                    <input
                      type="search"
                      maxLength={200}
                      value={pipelineSearch}
                      placeholder={
                        canManage ? 'Candidate name, resume filename or candidate ID' : 'Candidate ID'
                      }
                      onChange={(event) => {
                        setPipelineSearch(event.target.value);
                        setOffset(0);
                      }}
                    />
                  </label>
                  <div className="library-summary">
                    <button
                      className={`button ${!stage ? 'primary' : 'secondary'} compact`}
                      onClick={() => {
                        setStage('');
                        setOffset(0);
                      }}
                    >
                      All stages
                    </button>
                    {hiringStages.map((value) => (
                      <button
                        key={value}
                        className={`button ${stage === value ? 'primary' : 'secondary'} compact`}
                        onClick={() => {
                          setStage(value);
                          setOffset(0);
                        }}
                      >
                        {label(value)} {pipeline.data?.counts[value] || 0}
                      </button>
                    ))}
                  </div>
                  <ErrorNotice error={pipeline.error} retry={() => void pipeline.refetch()} />
                  {pipeline.isPending && <Spinner text="Loading hiring pipeline…" />}
                  {pipeline.data?.items.length === 0 && (
                    <Empty
                      title="No applications in this view"
                      action={
                        <Link className="button secondary" to={`/jobs/${jobId}?tab=resumes`}>
                          Add candidates to this job
                        </Link>
                      }
                    >
                      Choose another stage or add candidates from your resume library.
                    </Empty>
                  )}
                  <div className="library-list">
                    {pipeline.data?.items.map((app) => (
                      <article key={app.id} className="library-row">
                        <div>
                          <Link
                            className="candidate-name"
                            to={`/candidates/${app.candidate_id}?job=${jobId}`}
                          >
                            {app.name || `Candidate ${shortId(app.candidate_id)}`}
                          </Link>
                          <p className="field-hint">Applied {timestamp(app.created_at)}</p>
                        </div>
                        <Badge value={app.stage} />
                        <div className="library-actions">
                          {canManage && (
                            <button
                              className="button secondary compact"
                              onClick={() => {
                                move.reset();
                                setMoving(app);
                              }}
                            >
                              Change stage
                            </button>
                          )}
                          <Link
                            className="text-button"
                            to={`/recruiting?tab=interview&job=${jobId}&candidate=${app.candidate_id}`}
                          >
                            Interviews & activities
                          </Link>
                        </div>
                      </article>
                    ))}
                  </div>
                  {pipeline.data && pipeline.data.total > 50 && (
                    <div className="upload-pagination">
                      <span>
                        {offset + 1}–{Math.min(offset + 50, pipeline.data.total)} of {pipeline.data.total}
                      </span>
                      <button
                        className="button secondary"
                        disabled={!offset}
                        onClick={() => setOffset(offset - 50)}
                      >
                        Previous applications
                      </button>
                      <button
                        className="button secondary"
                        disabled={offset + 50 >= pipeline.data.total}
                        onClick={() => setOffset(offset + 50)}
                      >
                        Next applications
                      </button>
                    </div>
                  )}
                </>
              )}
            </>
          )}
          {kindTitles[tab] && (
            <>
              <div className="form-grid ats-filters">
                <label>
                  Search records
                  <input
                    type="search"
                    maxLength={200}
                    value={activitySearch}
                    placeholder={canManage ? 'Record title, job or candidate' : 'Record title or job'}
                    onChange={(event) => {
                      setActivitySearch(event.target.value);
                      setActivityOffset(0);
                    }}
                  />
                </label>
                <label>
                  Filter by status
                  <select
                    value={statusFilter}
                    onChange={(event) => {
                      setStatusFilter(event.target.value);
                      setActivityOffset(0);
                    }}
                  >
                    <option value="">All statuses</option>
                    {(tab === 'offer'
                      ? ['open', 'accepted', 'declined', 'cancelled']
                      : ['feedback', 'note'].includes(tab)
                        ? ['completed']
                        : ['open', 'completed', 'cancelled']
                    ).map((value) => (
                      <option key={value} value={value}>
                        {label(value)}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Filter by owner
                  <select
                    value={ownerFilter}
                    onChange={(event) => {
                      setOwnerFilter(event.target.value);
                      setActivityOffset(0);
                    }}
                  >
                    <option value="">Everyone</option>
                    {team.data?.map((owner) => (
                      <option key={owner.id} value={owner.id}>
                        {owner.label}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              {tab === 'task' && (
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={overdueOnly}
                    onChange={(event) => {
                      setOverdueOnly(event.target.checked);
                      setActivityOffset(0);
                      setStatusFilter('');
                    }}
                  />
                  Only overdue tasks
                </label>
              )}
              {(activitySearch || statusFilter || ownerFilter || overdueOnly) && (
                <button
                  className="text-button"
                  onClick={() => {
                    setActivitySearch('');
                    setStatusFilter('');
                    setOwnerFilter('');
                    setOverdueOnly(false);
                    setActivityOffset(0);
                  }}
                >
                  Clear record filters
                </button>
              )}
              {activities.data && (
                <p className="field-hint" role="status">
                  {activities.data.total} matching {activities.data.total === 1 ? 'record' : 'records'}
                </p>
              )}
              <ErrorNotice
                error={activities.error || update.error || calendar.error}
                retry={() => void activities.refetch()}
              />
              {activities.isPending && <Spinner text="Loading hiring records…" />}
              {activities.data?.items.length === 0 && (
                <Empty title="No matching records">
                  {canManage || tab === 'feedback'
                    ? 'Use the add button to record the team’s next step. You can also start from a candidate’s profile.'
                    : 'Your recruiting team’s records will appear here when they are added.'}
                </Empty>
              )}
              <div className="library-list">
                {activities.data?.items.map((item) => (
                  <article className="ats-activity" key={item.id}>
                    <div className="section-heading">
                      <h3>{item.title}</h3>
                      <Badge value={item.status} />
                    </div>
                    {item.candidate_id && (
                      <Link className="text-button" to={`/candidates/${item.candidate_id}`}>
                        {item.candidate_name || `Candidate ${shortId(item.candidate_id)}`} · Open profile
                      </Link>
                    )}
                    <p className="field-hint">
                      {item.job_id ? (
                        <Link to={`/jobs/${item.job_id}`}>{item.job_title || 'View job'}</Link>
                      ) : (
                        'No job linked'
                      )}
                    </p>
                    <p className="field-hint">
                      Owner:{' '}
                      {team.data?.find((owner) => owner.id === item.assigned_to)?.label || 'Unassigned'} ·
                      Recorded {timestamp(item.created_at)}
                    </p>
                    {item.due_at && (
                      <p>
                        <CalendarDays size={14} /> {timestamp(item.due_at)}
                        {item.end_at && ` → ${timestamp(item.end_at)}`}
                        {item.kind === 'task' &&
                          item.status === 'open' &&
                          Date.parse(item.due_at) < referenceTime && (
                            <span className="subtle-chip">Overdue</span>
                          )}
                      </p>
                    )}
                    {item.details.location && <p>Location / meeting: {item.details.location}</p>}
                    {item.details.round_name && (
                      <p>
                        Round: {item.details.round_name} · Plan v{item.details.plan_version}
                      </p>
                    )}
                    {item.details.ratings && (
                      <p>
                        {Object.entries(item.details.ratings)
                          .map(([criterion, rating]) => criterion + ': ' + rating + '/5')
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
                    {item.kind === 'offer' && (
                      <p>
                        {item.details.currency} {item.details.amount} / {item.details.pay_period} · Intended
                        start: {item.details.start_date}
                      </p>
                    )}
                    <p className="ats-notes">{item.notes}</p>
                    <div className="inline-actions">
                      {item.kind === 'offer' && canManage && (
                        <>
                          <Link
                            className="button secondary compact"
                            to={
                              '/operations?kind=offer_approval' + (item.job_id ? '&job=' + item.job_id : '')
                            }
                          >
                            Request offer approval
                          </Link>
                          <button
                            className="button secondary compact"
                            onClick={() => {
                              generateLetter.reset();
                              setTemplateId('');
                              setTemplateOffset(0);
                              setLetterOffer(item);
                            }}
                          >
                            Review / download letter
                          </button>
                        </>
                      )}
                      {item.kind === 'interview' && canManage && (
                        <button
                          className="button secondary compact"
                          disabled={calendar.isPending}
                          onClick={() => calendar.mutate(item.id)}
                        >
                          <Download size={14} />
                          Download calendar event
                        </button>
                      )}
                      {canManage && ['task', 'interview', 'offer'].includes(item.kind) && (
                        <button
                          className="button secondary compact"
                          onClick={() => {
                            edit.reset();
                            setEditing(item);
                          }}
                        >
                          {item.kind === 'interview' ? 'Edit / reschedule' : 'Edit record'}
                        </button>
                      )}
                      {canManage && ['task', 'interview', 'offer'].includes(item.kind) && (
                        <label className="ats-status-label">
                          Record status
                          <select
                            aria-label={`Status for ${item.title}`}
                            value={item.status}
                            disabled={update.isPending}
                            onChange={(event) =>
                              update.mutate({
                                id: item.id,
                                status: event.target.value,
                                expected_updated_at: item.updated_at,
                              })
                            }
                          >
                            {(item.kind === 'offer'
                              ? ['open', 'accepted', 'declined', 'cancelled']
                              : ['open', 'completed', 'cancelled']
                            ).map((status) => (
                              <option key={status} value={status}>
                                {label(status)}
                              </option>
                            ))}
                          </select>
                        </label>
                      )}
                    </div>
                  </article>
                ))}
              </div>
              {activities.data && activities.data.total > 50 && (
                <div className="upload-pagination">
                  <span>
                    {activityOffset + 1}–{Math.min(activityOffset + 50, activities.data.total)} of{' '}
                    {activities.data.total}
                  </span>
                  <button
                    className="button secondary"
                    disabled={!activityOffset}
                    onClick={() => setActivityOffset(activityOffset - 50)}
                  >
                    Previous records
                  </button>
                  <button
                    className="button secondary"
                    disabled={activityOffset + 50 >= activities.data.total}
                    onClick={() => setActivityOffset(activityOffset + 50)}
                  >
                    Next records
                  </button>
                </div>
              )}
            </>
          )}
        </section>
      )}
      {tab === 'reports' && data && (
        <section className="panel">
          <div className="section-heading">
            <h2>Hiring reports</h2>
            <button className="button secondary" onClick={exportReport}>
              <Download size={16} />
              Export report CSV
            </button>
          </div>
          <div className="form-grid">
            <label>
              Applications added from (UTC)
              <input type="date" value={reportFrom} onChange={(event) => setReportFrom(event.target.value)} />
            </label>
            <label>
              Applications added through (UTC)
              <input
                type="date"
                value={reportTo}
                min={reportFrom || undefined}
                onChange={(event) => setReportTo(event.target.value)}
              />
            </label>
          </div>
          <p className="field-hint">
            Application and conversion counts use the selected application-date cohort. Task, interview and
            offer totals show the current workspace workload.
          </p>
          <h3>Application sources</h3>
          <div className="library-summary">
            {Object.entries(data.sources || {}).map(([source, count]) => (
              <span className="subtle-chip" key={source}>
                {label(source)} {count}
              </span>
            ))}
          </div>
          <h3>Hiring conversion</h3>
          <p className="field-hint">
            Unique applications that have reached each recorded stage. One application can appear in several
            stages.
          </p>
          <div className="library-summary">
            {Object.entries(data.reached_stages || {}).map(([stage, count]) => (
              <span className="subtle-chip" key={stage}>
                {label(stage)} {count}
              </span>
            ))}
          </div>
          <p>
            Average time from application to first recorded hire:{' '}
            <strong>
              {data.average_days_to_hire === null
                ? 'No recorded hires yet'
                : `${data.average_days_to_hire} days`}
            </strong>
          </p>
          {!!data.monthly_cohorts?.length && (
            <section>
              <h3>Monthly hiring conversion</h3>
              <p className="field-hint">
                Applications grouped by their submission month in UTC, with stages they have reached so far.
                Showing the latest 24 months with applications in this date range.
              </p>
              <div className="library-list">
                {data.monthly_cohorts.map((month) => (
                  <article className="ats-activity" key={month.month}>
                    <h4>{month.month}</h4>
                    <p>
                      {month.applications} applications · {month.interview} reached interview · {month.offer}{' '}
                      reached offer · {month.hired} recorded hires
                    </p>
                    <p className="field-hint">
                      Hire conversion:{' '}
                      {month.applications ? Math.round((100 * month.hired) / month.applications) : 0}%
                    </p>
                  </article>
                ))}
              </div>
            </section>
          )}
          <p>
            Open offers: {data.open_offers ?? 'Restricted for your role'}. Counts are applications, so a
            candidate applying to two jobs counts twice.
          </p>
          <div className="library-list">
            {data.by_job.map((job) => (
              <article className="ats-activity" key={job.id}>
                <Link className="candidate-name" to={`/recruiting?tab=pipeline&job=${job.id}`}>
                  {job.title}
                </Link>
                <div className="library-summary">
                  {hiringStages.map((stage) => (
                    <span className="subtle-chip" key={stage}>
                      {label(stage)} {job.stages[stage] || 0}
                    </span>
                  ))}
                </div>
              </article>
            ))}
          </div>
        </section>
      )}
      {(adding || editing) && kindTitles[tab] && (canManage || tab === 'feedback') && (
        <ConfirmDialog
          title={`${editing ? 'Edit' : 'Add'} ${kindTitles[tab].toLowerCase()}`}
          onClose={() => {
            if (!create.isPending && !edit.isPending) {
              setAdding(false);
              setEditing(null);
            }
          }}
          initialFocus='[name="title"]'
        >
          <form key={editing?.id || 'new'} className="hr-form" onSubmit={submit}>
            <fieldset disabled={create.isPending || edit.isPending}>
              {editing && <p>Editing the existing record. Candidate and job links stay attached.</p>}
              <label>
                Title
                <input
                  name="title"
                  defaultValue={editing?.title}
                  required
                  minLength={2}
                  maxLength={300}
                  placeholder={tab === 'interview' ? 'e.g. Technical interview · round 1' : undefined}
                />
              </label>
              {!candidateId && !editing && (
                <>
                  <label>
                    Job
                    <select
                      value={jobId}
                      onChange={(event) => {
                        setJobId(event.target.value);
                        setStage('');
                        setOffset(0);
                        setCandidateOffset(0);
                        setCandidateSearch('');
                      }}
                    >
                      <option value="">{tab === 'task' ? 'General recruiter task' : 'Choose a job'}</option>
                      {jobs.data?.map((job) => (
                        <option key={job.id} value={job.id}>
                          {job.title}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Find a candidate
                    <input
                      type="search"
                      maxLength={200}
                      value={candidateSearch}
                      disabled={!jobId}
                      placeholder={
                        canManage ? 'Candidate name, resume filename or candidate ID' : 'Candidate ID'
                      }
                      onChange={(event) => {
                        setCandidateSearch(event.target.value);
                        setCandidateOffset(0);
                      }}
                    />
                  </label>
                  <label>
                    Candidate application
                    <select
                      key={`${jobId}:${candidateSearch}:${candidateOffset}`}
                      name="application"
                      required={tab !== 'task'}
                      disabled={!jobId || candidateOptions.isPending}
                    >
                      <option value="">
                        {tab === 'task' ? 'Optional · no candidate' : 'Choose a candidate'}
                      </option>
                      {candidateOptions.data?.items.map((app) => (
                        <option key={app.id} value={app.id}>
                          {app.name || `Candidate ${shortId(app.candidate_id)}`} · {label(app.stage)}
                        </option>
                      ))}
                    </select>
                  </label>
                  {jobId && candidateOptions.isPending && <Spinner text="Finding candidates…" />}
                  {candidateOptions.data?.total === 0 && (
                    <p role="status">No matching candidates. Try another search or job.</p>
                  )}
                  <ErrorNotice error={candidateOptions.error} retry={() => void candidateOptions.refetch()} />
                  {candidateOptions.data && candidateOptions.data.total > 50 && (
                    <div className="inline-actions">
                      <button
                        type="button"
                        className="text-button"
                        disabled={!candidateOffset}
                        onClick={() => setCandidateOffset(candidateOffset - 50)}
                      >
                        Previous candidates
                      </button>
                      <button
                        type="button"
                        className="text-button"
                        disabled={candidateOffset + 50 >= candidateOptions.data.total}
                        onClick={() => setCandidateOffset(candidateOffset + 50)}
                      >
                        Next candidates
                      </button>
                    </div>
                  )}
                  <p className="field-hint">
                    For a saved candidate without a job application, open their profile and use Recruiting
                    activities.
                  </p>
                </>
              )}
              <label>
                Assigned team member
                <select name="owner" defaultValue={editing?.assigned_to || ''}>
                  <option value="">{editing ? 'Unassigned' : 'Assign to me'}</option>
                  {team.data?.map((owner) => (
                    <option key={owner.id} value={owner.id}>
                      {owner.label}
                    </option>
                  ))}
                </select>
              </label>
              {!editing && hiringPlan.data && hiringPlan.data.rounds.length > 0 && (
                <label>
                  Interview round
                  <select value={roundId} required onChange={(event) => setRoundId(event.target.value)}>
                    <option value="">Choose round</option>
                    {hiringPlan.data.rounds.map((row) => (
                      <option key={row.id} value={row.id}>
                        {row.name}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <ErrorNotice error={hiringPlan.error} />
              {!editing && tab === 'feedback' && selectedRound && (
                <div key={selectedRound.id} className="form-grid">
                  {selectedRound.criteria.map((criterion, index) => (
                    <label key={criterion}>
                      {criterion}
                      <select name={'criterion-' + index} required>
                        <option value="">Select rating</option>
                        {[1, 2, 3, 4, 5].map((value) => (
                          <option key={value} value={value}>
                            {value} / 5
                          </option>
                        ))}
                      </select>
                    </label>
                  ))}
                </div>
              )}
              {['task', 'interview'].includes(tab) && (
                <label>
                  {tab === 'task' ? 'Due date and time (optional)' : 'Interview start'}
                  <input
                    type="datetime-local"
                    step="1"
                    name="due_at"
                    defaultValue={localDateTime(editing?.due_at)}
                    required={tab === 'interview'}
                  />
                </label>
              )}
              {tab === 'interview' && (
                <>
                  <label>
                    Interview end
                    <input
                      type="datetime-local"
                      step="1"
                      name="end_at"
                      defaultValue={localDateTime(editing?.end_at)}
                      required
                    />
                  </label>
                  <label>
                    Location or meeting link
                    <input
                      name="location"
                      defaultValue={editing?.details.location}
                      maxLength={1000}
                      placeholder="e.g. Office / video meeting URL"
                    />
                  </label>
                  <p className="field-hint">
                    Times use your device’s timezone. Download the calendar event after saving. Invitations
                    are not sent automatically.
                  </p>
                </>
              )}
              {tab === 'feedback' && (
                <>
                  <div className="form-grid">
                    {[
                      ['technical', 'Technical / role skills'],
                      ['communication', 'Communication'],
                      ['experience', 'Relevant experience'],
                    ].map(([field, title]) => (
                      <label key={field}>
                        {title}
                        <select name={field} aria-label={title} required>
                          <option value="">Select rating</option>
                          {[1, 2, 3, 4, 5].map((value) => (
                            <option key={value} value={value}>
                              {value} / 5
                            </option>
                          ))}
                        </select>
                      </label>
                    ))}
                  </div>
                  <label>
                    Recommendation
                    <select name="recommendation" aria-label="Recommendation" required>
                      <option value="">Choose recommendation</option>
                      <option value="hire">Hire</option>
                      <option value="no_hire">Do not hire</option>
                      <option value="unsure">Need more evidence</option>
                    </select>
                  </label>
                </>
              )}
              {tab === 'offer' && (
                <>
                  <div className="form-grid">
                    <label>
                      Compensation amount
                      <input
                        type="number"
                        name="amount"
                        defaultValue={editing?.details.amount}
                        min="0"
                        max="1000000000"
                        step="0.01"
                        required
                      />
                    </label>
                    <label>
                      Currency code
                      <input
                        name="currency"
                        defaultValue={editing?.details.currency || 'INR'}
                        pattern="[A-Z]{3}"
                        maxLength={3}
                        required
                      />
                    </label>
                    <label>
                      Pay period
                      <select name="pay_period" defaultValue={editing?.details.pay_period || 'year'}>
                        <option value="year">Annual</option>
                        <option value="month">Monthly</option>
                        <option value="hour">Hourly</option>
                      </select>
                    </label>
                    <label>
                      Intended start date
                      <input
                        name="start_date"
                        type="date"
                        defaultValue={editing?.details.start_date}
                        required
                      />
                    </label>
                  </div>
                  <p className="field-hint">
                    This records an offer for your team. It does not send an offer letter or sign a contract.
                  </p>
                </>
              )}
              <label>
                {tab === 'feedback' ? 'Evidence and feedback' : 'Notes'}
                <textarea
                  name="notes"
                  defaultValue={editing?.notes}
                  rows={4}
                  maxLength={10000}
                  minLength={tab === 'feedback' ? 5 : undefined}
                  required={tab === 'feedback'}
                />
              </label>
              <ErrorNotice error={editing ? edit.error : create.error} />
              <div className="dialog-actions">
                <button
                  type="button"
                  className="button secondary"
                  onClick={() => {
                    setAdding(false);
                    setEditing(null);
                  }}
                >
                  Cancel
                </button>
                <button
                  className="button primary"
                  type="submit"
                  disabled={
                    !editing &&
                    !!jobId &&
                    ['interview', 'feedback'].includes(tab) &&
                    (hiringPlan.isPending || hiringPlan.isError)
                  }
                >
                  {create.isPending || edit.isPending ? 'Saving…' : editing ? 'Save changes' : 'Save record'}
                </button>
              </div>
            </fieldset>
          </form>
        </ConfirmDialog>
      )}
      {letterOffer && (
        <ConfirmDialog
          title="Review offer letter"
          onClose={() => {
            if (!generateLetter.isPending) setLetterOffer(null);
          }}
        >
          <form
            className="hr-form"
            onSubmit={(event) => {
              event.preventDefault();
              generateLetter.mutate(new FormData(event.currentTarget).get('reviewed') === 'on');
            }}
          >
            <fieldset disabled={generateLetter.isPending}>
              <p>
                {letterOffer.candidate_name || 'Selected candidate'} · {letterOffer.job_title || 'Position'} ·{' '}
                {letterOffer.details.currency} {letterOffer.details.amount} / {letterOffer.details.pay_period}{' '}
                · Start {letterOffer.details.start_date}
              </p>
              <label>
                Letter template
                <select required value={templateId} onChange={(event) => setTemplateId(event.target.value)}>
                  <option value="">Choose template</option>
                  {templates.data?.items.map((row) => (
                    <option key={row.id} value={row.id}>
                      {row.title}
                    </option>
                  ))}
                </select>
              </label>
              {templates.data?.total === 0 && (
                <p>
                  Create a template in{' '}
                  <Link to="/operations?kind=offer_template">Hiring workflows → Offer letter templates</Link>.
                </p>
              )}
              {templateId && (
                <pre className="ats-notes">
                  {templates.data?.items.find((row) => row.id === templateId)?.data.content}
                </pre>
              )}
              {templates.data && templates.data.total > 25 && (
                <div className="inline-actions">
                  <button
                    type="button"
                    className="text-button"
                    disabled={!templateOffset}
                    onClick={() => {
                      setTemplateOffset(templateOffset - 25);
                      setTemplateId('');
                    }}
                  >
                    Previous templates
                  </button>
                  <button
                    type="button"
                    className="text-button"
                    disabled={templateOffset + 25 >= templates.data.total}
                    onClick={() => {
                      setTemplateOffset(templateOffset + 25);
                      setTemplateId('');
                    }}
                  >
                    Next templates
                  </button>
                </div>
              )}
              <label className="checkbox-label">
                <input type="checkbox" name="reviewed" required />I reviewed the template, candidate and offer
                details.
              </label>
              <p className="field-hint">
                Downloads a printable HTML document. Existing approval requests must be approved for the
                current offer. No letter is sent and no contract is signed.
              </p>
              <ErrorNotice error={templates.error || generateLetter.error} />
              <button className="button primary" disabled={!templateId}>
                Download reviewed letter
              </button>
            </fieldset>
          </form>
        </ConfirmDialog>
      )}
      {moving && (
        <ConfirmDialog
          title="Change hiring stage"
          onClose={() => {
            if (!move.isPending) setMoving(null);
          }}
        >
          <form
            className="hr-form"
            onSubmit={(event) => {
              event.preventDefault();
              const form = new FormData(event.currentTarget);
              move.mutate({ stage: String(form.get('stage')), reason: String(form.get('reason')) });
            }}
          >
            <p>
              {moving.name || `Candidate ${shortId(moving.candidate_id)}`} · Current stage:{' '}
              {label(moving.stage)}
            </p>
            <label>
              New stage
              <select name="stage" defaultValue={moving.stage} disabled={move.isPending}>
                {hiringStages.map((value) => (
                  <option key={value} value={value}>
                    {label(value)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Reason
              <textarea
                name="reason"
                required
                minLength={2}
                maxLength={2000}
                disabled={move.isPending}
                placeholder="Record why the team is changing this stage"
              />
            </label>
            <ErrorNotice error={move.error} />
            <button className="button primary" disabled={move.isPending}>
              Save stage change
            </button>
          </form>
        </ConfirmDialog>
      )}
    </div>
  );
}
