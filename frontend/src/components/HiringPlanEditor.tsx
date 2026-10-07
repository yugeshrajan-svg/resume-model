import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { ErrorNotice, Spinner } from './ui';
export type HiringPlan = { version: number; rounds: { id: string; name: string; criteria: string[] }[] };
export default function HiringPlanEditor({ jobId, canManage }: { jobId: string; canManage: boolean }) {
  const client = useQueryClient();
  const [draft, setDraft] = useState<HiringPlan | null>(null);
  const plan = useQuery({
    queryKey: ['hiring-plan', jobId],
    queryFn: ({ signal }) => api.request<HiringPlan>('/jobs/' + jobId + '/hiring-plan', { signal }),
  });
  const save = useMutation({
    mutationFn: (body: HiringPlan) =>
      api.request('/jobs/' + jobId + '/hiring-plan', {
        method: 'PUT',
        body: {
          expected_version: body.version,
          rounds: body.rounds.map((round) => ({
            ...round,
            name: round.name.trim(),
            criteria: round.criteria.map((criterion) => criterion.trim()).filter(Boolean),
          })),
        },
      }),
    onSuccess: async () => {
      setDraft(null);
      await client.invalidateQueries({ queryKey: ['hiring-plan', jobId] });
    },
  });
  function update(index: number, changes: Partial<HiringPlan['rounds'][number]>) {
    setDraft(
      (previous) =>
        previous && {
          ...previous,
          rounds: previous.rounds.map((row, i) => (i === index ? { ...row, ...changes } : row)),
        },
    );
  }
  return (
    <section aria-label="Interview plan">
      <h2>Interview rounds and scorecards</h2>
      <p>
        Set the rounds and criteria for this job. Every feedback record keeps the plan version used at the
        time.
      </p>
      <ErrorNotice error={plan.error || save.error} retry={() => void plan.refetch()} />
      {plan.isPending && <Spinner text="Loading interview plan…" />}
      {!draft && plan.data && (
        <>
          <p>Plan version {plan.data.version || 'not configured'}</p>
          {plan.data.rounds.map((row, i) => (
            <article className="ats-activity" key={row.id}>
              <h3>
                {i + 1}. {row.name}
              </h3>
              <p>{row.criteria.join(' · ')}</p>
            </article>
          ))}
          {canManage && (
            <button
              className="button primary"
              onClick={() => {
                save.reset();
                setDraft(plan.data!);
              }}
            >
              Configure interview plan
            </button>
          )}
        </>
      )}
      {draft && (
        <form
          className="hr-form"
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate(draft);
          }}
        >
          <fieldset disabled={save.isPending}>
            {draft.rounds.map((row, index) => (
              <article className="ats-activity" key={row.id}>
                <label>
                  Round {index + 1} name
                  <input
                    value={row.name}
                    minLength={2}
                    maxLength={100}
                    required
                    onChange={(e) => update(index, { name: e.target.value })}
                  />
                </label>
                <label>
                  Round {index + 1} criteria (one per line)
                  <textarea
                    value={row.criteria.join('\n')}
                    required
                    onChange={(e) => update(index, { criteria: e.target.value.split('\n') })}
                  />
                </label>
                <button
                  className="text-button"
                  type="button"
                  onClick={() => setDraft({ ...draft, rounds: draft.rounds.filter((_, i) => i !== index) })}
                >
                  Remove round {index + 1}
                </button>
              </article>
            ))}
            <button
              className="button secondary"
              type="button"
              disabled={draft.rounds.length >= 12}
              onClick={() =>
                setDraft({
                  ...draft,
                  rounds: [...draft.rounds, { id: crypto.randomUUID(), name: '', criteria: [''] }],
                })
              }
            >
              Add interview round
            </button>
            <p className="field-hint">
              Use 1–12 rounds, each with 1–12 distinct criteria. Names and criteria can be edited in a new
              version.
            </p>
            <div className="dialog-actions">
              <button className="button secondary" type="button" onClick={() => setDraft(null)}>
                Cancel
              </button>
              <button className="button primary" disabled={!draft.rounds.length}>
                Save interview plan
              </button>
            </div>
          </fieldset>
        </form>
      )}
    </section>
  );
}
