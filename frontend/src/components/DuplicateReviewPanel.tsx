import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { useSession } from '../hooks/useSession';
import { Empty, ErrorNotice, Spinner } from './ui';
import ConfirmDialog from './ConfirmDialog';
export default function DuplicateReviewPanel() {
  const admin = useSession()!.role === 'admin';
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const [source, setSource] = useState('');
  const [target, setTarget] = useState('');
  const [reason, setReason] = useState('');
  const [notice, setNotice] = useState('');
  const client = useQueryClient();
  const cases = useQuery({
    queryKey: ['duplicate-cases', search, offset],
    queryFn: ({ signal }) =>
      api.request<{
        total: number;
        items: {
          name: string;
          count: number;
          profiles: { id: string; name: string; email: string | null; phone: string | null }[];
        }[];
      }>('/talent-pool/duplicates?' + new URLSearchParams({ q: search, offset: String(offset) }), { signal }),
  });
  const preview = useMutation({
    mutationFn: (body: { source_id: string; target_id: string; reason: string }) =>
      api.request<{
        can_merge: boolean;
        source_resumes: number;
        source_applications: number;
        conflicting_jobs: string[];
        active_processing: boolean;
        unfinished_resumes: boolean;
      }>('/candidates/merge/preview', { method: 'POST', body }),
  });
  const merge = useMutation({
    mutationFn: () =>
      api.request('/candidates/merge', { method: 'POST', body: { ...preview.variables, confirmed: true } }),
    onSuccess: async () => {
      preview.reset();
      setSource('');
      setTarget('');
      setReason('');
      setNotice('Profiles consolidated. Resume files, applications and consent history were preserved.');
      await Promise.all(
        ['duplicate-cases', 'candidate', 'talent-pool-resumes', 'pipeline'].map((key) =>
          client.invalidateQueries({ queryKey: [key] }),
        ),
      );
    },
  });
  return (
    <section className="panel">
      <h2>Possible duplicate profiles</h2>
      <p>
        Matching names are review hints. Compare the original resumes and contact details before deciding
        whether profiles represent the same person.
      </p>
      <label>
        Search duplicate names
        <input
          type="search"
          maxLength={200}
          value={search}
          onChange={(event) => {
            setSearch(event.target.value);
            setOffset(0);
          }}
        />
      </label>
      <ErrorNotice error={cases.error || preview.error || merge.error} retry={() => void cases.refetch()} />
      {notice && (
        <p className="notice success" role="status">
          {notice}
        </p>
      )}
      {cases.isPending && <Spinner text="Checking duplicate names…" />}
      {cases.data?.total === 0 && (
        <Empty title="No matching-name groups">
          An administrator can still review two specific profile IDs below.
        </Empty>
      )}
      {cases.data?.items.map((group) => (
        <article className="ats-activity" key={group.name}>
          <h3>
            {group.name} · {group.count} profiles
          </h3>
          {group.profiles.map((profile) => (
            <div key={profile.id} className="ats-activity">
              <Link className="candidate-name" to={'/candidates/' + profile.id}>
                {profile.name} · Open profile
              </Link>
              <p>
                {profile.email || 'No email'} · {profile.phone || 'No phone'}
              </p>
              {admin && (
                <div className="inline-actions">
                  <button className="text-button" onClick={() => setSource(profile.id)}>
                    Move this profile
                  </button>
                  <button className="text-button" onClick={() => setTarget(profile.id)}>
                    Keep this profile
                  </button>
                </div>
              )}
            </div>
          ))}
          {group.count > 20 && (
            <p>Showing the first 20 profiles. Refine the search or review specific profile IDs.</p>
          )}
        </article>
      ))}
      {cases.data && cases.data.total > 25 && (
        <div className="upload-pagination">
          <button className="button secondary" disabled={!offset} onClick={() => setOffset(offset - 25)}>
            Previous groups
          </button>
          <button
            className="button secondary"
            disabled={offset + 25 >= cases.data.total}
            onClick={() => setOffset(offset + 25)}
          >
            Next groups
          </button>
        </div>
      )}
      {admin && (
        <form
          className="hr-form"
          onSubmit={(event) => {
            event.preventDefault();
            merge.reset();
            preview.mutate({ source_id: source, target_id: target, reason });
          }}
        >
          <h3>Review two profiles</h3>
          <label>
            Source profile ID
            <input value={source} required onChange={(event) => setSource(event.target.value)} />
          </label>
          <label>
            Profile ID to keep
            <input value={target} required onChange={(event) => setTarget(event.target.value)} />
          </label>
          <label>
            Why are these the same person?
            <textarea
              value={reason}
              minLength={10}
              maxLength={2000}
              required
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          <button className="button secondary" disabled={preview.isPending}>
            Review consolidation
          </button>
        </form>
      )}
      {preview.data && (
        <ConfirmDialog
          title="Review profile consolidation"
          onClose={() => {
            if (!merge.isPending) preview.reset();
          }}
        >
          <p>
            Move profile: <code>{preview.variables?.source_id}</code>
          </p>
          <p>
            Keep profile: <code>{preview.variables?.target_id}</code>
          </p>
          <p>Recorded reason: {preview.variables?.reason}</p>
          <p>
            Move {preview.data.source_resumes} resume files and {preview.data.source_applications}{' '}
            applications to the profile being kept. Its existing contact details remain the primary details.
          </p>
          {!preview.data.can_merge ? (
            <p className="notice error">
              Consolidation is blocked by overlapping job applications or unfinished processing. Both profiles
              remain available so their history is preserved.
            </p>
          ) : (
            <form
              className="hr-form"
              onSubmit={(event) => {
                event.preventDefault();
                merge.mutate();
              }}
            >
              <p>
                The source profile will be removed. Its stored history moves to the retained profile. This
                consolidation cannot be undone.
              </p>
              <label className="checkbox-label">
                <input type="checkbox" required disabled={merge.isPending} />I verified that these are the
                same person and reviewed both profiles.
              </label>
              <ErrorNotice error={merge.error} />
              <button className="button primary" disabled={merge.isPending}>
                Confirm consolidation
              </button>
            </form>
          )}
        </ConfirmDialog>
      )}
    </section>
  );
}
