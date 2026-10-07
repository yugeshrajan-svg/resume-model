import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api/client';
import { useSession } from '../hooks/useSession';
import { timestamp } from '../lib/format';
import { Badge, Empty, ErrorNotice, Spinner } from '../components/ui';
import ConfirmDialog from '../components/ConfirmDialog';
import type { HiringPlan } from '../components/HiringPlanEditor';

export default function CandidatePortalPage() {
  const role = useSession()!.role;
  const allowed = ['admin', 'recruiter'].includes(role);
  const client = useQueryClient();
  const [jobId, setJobId] = useState('');
  const [search, setSearch] = useState('');
  const [candidateOffset, setCandidateOffset] = useState(0);
  const [linkOffset, setLinkOffset] = useState(0);
  const [publishing, setPublishing] = useState(false);
  const [booking, setBooking] = useState(false);
  const [slots, setSlots] = useState([{ id: crypto.randomUUID(), start: '', end: '' }]);
  const jobs = useQuery({
    queryKey: ['jobs'],
    queryFn: ({ signal }) =>
      api.request<{ id: string; title: string; jd_text: string }[]>('/jobs', { signal }),
    enabled: allowed,
  });
  const links = useQuery({
    queryKey: ['portal-links', linkOffset],
    queryFn: ({ signal }) =>
      api.request<
        {
          id: string;
          kind: string;
          title: string;
          status: string;
          candidate_id: string | null;
          data: { expires_at?: string };
        }[]
      >('/portal/links?offset=' + linkOffset, { signal }),
    enabled: allowed,
  });
  const candidates = useQuery({
    queryKey: ['pipeline', 'booking', jobId, search, candidateOffset],
    queryFn: ({ signal }) =>
      api.request<{ total: number; items: { candidate_id: string; name: string | null }[] }>(
        '/jobs/' + jobId + '/pipeline?' + new URLSearchParams({ q: search, offset: String(candidateOffset) }),
        { signal },
      ),
    enabled: booking && !!jobId,
  });
  const team = useQuery({
    queryKey: ['recruiting-team'],
    queryFn: ({ signal }) =>
      api.request<{ id: string; label: string; role: string }[]>('/recruiting/team', { signal }),
    enabled: allowed,
  });
  const plan = useQuery({
    queryKey: ['hiring-plan', jobId],
    queryFn: ({ signal }) => api.request<HiringPlan>('/jobs/' + jobId + '/hiring-plan', { signal }),
    enabled: booking && !!jobId,
  });
  const publish = useMutation({
    mutationFn: () =>
      api.request('/portal/jobs', { method: 'POST', body: { job_id: jobId, confirmed: true } }),
    onSuccess: async () => {
      setPublishing(false);
      await client.invalidateQueries({ queryKey: ['portal-links'] });
    },
  });
  const createBooking = useMutation({
    mutationFn: (body: unknown) => api.request('/portal/booking-links', { method: 'POST', body }),
    onSuccess: async () => {
      setBooking(false);
      await client.invalidateQueries({ queryKey: ['portal-links'] });
    },
  });
  const close = useMutation({
    mutationFn: (id: string) => api.request('/portal/links/' + id + '/close', { method: 'POST' }),
    onSuccess: () => client.invalidateQueries({ queryKey: ['portal-links'] }),
  });
  const selectedJob = jobs.data?.find((job) => job.id === jobId);
  return (
    <div className="page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Candidate experience</span>
          <h1>Candidate portal</h1>
          <p>Enable application pages and offer interview times through a booking link.</p>
        </div>
      </div>
      {!allowed ? (
        <Empty title="Recruiter access required">
          Administrators and recruiters manage candidate-facing links.
        </Empty>
      ) : (
        <>
          <p className="notice">
            Links work on this app’s address. A localhost link is a local preview; use an internet-accessible
            deployment before sharing with applicants. Job-board distribution and email invitations need
            provider connections.
          </p>
          <ErrorNotice
            error={jobs.error || links.error || team.error || close.error}
            retry={() => void links.refetch()}
          />
          <section className="panel">
            <label>
              Job
              <select
                value={jobId}
                onChange={(event) => {
                  setJobId(event.target.value);
                  setCandidateOffset(0);
                }}
              >
                <option value="">Choose job</option>
                {jobs.data?.map((job) => (
                  <option key={job.id} value={job.id}>
                    {job.title}
                  </option>
                ))}
              </select>
            </label>
            <div className="inline-actions">
              <button
                className="button primary"
                disabled={!jobId}
                onClick={() => {
                  publish.reset();
                  setPublishing(true);
                }}
              >
                Enable application page
              </button>
              <button
                className="button secondary"
                disabled={!jobId}
                onClick={() => {
                  createBooking.reset();
                  setSearch('');
                  setCandidateOffset(0);
                  setSlots([{ id: crypto.randomUUID(), start: '', end: '' }]);
                  setBooking(true);
                }}
              >
                Create interview booking link
              </button>
            </div>
          </section>
          <section className="panel">
            <h2>Application and booking links</h2>
            {links.isPending && <Spinner text="Loading portal links…" />}
            {links.data?.length === 0 && (
              <Empty title="No portal links yet">Choose a job to create the first link.</Empty>
            )}
            {links.data?.map((row) => (
              <article className="ats-activity" key={row.id}>
                <div className="section-heading">
                  <h3>{row.title}</h3>
                  <Badge value={row.status} />
                </div>
                <p>
                  {row.kind === 'public_job' ? 'Application page' : 'Interview booking'}
                  {row.data.expires_at && ' · Expires ' + timestamp(row.data.expires_at)}
                </p>
                {['published', 'open'].includes(row.status) && (
                  <>
                    <Link
                      className="text-button"
                      to={(row.kind === 'public_job' ? '/apply/' : '/book/') + row.id}
                    >
                      Open candidate view
                    </Link>
                    <p className="ats-notes">
                      {window.location.origin + (row.kind === 'public_job' ? '/apply/' : '/book/') + row.id}
                    </p>
                    <button
                      className="button secondary compact"
                      disabled={close.isPending}
                      onClick={() => close.mutate(row.id)}
                    >
                      Close link
                    </button>
                  </>
                )}
                {row.candidate_id && (
                  <Link className="text-button" to={'/candidates/' + row.candidate_id}>
                    Open candidate profile
                  </Link>
                )}
              </article>
            ))}
            <div className="upload-pagination">
              <button
                className="button secondary"
                disabled={!linkOffset}
                onClick={() => setLinkOffset(linkOffset - 25)}
              >
                Previous links
              </button>
              <button
                className="button secondary"
                disabled={(links.data?.length || 0) < 25}
                onClick={() => setLinkOffset(linkOffset + 25)}
              >
                Next links
              </button>
            </div>
          </section>
        </>
      )}
      {publishing && selectedJob && (
        <ConfirmDialog
          title="Review application page"
          onClose={() => {
            if (!publish.isPending) setPublishing(false);
          }}
        >
          <h2>{selectedJob.title}</h2>
          <p className="ats-notes">{selectedJob.jd_text}</p>
          <form
            className="hr-form"
            onSubmit={(event) => {
              event.preventDefault();
              publish.mutate();
            }}
          >
            <label className="checkbox-label">
              <input type="checkbox" required disabled={publish.isPending} />I reviewed this description and
              want it visible through the application link.
            </label>
            <ErrorNotice error={publish.error} />
            <button className="button primary" disabled={publish.isPending}>
              Enable reviewed page
            </button>
          </form>
        </ConfirmDialog>
      )}
      {booking && (
        <ConfirmDialog
          title="Offer interview slots"
          onClose={() => {
            if (!createBooking.isPending) setBooking(false);
          }}
        >
          <form
            className="hr-form"
            onSubmit={(event) => {
              event.preventDefault();
              const form = new FormData(event.currentTarget);
              createBooking.mutate({
                job_id: jobId,
                candidate_id: form.get('candidate'),
                assigned_to: form.get('owner'),
                location: form.get('location') || '',
                round_id: form.get('round') || null,
                plan_version: form.get('round') ? plan.data?.version : null,
                slots: slots.map((slot) => ({
                  start: new Date(slot.start).toISOString(),
                  end: new Date(slot.end).toISOString(),
                })),
              });
            }}
          >
            <fieldset disabled={createBooking.isPending}>
              <label>
                Search candidates
                <input
                  type="search"
                  value={search}
                  onChange={(event) => {
                    setSearch(event.target.value);
                    setCandidateOffset(0);
                  }}
                />
              </label>
              <label>
                Candidate
                <select key={search + candidateOffset} name="candidate" required>
                  <option value="">Choose candidate</option>
                  {candidates.data?.items.map((row) => (
                    <option key={row.candidate_id} value={row.candidate_id}>
                      {row.name || row.candidate_id}
                    </option>
                  ))}
                </select>
              </label>
              {candidates.data && candidates.data.total > 50 && (
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
                    disabled={candidateOffset + 50 >= candidates.data.total}
                    onClick={() => setCandidateOffset(candidateOffset + 50)}
                  >
                    Next candidates
                  </button>
                </div>
              )}
              <label>
                Interviewer
                <select name="owner" required>
                  <option value="">Choose interviewer</option>
                  {team.data
                    ?.filter((user) => user.role !== 'employee')
                    .map((user) => (
                      <option key={user.id} value={user.id}>
                        {user.label}
                      </option>
                    ))}
                </select>
              </label>
              {!!plan.data?.rounds.length && (
                <label>
                  Interview round
                  <select name="round" required>
                    <option value="">Choose round</option>
                    {plan.data.rounds.map((round) => (
                      <option key={round.id} value={round.id}>
                        {round.name}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <label>
                Location or meeting link
                <input name="location" maxLength={1000} />
              </label>
              {slots.map((slot, index) => (
                <article className="ats-activity" key={slot.id}>
                  <label>
                    Slot {index + 1} start
                    <input
                      type="datetime-local"
                      required
                      value={slot.start}
                      onChange={(event) =>
                        setSlots((previous) =>
                          previous.map((row) =>
                            row.id === slot.id ? { ...row, start: event.target.value } : row,
                          ),
                        )
                      }
                    />
                  </label>
                  <label>
                    Slot {index + 1} end
                    <input
                      type="datetime-local"
                      required
                      value={slot.end}
                      onChange={(event) =>
                        setSlots((previous) =>
                          previous.map((row) =>
                            row.id === slot.id ? { ...row, end: event.target.value } : row,
                          ),
                        )
                      }
                    />
                  </label>
                  {slots.length > 1 && (
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => setSlots((previous) => previous.filter((row) => row.id !== slot.id))}
                    >
                      Remove slot {index + 1}
                    </button>
                  )}
                </article>
              ))}
              <button
                type="button"
                className="button secondary"
                disabled={slots.length >= 20}
                onClick={() =>
                  setSlots((previous) => [...previous, { id: crypto.randomUUID(), start: '', end: '' }])
                }
              >
                Add another slot
              </button>
              <p className="field-hint">
                Times use your device timezone. Offer slots within 14 days, up to 4 hours each. The first
                confirmed booking closes the link. Calendar availability is checked against interviews stored
                in this app.
              </p>
              <ErrorNotice error={createBooking.error || candidates.error || plan.error} />
              <button className="button primary" disabled={candidates.isPending || plan.isPending}>
                Create booking link
              </button>
            </fieldset>
          </form>
        </ConfirmDialog>
      )}
    </div>
  );
}

export function PublicCandidatePage({ booking = false }: { booking?: boolean }) {
  const { linkId } = useParams() as { linkId: string };
  const [success, setSuccess] = useState(false);
  const data = useQuery({
    queryKey: ['public-portal', booking, linkId],
    queryFn: ({ signal }) =>
      api.request<{
        title: string;
        jd_text?: string;
        company?: string;
        location?: string;
        slots?: { index: number; start: string; end: string }[];
      }>('/public/' + (booking ? 'book/' : 'jobs/') + linkId, { signal, public: true }),
  });
  const submit = useMutation({
    mutationFn: (value: File | number) =>
      booking
        ? api.request('/public/book/' + linkId, { method: 'POST', public: true, body: { index: value } })
        : api.request('/public/jobs/' + linkId + '/apply', {
            method: 'POST',
            public: true,
            rawBody: value as File,
            headers: { 'x-filename': encodeURIComponent((value as File).name), 'x-candidate-consent': 'yes' },
          }),
    onSuccess: () => setSuccess(true),
  });
  return (
    <main className="page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">
            {booking ? 'Interview booking' : data.data?.company || 'Job application'}
          </span>
          <h1>{data.data?.title || (booking ? 'Choose an interview time' : 'Apply for this job')}</h1>
        </div>
      </div>
      <ErrorNotice error={data.error || submit.error} retry={() => void data.refetch()} />
      {data.isPending && <Spinner text="Loading candidate page…" />}
      {success ? (
        <section className="panel">
          <h2>{booking ? 'Your interview is booked' : 'Your application was received'}</h2>
          <p>
            {booking
              ? 'The recruiting team can now see your chosen time.'
              : 'The recruiting team will review your resume and application.'}
          </p>
        </section>
      ) : (
        data.data && (
          <section className="panel">
            {data.data.jd_text && <p className="ats-notes">{data.data.jd_text}</p>}
            {data.data.location && <p>Location / meeting: {data.data.location}</p>}
            {booking && !data.data.slots?.length ? (
              <Empty title="No remaining slots">Contact the recruiter for a new booking link.</Empty>
            ) : (
              <form
                className="hr-form"
                onSubmit={(event) => {
                  event.preventDefault();
                  const form = new FormData(event.currentTarget);
                  submit.mutate(booking ? Number(form.get('slot')) : (form.get('resume') as File));
                }}
              >
                <fieldset disabled={submit.isPending}>
                  {booking ? (
                    <>
                      <h2>Available interview times</h2>
                      <p>Times are shown in your device timezone.</p>
                      {data.data.slots?.map((slot) => (
                        <label className="checkbox-label" key={slot.index}>
                          <input type="radio" name="slot" value={slot.index} required />
                          {timestamp(slot.start)} → {timestamp(slot.end)}
                        </label>
                      ))}
                    </>
                  ) : (
                    <>
                      <label>
                        Your resume
                        <input type="file" name="resume" accept=".pdf,.docx,.txt" required />
                      </label>
                      <p className="field-hint">PDF, DOCX or TXT, up to 20 MB.</p>
                      <label className="checkbox-label">
                        <input type="checkbox" required />I agree to this employer processing my resume for
                        this job application.
                      </label>
                    </>
                  )}
                  <button className="button primary">
                    {submit.isPending ? 'Saving…' : booking ? 'Confirm interview time' : 'Submit application'}
                  </button>
                </fieldset>
              </form>
            )}
          </section>
        )
      )}
    </main>
  );
}
