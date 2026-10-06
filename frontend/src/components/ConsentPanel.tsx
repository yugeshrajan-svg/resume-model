import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import type { ConsentHistory } from '../api/types';
import { useSession } from '../hooks/useSession';
import { timestamp } from '../lib/format';
import { Badge, ErrorNotice, Spinner } from './ui';
import ConfirmDialog from './ConfirmDialog';

export default function ConsentPanel({ candidateId }: { candidateId: string }) {
  const canWrite = useSession()!.role !== 'hiring_manager';
  const client = useQueryClient();
  const [source, setSource] = useState('');
  const [purpose, setPurpose] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [pool, setPool] = useState(false);
  const [revokeOpen, setRevokeOpen] = useState(false);
  const [notice, setNotice] = useState('');
  const consent = useQuery({
    queryKey: ['consents', candidateId],
    queryFn: ({ signal }) => api.request<ConsentHistory>(`/candidates/${candidateId}/consents`, { signal }),
  });
  async function refresh() {
    await Promise.all([
      client.invalidateQueries({ queryKey: ['consents', candidateId] }),
      client.invalidateQueries({ queryKey: ['candidate', candidateId] }),
      client.invalidateQueries({ queryKey: ['runs'] }),
    ]);
  }
  const grant = useMutation({
    mutationFn: () =>
      api.request(`/candidates/${candidateId}/consents`, {
        method: 'POST',
        body: { source: source.trim(), purpose: purpose.trim(), scope: pool ? 'talent_pool' : 'job' },
      }),
    onSuccess: async () => {
      setSource('');
      setPurpose('');
      setConfirmed(false);
      setPool(false);
      setNotice('Consent recorded.');
      await refresh();
    },
  });
  const revoke = useMutation({
    mutationFn: () => api.request(`/candidates/${candidateId}/consents/revoke`, { method: 'POST' }),
    onSuccess: async () => {
      setRevokeOpen(false);
      setNotice('All active consent grants revoked.');
      await refresh();
    },
  });
  return (
    <section className="panel" aria-label="Candidate consent">
      <div className="section-heading">
        <div>
          <h2>Consent</h2>
          <p>Screening requires an active consent grant.</p>
        </div>
        {consent.data && <Badge value={consent.data.has_active_consent ? 'active' : 'no_active_consent'} />}
      </div>
      <ErrorNotice
        error={consent.error}
        retry={() => {
          void consent.refetch();
        }}
      />
      {notice && (
        <p className="success-message" role="status">
          {notice}
        </p>
      )}
      {consent.isPending ? (
        <Spinner text="Loading consent history…" />
      ) : (
        consent.data &&
        (consent.data.consents.length ? (
          <ol className="history-list">
            {consent.data.consents.map((item) => (
              <li key={item.consent_id}>
                <div>
                  <strong>
                    {item.source}
                    {item.scope === 'talent_pool' && <span className="subtle-chip">Talent pool</span>}
                  </strong>
                  <Badge value={item.active ? 'active' : 'revoked'} />
                </div>
                <p>{item.purpose}</p>
                <small>
                  Granted {timestamp(item.granted_at)}
                  {item.revoked_at && <> · Revoked {timestamp(item.revoked_at)}</>}
                </small>
              </li>
            ))}
          </ol>
        ) : (
          <p className="muted">No consent has been recorded.</p>
        ))
      )}
      {canWrite && (
        <>
          <details className="consent-grant">
            <summary>Record a consent grant</summary>
            <form
              className="hr-form"
              onSubmit={(event) => {
                event.preventDefault();
                setNotice('');
                grant.mutate();
              }}
            >
              <label>
                Grant source
                <input
                  required
                  minLength={2}
                  maxLength={200}
                  value={source}
                  disabled={grant.isPending}
                  onChange={(event) => setSource(event.target.value)}
                />
              </label>
              <label>
                Grant purpose
                <input
                  required
                  minLength={2}
                  maxLength={300}
                  value={purpose}
                  disabled={grant.isPending}
                  onChange={(event) => setPurpose(event.target.value)}
                />
              </label>
              <label className="check-label">
                <input
                  type="checkbox"
                  checked={confirmed}
                  disabled={grant.isPending}
                  onChange={(event) => setConfirmed(event.target.checked)}
                />
                The candidate has given this consent
              </label>
              <label className="check-label">
                <input
                  type="checkbox"
                  checked={pool}
                  disabled={grant.isPending}
                  onChange={(event) => setPool(event.target.checked)}
                />
                Includes consideration for future roles (talent pool)
              </label>
              <ErrorNotice error={grant.error} />
              <button
                className="button primary"
                disabled={
                  grant.isPending || !confirmed || source.trim().length < 2 || purpose.trim().length < 2
                }
              >
                {grant.isPending ? 'Recording…' : 'Record consent'}
              </button>
            </form>
          </details>
          {consent.data?.has_active_consent && (
            <button
              className="button danger secondary"
              disabled={grant.isPending || revoke.isPending}
              onClick={() => {
                revoke.reset();
                setRevokeOpen(true);
              }}
            >
              Revoke active consent
            </button>
          )}
        </>
      )}
      {revokeOpen && (
        <ConfirmDialog
          title="Revoke active consent?"
          onClose={() => {
            if (!revoke.isPending) setRevokeOpen(false);
          }}
        >
          <p>
            This revokes every active grant for this candidate. Future and in-progress screenings cannot score
            them. Historical results remain available.
          </p>
          <ErrorNotice error={revoke.error} />
          <div className="dialog-actions">
            <button
              className="button secondary"
              disabled={revoke.isPending}
              onClick={() => setRevokeOpen(false)}
            >
              Cancel
            </button>
            <button className="button danger" disabled={revoke.isPending} onClick={() => revoke.mutate()}>
              {revoke.isPending ? 'Revoking…' : 'Confirm revocation'}
            </button>
          </div>
        </ConfirmDialog>
      )}
    </section>
  );
}
