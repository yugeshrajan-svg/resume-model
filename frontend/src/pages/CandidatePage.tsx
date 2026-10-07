import { useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ArrowLeft, CheckCheck, Mail, Phone, Quote, UserRound } from 'lucide-react';
import { api } from '../api/client';
import type { CandidateProfile, RunInput } from '../api/types';
import { useSession } from '../hooks/useSession';
import { date, featureLabels, label, scoreTier, shortId } from '../lib/format';
import { Badge, Empty, ErrorNotice, ExplanationView, Spinner } from '../components/ui';
import DecisionPanel from '../components/DecisionPanel';
import ConsentPanel from '../components/ConsentPanel';
import PrivacyPanel from '../components/PrivacyPanel';
import ResumeHistoryItem from '../components/ResumeHistoryItem';
import JobTitle from '../components/JobTitle';
import CandidateActivityFeed from '../components/CandidateActivityFeed';

export default function CandidatePage() {
  const { candidateId } = useParams() as { candidateId: string };
  const [params] = useSearchParams();
  const [selected, setSelected] = useState<{ context: string; runId: string } | null>(null);
  const role = useSession()!.role;
  const canReadContacts = role !== 'hiring_manager';
  const navigate = useNavigate();
  const profile = useQuery({
    queryKey: ['candidate', candidateId],
    queryFn: ({ signal }) => api.request<CandidateProfile>(`/candidates/${candidateId}`, { signal }),
    refetchInterval: (query) =>
      query.state.data?.resumes.some((resume) => ['registered', 'processing'].includes(resume.status))
        ? 2000
        : false,
  });
  const jobId = params.get('job');
  const context = `${candidateId}:${jobId || ''}`;
  const requestedRun = selected?.context === context ? selected.runId : params.get('run');
  const data = profile.data;
  const history = data?.screening.filter((item) => !jobId || item.job_id === jobId) || [];
  const assessment = requestedRun ? history.find((item) => item.run_id === requestedRun) : history[0];
  const inputs = useQuery({
    queryKey: ['run-inputs', assessment?.run_id],
    queryFn: ({ signal }) =>
      api.request<RunInput[]>(`/screening-runs/${assessment!.run_id}/inputs`, { signal }),
    enabled: !!assessment,
  });
  const input = inputs.data?.find((item) => item.candidate_id === candidateId);
  const back = jobId
    ? `/jobs/${jobId}?tab=results${params.get('run') ? `&run=${params.get('run')}` : ''}`
    : `/talent-pool?tab=${canReadContacts ? 'library' : 'search'}`;
  if (profile.isPending)
    return (
      <div className="page">
        <Spinner text="Loading candidate profile…" />
      </div>
    );
  if (!data)
    return (
      <div className="page">
        <ErrorNotice
          error={profile.error}
          retry={() => {
            void profile.refetch();
          }}
        />
        <Link to={back}>Back to workspace</Link>
      </div>
    );
  return (
    <div className="page candidate-page">
      <Link className="back-link" to={back}>
        <ArrowLeft size={16} />
        {jobId ? 'Back to results' : canReadContacts ? 'Back to saved resumes' : 'Back to resume search'}
      </Link>
      <div className="page-heading">
        <div className="candidate-heading-identity">
          <span className="profile-avatar" aria-hidden="true">
            <UserRound size={28} />
          </span>
          <div>
            <span className="eyebrow">Candidate review</span>
            <h1>{(canReadContacts && data.contact?.name) || `Candidate ${shortId(data.candidate_id)}`}</h1>
            <p>
              Added {date(data.created_at)}
              <span className="middot">·</span>
              {data.resumes.length} resume {data.resumes.length === 1 ? 'version' : 'versions'}
            </p>
          </div>
        </div>
        {assessment && (
          <div className={`profile-score ${scoreTier(assessment.score, assessment.eligible)}`}>
            <svg viewBox="0 0 120 120" aria-hidden="true">
              <circle className="ring-track" cx="60" cy="60" r="52" />
              <circle
                className="ring-value"
                cx="60"
                cy="60"
                r="52"
                pathLength={100}
                strokeDasharray={`${Math.max(0, Math.min(100, assessment.score ?? 0))} 100`}
              />
            </svg>
            <div>
              <span>{assessment.score === null ? '—' : assessment.score.toFixed(1)}</span>
              <small>Screening score / 100</small>
            </div>
          </div>
        )}
      </div>
      {canReadContacts && data.contact && (
        <div className="contact-strip">
          {data.contact.email && (
            <span>
              <Mail size={16} />
              {data.contact.email}
            </span>
          )}
          {data.contact.phone && (
            <span>
              <Phone size={16} />
              {data.contact.phone}
            </span>
          )}
        </div>
      )}
      <div className="profile-layout">
        <div className="profile-main">
          <CandidateActivityFeed key={candidateId} candidateId={candidateId} canManage={canReadContacts} />
          <section className="panel">
            <div className="section-heading">
              <div>
                <h2>Evidence, in context</h2>
                <p>Review a screening run to understand the candidate’s fit.</p>
              </div>
              {history.length > 0 && (
                <label className="inline-label">
                  Run
                  <select
                    aria-label="Candidate screening run"
                    value={assessment?.run_id || ''}
                    onChange={(event) => setSelected({ context, runId: event.target.value })}
                  >
                    <option value="" disabled>
                      Select a run
                    </option>
                    {history.map((item) => (
                      <option key={item.run_id} value={item.run_id}>
                        {date(item.run_created_at)} · {shortId(item.run_id)} · v{item.requirements_version}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </div>
            {!assessment ? (
              <Empty title={requestedRun ? 'No result for the selected run' : 'No screening results yet'}>
                {requestedRun
                  ? 'Choose another run to view this candidate’s assessment.'
                  : 'This candidate’s screening results will appear here after processing.'}
              </Empty>
            ) : (
              <>
                <div className="assessment-meta">
                  <Badge
                    value={
                      assessment.eligible === null
                        ? assessment.status
                        : assessment.eligible
                          ? 'eligible'
                          : 'ineligible'
                    }
                  />
                  {input && <span>Assessed resume v{input.resume_version}</span>}
                  <span>Requirements v{assessment.requirements_version}</span>
                  <span>Screened {date(assessment.run_created_at)}</span>
                </div>
                <ErrorNotice
                  error={inputs.error}
                  retry={() => {
                    void inputs.refetch();
                  }}
                />
                {!!assessment.knockout?.reasons?.length && (
                  <div className="notice error">
                    <div>
                      <strong>Eligibility checks</strong>
                      <ul>
                        {assessment.knockout.reasons.map((reason, index) => (
                          <li key={index}>{reason.detail}</li>
                        ))}
                      </ul>
                    </div>
                  </div>
                )}
                {!!assessment.features?.length && (
                  <div className="fit-glance">
                    <h3>Score at a glance</h3>
                    {assessment.features.map((feature) => (
                      <div className="fit-row" key={feature.name}>
                        <span>{featureLabels[feature.name] || label(feature.name)}</span>
                        <div className="fit-track" aria-hidden="true">
                          {feature.applicable && <span style={{ width: `${feature.value * 100}%` }} />}
                        </div>
                        <strong>{feature.applicable ? `${Math.round(feature.value * 100)}%` : 'N/A'}</strong>
                      </div>
                    ))}
                  </div>
                )}
                <ExplanationView explanation={assessment.explanation} />
                <details className="evidence-details">
                  <summary className="feature-heading">
                    <h3>Feature breakdown</h3>
                    <span className="subtle-chip">
                      <CheckCheck size={13} />
                      Verified evidence
                    </span>
                  </summary>
                  <div className="features">
                    {assessment.features?.map((feature) => (
                      <article className="feature-card" key={feature.name}>
                        <div className="feature-card-top">
                          <h4>{featureLabels[feature.name] || label(feature.name)}</h4>
                          <strong>
                            {feature.applicable ? `${Math.round(feature.value * 100)}%` : 'Not applicable'}
                          </strong>
                        </div>
                        {feature.applicable && (
                          <div className="feature-track">
                            <span style={{ width: `${feature.value * 100}%` }} />
                          </div>
                        )}
                        <p>{feature.detail}</p>
                        {feature.evidence.map((quote, index) => (
                          <blockquote key={index}>
                            <Quote size={14} />
                            {quote}
                          </blockquote>
                        ))}
                      </article>
                    ))}
                  </div>
                </details>
              </>
            )}
          </section>
        </div>
        <aside className="profile-aside">
          <section className="panel">
            <h3>Recruiting activities</h3>
            <p>Coordinate this candidate’s interviews, feedback and follow-ups.</p>
            <div className="stack">
              {(canReadContacts
                ? [
                    ['interview', 'Schedule interview'],
                    ['task', 'Add follow-up task'],
                    ['feedback', 'Record feedback'],
                    ['note', 'Add candidate note'],
                    ['offer', 'Record offer'],
                  ]
                : [['feedback', 'Record feedback']]
              ).map(([kind, title]) => (
                <Link
                  className="button secondary compact"
                  key={kind}
                  to={`/recruiting?tab=${kind}&candidate=${candidateId}&new=1${jobId ? `&job=${jobId}` : ''}`}
                >
                  {title}
                </Link>
              ))}
              <Link className="text-button" to={`/recruiting?tab=interview&candidate=${candidateId}`}>
                View interview records
              </Link>
            </div>
          </section>
          {data.applications
            .filter((application) => !jobId || application.job_id === jobId)
            .map((application) => (
              <DecisionPanel
                key={application.application_id}
                applicationId={application.application_id}
                candidateId={candidateId}
                jobId={application.job_id}
                stage={application.stage}
              />
            ))}
          <section className="panel">
            <h3>Resume history</h3>
            <div className="resume-history">
              {data.resumes.map((resume) => (
                <ResumeHistoryItem key={resume.resume_id} candidateId={candidateId} resume={resume} />
              ))}
            </div>
          </section>
          {role === 'admin' && (
            <PrivacyPanel
              key={candidateId}
              candidateId={candidateId}
              resumeIds={data.resumes.map((resume) => resume.resume_id)}
              onDeleted={() => navigate(back, { replace: true })}
            />
          )}
          <section className="panel">
            <h3>Applications</h3>
            {data.applications.map((application) => (
              <div className="application-item" key={application.application_id}>
                <Link to={`/jobs/${application.job_id}?tab=results`}>
                  <JobTitle id={application.job_id} />
                </Link>
                <Badge value={application.stage} />
              </div>
            ))}
          </section>
        </aside>
      </div>
      <div className="candidate-consent">
        <ConsentPanel key={candidateId} candidateId={candidateId} />
      </div>
    </div>
  );
}
