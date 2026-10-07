import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import type { Company } from '../api/types';
import { ErrorNotice, Spinner } from '../components/ui';
import ConfirmDialog from '../components/ConfirmDialog';

export default function SettingsPage() {
  const client = useQueryClient();
  const [draft, setDraft] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<number | null>(null);
  const [saved, setSaved] = useState(false);
  const company = useQuery({
    queryKey: ['company'],
    queryFn: ({ signal }) => api.request<Company>('/company', { signal }),
  });
  const update = useMutation({
    mutationFn: (days: number) =>
      api.request<Company>('/company', { method: 'PATCH', body: { retention_days: days } }),
    onSuccess: (data) => {
      client.setQueryData(['company'], data);
      setDraft(null);
      setConfirm(null);
      setSaved(true);
    },
  });
  const data = company.data;
  const value = draft ?? String(data?.retention_days ?? '');
  const valid = /^\d+$/.test(value) && Number(value) >= 1 && Number(value) <= 3650;
  return (
    <div className="page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Workspace admin</span>
          <h1>Retention & settings.</h1>
          <p>Keep candidate information for as long as your hiring process needs it.</p>
        </div>
      </div>
      <ErrorNotice
        error={company.error}
        retry={() => {
          void company.refetch();
        }}
      />
      {company.isPending ? (
        <Spinner text="Loading company settings…" />
      ) : (
        data && (
          <div className="admin-layout">
            <section className="panel">
              <h2>{data.name}</h2>
              <div className="settings-metrics">
                <div>
                  <strong>{data.candidates_total}</strong>
                  <span>Candidates stored</span>
                </div>
                <div>
                  <strong>{data.candidates_past_retention}</strong>
                  <span>Past the current retention period</span>
                </div>
              </div>
              <p>
                The retention worker runs daily at {data.retention_sweep_hour_utc.toString().padStart(2, '0')}
                :00 UTC.
              </p>
              <p className="field-hint">
                Counts reflect the saved {data.retention_days}-day setting. The worker rechecks candidate
                activity before erasure.
              </p>
            </section>
            <section className="panel">
              <h2>Candidate retention</h2>
              <form
                className="hr-form"
                onSubmit={(event) => {
                  event.preventDefault();
                  if (!valid) return;
                  setSaved(false);
                  update.reset();
                  if (Number(value) < data.retention_days) setConfirm(Number(value));
                  else update.mutate(Number(value));
                }}
              >
                <label>
                  Retention days
                  <input
                    type="number"
                    required
                    min={1}
                    max={3650}
                    step={1}
                    value={value}
                    disabled={update.isPending}
                    onChange={(event) => {
                      setDraft(event.target.value);
                      setSaved(false);
                    }}
                  />
                </label>
                <p className="field-hint">
                  Between 1 and 3,650 days since the candidate's latest activity. Saving schedules no
                  immediate deletion; the daily retention sweep applies this setting.
                </p>
                <ErrorNotice error={confirm === null ? update.error : null} />
                {saved && (
                  <p role="status" className="success-message">
                    Retention settings saved.
                  </p>
                )}
                <button
                  className="button primary"
                  disabled={update.isPending || !valid || Number(value) === data.retention_days}
                >
                  {update.isPending ? 'Saving…' : 'Save retention settings'}
                </button>
              </form>
            </section>
          </div>
        )
      )}
      {confirm !== null && data && (
        <ConfirmDialog
          title="Shorten candidate retention?"
          onClose={() => {
            if (!update.isPending) setConfirm(null);
          }}
        >
          <p>
            Change retention from {data.retention_days} to {confirm} days. The next daily sweep may
            permanently erase more inactive candidates.
          </p>
          <p>
            {data.candidates_past_retention} candidates are past the current {data.retention_days}-day limit.
            The count for the proposed {confirm}-day limit is not available until it is saved.
          </p>
          <ErrorNotice error={update.error} />
          <div className="dialog-actions">
            <button className="button secondary" disabled={update.isPending} onClick={() => setConfirm(null)}>
              Cancel
            </button>
            <button
              className="button danger"
              disabled={update.isPending}
              onClick={() => update.mutate(confirm)}
            >
              {update.isPending ? 'Saving…' : 'Confirm shorter retention'}
            </button>
          </div>
        </ConfirmDialog>
      )}
    </div>
  );
}
