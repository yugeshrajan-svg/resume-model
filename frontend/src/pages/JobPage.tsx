import { Link, useParams, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ArrowLeft, Check, FileText } from 'lucide-react';
import { api } from '../api/client';
import type { Job, RequirementsVersion } from '../api/types';
import { date } from '../lib/format';
import { Badge, ErrorNotice, Spinner } from '../components/ui';
import RequirementsPanel from '../components/RequirementsPanel';
import UploadPanel from '../components/UploadPanel';
import ResultsPanel from '../components/ResultsPanel';
import PoolMatchesPanel from '../components/PoolMatchesPanel';

export default function JobPage() {
  const { jobId } = useParams() as { jobId: string };
  const [params, setParams] = useSearchParams();
  const requested = params.get('tab') || 'requirements';
  const tab = ['requirements', 'resumes', 'results'].includes(requested) ? requested : 'requirements';
  const job = useQuery({
    queryKey: ['job', jobId],
    queryFn: ({ signal }) => api.request<Job>(`/jobs/${jobId}`, { signal }),
  });
  const requirements = useQuery({
    queryKey: ['requirements', jobId],
    queryFn: ({ signal }) => api.request<RequirementsVersion[]>(`/jobs/${jobId}/requirements`, { signal }),
  });
  if (job.isPending)
    return (
      <div className="page">
        <Spinner text="Opening job workspace…" />
        <ErrorNotice error={job.error} />
      </div>
    );
  if (!job.data)
    return (
      <div className="page">
        <ErrorNotice
          error={job.error}
          retry={() => {
            void job.refetch();
          }}
        />
        <Link to="/jobs">Back to jobs</Link>
      </div>
    );
  const latest = requirements.data?.at(-1);
  const confirmed = requirements.data?.filter((version) => version.confirmed_at).at(-1);
  return (
    <div className="page job-workspace-page">
      <Link to="/jobs" className="back-link">
        <ArrowLeft size={16} />
        All jobs
      </Link>
      <div className="page-heading job-heading">
        <div>
          <span className="eyebrow">Job</span>
          <h1>{job.data.title}</h1>
          <p>
            Created {date(job.data.created_at)}
            <span className="middot">·</span>
            {confirmed ? `Confirmed requirements v${confirmed.version}` : 'Requirements need confirmation'}
          </p>
        </div>
        {latest && <Badge value={latest.confirmed_at ? 'confirmed' : 'draft'} />}
      </div>
      <Link className="button secondary compact" to={`/recruiting?tab=pipeline&job=${jobId}`}>
        Open hiring pipeline &amp; interviews
      </Link>
      <div className="tabs" role="tablist" aria-label="Job workflow">
        {[
          ['requirements', '01', 'Requirements', 'Define the right fit'],
          ['resumes', '02', 'Resumes', 'Bring in your candidates'],
          ['results', '03', 'Screening & results', 'Review your shortlist'],
        ].map(([value, number, title, description]) => (
          <button
            key={value}
            role="tab"
            aria-label={title}
            aria-selected={tab === value}
            aria-controls={`panel-${value}`}
            tabIndex={tab === value ? 0 : -1}
            id={`tab-${value}`}
            className={tab === value ? 'selected' : ''}
            onKeyDown={(event) => {
              const values = ['requirements', 'resumes', 'results'];
              const index = values.indexOf(value);
              const target =
                event.key === 'ArrowRight'
                  ? (index + 1) % 3
                  : event.key === 'ArrowLeft'
                    ? (index + 2) % 3
                    : event.key === 'Home'
                      ? 0
                      : event.key === 'End'
                        ? 2
                        : null;
              if (target === null) return;
              event.preventDefault();
              document.getElementById(`tab-${values[target]}`)?.focus();
              setParams((previous) => {
                const next = new URLSearchParams(previous);
                next.set('tab', values[target]);
                return next;
              });
            }}
            onClick={() =>
              setParams((previous) => {
                const next = new URLSearchParams(previous);
                next.set('tab', value);
                return next;
              })
            }
          >
            <span
              className={`workflow-number ${value === 'requirements' && confirmed ? 'complete' : ''}`}
              aria-hidden="true"
            >
              {value === 'requirements' && confirmed ? <Check size={15} /> : number}
            </span>
            <span className="workflow-tab-copy">
              <strong>{title}</strong>
              <small>{description}</small>
            </span>
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {tab === 'requirements' && (
          <RequirementsPanel
            key={jobId}
            job={job.data}
            versions={requirements.data || []}
            loading={requirements.isPending}
            error={requirements.error}
            retry={() => {
              void requirements.refetch();
            }}
          />
        )}
        {tab === 'resumes' && (
          <div className="stack">
            <PoolMatchesPanel key={`pool-${jobId}`} jobId={jobId} />
            <UploadPanel key={jobId} jobId={jobId} />
          </div>
        )}
        {tab === 'results' && (
          <ResultsPanel
            key={jobId}
            jobId={jobId}
            confirmed={confirmed}
            requirementsLoading={requirements.isPending}
          />
        )}
      </div>
      <details className="jd-details">
        <summary>
          <FileText size={16} />
          Original job description
        </summary>
        <p>{job.data.jd_text}</p>
      </details>
    </div>
  );
}
