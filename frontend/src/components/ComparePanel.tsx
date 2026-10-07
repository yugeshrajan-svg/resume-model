import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import type { Comparison } from '../api/types';
import { featureLabels, label, shortId } from '../lib/format';
import CandidateName from './CandidateName';
import { Badge, ErrorNotice, Spinner } from './ui';

export default function ComparePanel({
  jobId,
  runId,
  candidateIds,
}: {
  jobId: string;
  runId: string;
  candidateIds: string[];
}) {
  const comparison = useQuery({
    queryKey: ['compare', jobId, runId, candidateIds],
    queryFn: ({ signal }) =>
      api.request<Comparison>('/candidates/compare', {
        method: 'POST',
        body: { job_id: jobId, run_id: runId, candidate_ids: candidateIds },
        signal,
      }),
    refetchInterval: (query) => (query.state.data && !query.state.data.run_complete ? 2000 : false),
  });
  const data = comparison.data;
  const features = Array.from(
    new Set(data?.candidates.flatMap((candidate) => Object.keys(candidate.features)) || []),
  );
  return (
    <section className="comparison" aria-label="Candidate comparison">
      <div className="section-heading">
        <div>
          <h3>Compare candidates</h3>
          <p>Run {shortId(runId)} · All results use the same requirements and reference date.</p>
        </div>
      </div>
      <ErrorNotice
        error={comparison.error}
        retry={() => {
          void comparison.refetch();
        }}
      />
      {comparison.isPending ? (
        <Spinner text="Loading comparison…" />
      ) : (
        data && (
          <>
            <p className="field-hint" role="status">
              {data.run_complete ? 'Run complete' : 'Screening in progress · Updates automatically'} ·{' '}
              {data.done} of {data.total} processed
            </p>
            <div className="table-scroll">
              <table className="comparison-table">
                <caption className="sr-only">Candidates in screening run {shortId(runId)}</caption>
                <thead>
                  <tr>
                    <th scope="col">Assessment</th>
                    {data.candidates.map((candidate) => (
                      <th scope="col" key={candidate.candidate_id}>
                        <Link to={`/candidates/${candidate.candidate_id}?job=${jobId}&run=${runId}`}>
                          <CandidateName id={candidate.candidate_id} />
                        </Link>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <th scope="row">Status</th>
                    {data.candidates.map((candidate) => (
                      <td key={candidate.candidate_id}>
                        <Badge value={candidate.status} />
                        <p>
                          {candidate.reason ||
                            candidate.reprocess_reason ||
                            (candidate.status === 'pending'
                              ? 'Screening in progress'
                              : candidate.status === 'not_in_run'
                                ? 'Not screened in this run'
                                : '')}
                        </p>
                      </td>
                    ))}
                  </tr>
                  <tr>
                    <th scope="row">Score / 100</th>
                    {data.candidates.map((candidate) => (
                      <td key={candidate.candidate_id}>
                        {candidate.score === null ? 'Unavailable' : candidate.score.toFixed(1)}
                      </td>
                    ))}
                  </tr>
                  <tr>
                    <th scope="row">Eligibility</th>
                    {data.candidates.map((candidate) => (
                      <td key={candidate.candidate_id}>
                        {candidate.eligible === null
                          ? 'Unavailable'
                          : candidate.eligible
                            ? 'Eligible'
                            : 'Ineligible'}
                      </td>
                    ))}
                  </tr>
                  {features.map((feature) => (
                    <tr key={feature}>
                      <th scope="row">{featureLabels[feature] || label(feature)}</th>
                      {data.candidates.map((candidate) => (
                        <td key={candidate.candidate_id}>
                          {candidate.status !== 'scored' || !(feature in candidate.features)
                            ? 'Unavailable'
                            : candidate.features[feature] === null
                              ? 'Not applicable'
                              : `${Math.round(candidate.features[feature] * 100)}%`}
                        </td>
                      ))}
                    </tr>
                  ))}
                  {(['strengths', 'gaps'] as const).map((kind) => (
                    <tr key={kind}>
                      <th scope="row">{label(kind)}</th>
                      {data.candidates.map((candidate) => (
                        <td key={candidate.candidate_id}>
                          {candidate[kind].length ? (
                            <ul>
                              {candidate[kind].map((text, index) => (
                                <li key={index}>{text}</li>
                              ))}
                            </ul>
                          ) : candidate.status === 'scored' ? (
                            'None reported'
                          ) : (
                            'Unavailable'
                          )}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )
      )}
    </section>
  );
}
