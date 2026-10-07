import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import type { Run, RunInput } from '../api/types';
import { shortId } from '../lib/format';
import { Badge, ErrorNotice, Spinner } from './ui';
import CandidateName from './CandidateName';

export default function RunRoster({
  run,
  selected,
  onSelect,
}: {
  run: Run;
  selected: string[];
  onSelect: (id: string, checked: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const [offset, setOffset] = useState(0);
  const inputs = useQuery({
    queryKey: ['run-inputs', run.id],
    queryFn: ({ signal }) => api.request<RunInput[]>(`/screening-runs/${run.id}/inputs`, { signal }),
    enabled: open,
  });
  const blocked = new Map(
    run.blocked.filter((item) => item.candidate_id).map((item) => [item.candidate_id!, item.reason]),
  );
  const ids = Array.from(
    new Set([...(inputs.data || []).map((item) => item.candidate_id), ...blocked.keys()]),
  );
  return (
    <details className="run-roster" onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>Choose from the full run roster</summary>
      <p className="field-hint">
        Include candidates whose results are pending or who were excluded from this run.
      </p>
      <ErrorNotice
        error={inputs.error}
        retry={() => {
          void inputs.refetch();
        }}
      />
      {inputs.isPending ? (
        <Spinner text="Loading run roster…" />
      ) : (
        inputs.data && (
          <>
            <ul className="roster-list">
              {ids.slice(offset, offset + 20).map((id) => (
                <li key={id}>
                  <label className="check-label">
                    <input
                      type="checkbox"
                      aria-label={`Select candidate ${shortId(id)} from run roster`}
                      checked={selected.includes(id)}
                      disabled={!selected.includes(id) && selected.length >= 5}
                      onChange={(event) => onSelect(id, event.target.checked)}
                    />
                    <span>
                      <CandidateName id={id} />
                      {blocked.has(id) && (
                        <>
                          <Badge value="blocked" />
                          <small>{blocked.get(id)}</small>
                        </>
                      )}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
            <div className="pagination">
              <span>{ids.length} run participants and excluded candidates</span>
              <div>
                <button
                  className="button secondary compact"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - 20))}
                >
                  Previous participants
                </button>
                <button
                  className="button secondary compact"
                  disabled={offset + 20 >= ids.length}
                  onClick={() => setOffset(offset + 20)}
                >
                  Next participants
                </button>
              </div>
            </div>
          </>
        )
      )}
    </details>
  );
}
