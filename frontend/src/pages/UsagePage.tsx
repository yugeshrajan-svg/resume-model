import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import type { Usage, UsageMetrics } from '../api/types';
import { label, shortId, usd } from '../lib/format';
import { defaultUsageRange, usageDays, validUsageRange } from '../lib/usage';
import { Empty, ErrorNotice, Spinner } from '../components/ui';

function MetricCells({ metrics }: { metrics: UsageMetrics }) {
  return (
    <>
      <td>{metrics.prompt_tokens.toLocaleString()}</td>
      <td>{metrics.completion_tokens.toLocaleString()}</td>
      <td>{metrics.ocr_pages.toLocaleString()}</td>
      <td>{usd(metrics.cost_usd)}</td>
    </>
  );
}
function MetricHeaders() {
  return (
    <>
      <th>Prompt tokens</th>
      <th>Completion tokens</th>
      <th>OCR pages</th>
      <th>Cost (USD)</th>
    </>
  );
}
export default function UsagePage() {
  const [range, setRange] = useState(defaultUsageRange);
  const [draft, setDraft] = useState(range);
  const valid = validUsageRange(draft.from, draft.to);
  const params = new URLSearchParams();
  if (range.from) params.set('from', `${range.from}T00:00:00Z`);
  if (range.to) params.set('to', `${range.to}T00:00:00Z`);
  const usage = useQuery({
    queryKey: ['usage', range.from, range.to],
    queryFn: ({ signal }) => api.request<Usage>(`/usage?${params}`, { signal }),
  });
  const data = usage.data;
  const days = data ? usageDays(data) : [];
  const maximum = Math.max(0, ...days.map((day) => day.cost_usd));
  return (
    <div className="page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Workspace admin</span>
          <h1>Usage & cost.</h1>
          <p>Actual model and OCR usage across your company, reported in UTC.</p>
        </div>
      </div>
      <section className="panel">
        <form
          className="usage-filters"
          onSubmit={(event) => {
            event.preventDefault();
            if (valid) setRange({ ...draft });
          }}
        >
          <label>
            Start date (inclusive, UTC)
            <input
              type="date"
              value={draft.from}
              onChange={(event) => setDraft({ ...draft, from: event.target.value })}
            />
          </label>
          <label>
            End date (exclusive, UTC)
            <input
              type="date"
              value={draft.to}
              onChange={(event) => setDraft({ ...draft, to: event.target.value })}
            />
          </label>
          <button className="button primary" disabled={!valid || usage.isFetching}>
            Apply dates
          </button>
          <button
            type="button"
            className="button secondary"
            onClick={() => {
              const all = { from: '', to: '' };
              setDraft(all);
              setRange(all);
            }}
          >
            All time
          </button>
        </form>
        {!valid && (
          <p role="alert" className="notice error">
            Enter valid dates with the start earlier than the exclusive end date.
          </p>
        )}
        <ErrorNotice
          error={usage.error}
          retry={() => {
            void usage.refetch();
          }}
        />
        {usage.isPending ? (
          <Spinner text="Loading usage…" />
        ) : (
          data && (
            <>
              <p className="field-hint">
                Showing {range.from || 'all earlier dates'} through{' '}
                {range.to ? `${range.to} (excluded)` : 'the latest activity'}.
              </p>
              <div className="usage-metrics">
                <div>
                  <span>Recorded cost</span>
                  <strong>{usd(data.totals.cost_usd)}</strong>
                  <small>USD</small>
                </div>
                <div>
                  <span>Prompt tokens</span>
                  <strong>{data.totals.prompt_tokens.toLocaleString()}</strong>
                </div>
                <div>
                  <span>Completion tokens</span>
                  <strong>{data.totals.completion_tokens.toLocaleString()}</strong>
                </div>
                <div>
                  <span>OCR pages</span>
                  <strong>{data.totals.ocr_pages.toLocaleString()}</strong>
                </div>
              </div>
              <h2>Daily usage</h2>
              {!data.by_day.length ? (
                <Empty title="No usage recorded in this period">
                  Usage appears here after model or OCR activity is recorded.
                </Empty>
              ) : (
                <>
                  {maximum > 0 && (
                    <figure className="usage-chart">
                      <svg
                        viewBox="0 0 700 160"
                        role="img"
                        aria-label={`Daily recorded cost in USD, maximum ${usd(maximum)}`}
                      >
                        <line x1="0" y1="154" x2="700" y2="154" stroke="currentColor" />
                        {days.map((day, index) => (
                          <rect
                            key={day.date}
                            x={
                              ((index + 0.5) * 700) / days.length -
                              Math.min(32, Math.max(0.5, 700 / days.length - 2)) / 2
                            }
                            y={154 - (day.cost_usd / maximum) * 144}
                            width={Math.min(32, Math.max(0.5, 700 / days.length - 2))}
                            height={(day.cost_usd / maximum) * 144}
                            fill="var(--chart)"
                          >
                            <title>
                              {day.date}: {usd(day.cost_usd)}
                            </title>
                          </rect>
                        ))}
                      </svg>
                      <div className="chart-dates" aria-hidden="true">
                        <span>{days[0]?.date}</span>
                        <span>{days.at(-1)?.date}</span>
                      </div>
                      <figcaption>
                        Recorded daily cost (USD); peak {usd(maximum)}.{' '}
                        {range.from && range.to && days.length <= 366 && days.length !== data.by_day.length
                          ? 'Days without usage are shown as zero.'
                          : 'Each bar represents a reported UTC date.'}{' '}
                        Exact values appear below.
                      </figcaption>
                    </figure>
                  )}
                  <div className="table-scroll" tabIndex={0} role="region" aria-label="Daily usage table">
                    <table className="candidate-table">
                      <thead>
                        <tr>
                          <th>UTC date</th>
                          <MetricHeaders />
                        </tr>
                      </thead>
                      <tbody>
                        {data.by_day.map((day) => (
                          <tr key={day.date}>
                            <th scope="row">{day.date}</th>
                            <MetricCells metrics={day} />
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
              {!!Object.keys(data.by_kind).length && (
                <>
                  <h2>By activity</h2>
                  <div
                    className="table-scroll"
                    tabIndex={0}
                    role="region"
                    aria-label="Usage by activity table"
                  >
                    <table className="candidate-table">
                      <thead>
                        <tr>
                          <th>Activity</th>
                          <MetricHeaders />
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(data.by_kind).map(([kind, metrics]) => (
                          <tr key={kind}>
                            <th scope="row">{label(kind)}</th>
                            <MetricCells metrics={metrics} />
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
              {!!data.runs.length && (
                <>
                  <h2>By screening run</h2>
                  <div
                    className="table-scroll"
                    tabIndex={0}
                    role="region"
                    aria-label="Usage by screening run table"
                  >
                    <table className="candidate-table">
                      <thead>
                        <tr>
                          <th>Run</th>
                          <MetricHeaders />
                        </tr>
                      </thead>
                      <tbody>
                        {data.runs.map((run) => (
                          <tr key={run.run_id}>
                            <th scope="row">
                              <Link to={`/jobs/${run.job_id}?tab=results&run=${run.run_id}`}>
                                {shortId(run.run_id)}
                              </Link>
                            </th>
                            <MetricCells metrics={run} />
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </>
          )
        )}
      </section>
    </div>
  );
}
