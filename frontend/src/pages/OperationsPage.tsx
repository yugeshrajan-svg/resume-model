import { useState } from 'react';
import type { FormEvent } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { useSession } from '../hooks/useSession';
import { Badge, Empty, ErrorNotice, Spinner } from '../components/ui';
import ConfirmDialog from '../components/ConfirmDialog';
import { label, shortId, timestamp } from '../lib/format';

type Spec = {
  label: string;
  area: string;
  fields: { name: string; label: string; type: string; required: boolean; options: string[] }[];
};
type Row = {
  id: string;
  kind: string;
  title: string;
  status: string;
  data: Record<string, unknown>;
  history: { actor_id: string; action: string; reason: string; at: string }[];
  assigned_to: string | null;
  created_by: string;
  candidate_id: string | null;
  job_id: string | null;
  updated_at: string;
};
type Member = { id: string; label: string; role: string };
type Choice = {
  id?: string;
  candidate_id?: string;
  name?: string | null;
  title?: string;
  candidate_name?: string | null;
};
const approvalKinds = ['requisition', 'offer_approval'];
function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

export default function OperationsPage() {
  const user = useSession()!;
  const [params, setParams] = useSearchParams();
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<Row | null>(null);
  const [deciding, setDeciding] = useState<{ row: Row; action: string } | null>(null);
  const [offset, setOffset] = useState(0);
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('');
  const [mine, setMine] = useState(false);
  const [notice, setNotice] = useState('');
  const [jobId, setJobId] = useState(params.get('job') || '');
  const [choiceSearch, setChoiceSearch] = useState('');
  const [choiceOffset, setChoiceOffset] = useState(0);
  const client = useQueryClient();
  const catalog = useQuery({
    queryKey: ['hr-catalog'],
    queryFn: ({ signal }) => api.request<Record<string, Spec>>('/hr/catalog', { signal }),
  });
  const kinds = Object.entries(catalog.data || {}).filter(([, spec]) => spec.area === 'hiring');
  const kind = kinds.some(([key]) => key === params.get('kind')) ? params.get('kind')! : kinds[0]?.[0] || '';
  const spec = catalog.data?.[kind];
  const team = useQuery({
    queryKey: ['hr-team'],
    queryFn: ({ signal }) => api.request<Member[]>('/hr/team', { signal }),
  });
  const rows = useQuery({
    queryKey: ['hr-records', kind, offset, search, status, mine],
    queryFn: ({ signal }) =>
      api.request<{ total: number; items: Row[] }>(
        '/hr/records?' +
          new URLSearchParams({ kind, offset: String(offset), q: search, status, mine: String(mine) }),
        { signal },
      ),
    enabled: !!kind,
  });
  const jobs = useQuery({
    queryKey: ['jobs'],
    queryFn: ({ signal }) => api.request<{ id: string; title: string }[]>('/jobs', { signal }),
  });
  const path =
    kind === 'offer_approval'
      ? '/recruiting/activities?' +
        new URLSearchParams({
          kind: 'offer',
          status: 'open',
          q: choiceSearch,
          offset: String(choiceOffset),
          ...(jobId ? { job_id: jobId } : {}),
        })
      : '/jobs/' +
        jobId +
        '/pipeline?' +
        new URLSearchParams({ stage: 'hired', q: choiceSearch, offset: String(choiceOffset) });
  const choices = useQuery({
    queryKey: ['hr-choices', path],
    queryFn: ({ signal }) => api.request<{ total: number; items: Choice[] }>(path, { signal }),
    enabled: adding && (kind === 'offer_approval' || (kind === 'onboarding' && !!jobId)),
  });
  async function refresh() {
    await Promise.all(
      ['hr-records', 'hr-choices', 'jobs', 'recruiting-reports', 'recruiting-activities'].map((key) =>
        client.invalidateQueries({ queryKey: [key] }),
      ),
    );
  }
  async function saved() {
    setAdding(false);
    setEditing(null);
    setNotice('Record saved.');
    await refresh();
  }
  const create = useMutation({
    mutationFn: (body: unknown) => api.request('/hr/records', { method: 'POST', body }),
    onSuccess: saved,
  });
  const edit = useMutation({
    mutationFn: ({ row, body }: { row: Row; body: unknown }) =>
      api.request('/hr/records/' + row.id, { method: 'PUT', body }),
    onSuccess: saved,
  });
  const action = useMutation({
    mutationFn: ({
      row,
      action,
      reason,
      completed_items,
    }: {
      row: Row;
      action: string;
      reason: string;
      completed_items?: number[];
    }) =>
      api.request('/hr/records/' + row.id + '/action', {
        method: 'POST',
        body: { action, reason, completed_items, expected_updated_at: row.updated_at },
      }),
    onSuccess: async () => {
      setDeciding(null);
      setNotice('Action recorded in history.');
      await refresh();
    },
  });
  function selectKind(value: string) {
    setParams({ kind: value });
    setOffset(0);
    setStatus('');
    setSearch('');
    setAdding(false);
    setEditing(null);
    setNotice('');
  }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const data = Object.fromEntries(spec!.fields.map((f) => [f.name, String(form.get(f.name) || '')]));
    if (editing)
      edit.mutate({
        row: editing,
        body: {
          title: form.get('title'),
          data,
          expected_updated_at: editing.updated_at,
          reason: form.get('edit_reason'),
        },
      });
    else
      create.mutate({
        kind,
        title: form.get('title'),
        data,
        assigned_to: form.get('reviewer') || null,
        activity_id: form.get('offer') || null,
        candidate_id: form.get('candidate') || null,
        job_id: ['offer_approval', 'onboarding'].includes(kind) ? jobId || null : null,
      });
  }
  function exportPage() {
    if (!rows.data) return;
    const quote = (value: unknown) => {
      let text = String(value ?? '');
      if (/^\s*[=+@-]/.test(text)) text = "'" + text;
      return '"' + text.replaceAll('"', '""') + '"';
    };
    const data = [
      ['Title', 'Status', ...spec!.fields.map((f) => f.label)],
      ...rows.data.items.map((row) => [row.title, row.status, ...spec!.fields.map((f) => row.data[f.name])]),
    ];
    download(
      new Blob([data.map((row) => row.map(quote).join(',')).join('\r\n')], {
        type: 'text/csv;charset=utf-8',
      }),
      kind + '-page.csv',
    );
  }
  const canManage = ['admin', 'recruiter'].includes(user.role);
  return (
    <div className="page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Hiring operations</span>
          <h1>Hiring workflows</h1>
          <p>Requisitions, approvals, offer templates and new-hire handover.</p>
        </div>
      </div>
      <ErrorNotice
        error={catalog.error || team.error || rows.error || action.error}
        retry={() => void rows.refetch()}
      />
      {notice && (
        <p className="notice success" role="status">
          {notice}
        </p>
      )}
      {catalog.isPending && <Spinner text="Loading hiring workflows…" />}
      <nav className="recruiting-sections" aria-label="Hiring workflow sections">
        {kinds.map(([key, value]) => (
          <button
            className={'button compact ' + (key === kind ? 'primary' : 'secondary')}
            aria-current={key === kind ? 'page' : undefined}
            key={key}
            onClick={() => selectKind(key)}
          >
            {value.label}
          </button>
        ))}
      </nav>
      {spec && (
        <section className="panel">
          <div className="section-heading">
            <h2>{spec.label}</h2>
            <div className="inline-actions">
              <button className="button secondary compact" onClick={() => void refresh()}>
                Refresh
              </button>
              <button
                className="button secondary compact"
                disabled={!rows.data?.items.length}
                onClick={exportPage}
              >
                Export this page
              </button>
              {canManage && (
                <button
                  className="button primary"
                  onClick={() => {
                    create.reset();
                    setEditing(null);
                    setChoiceSearch('');
                    setChoiceOffset(0);
                    setAdding(true);
                  }}
                >
                  Add record
                </button>
              )}
            </div>
          </div>
          {kind === 'offer_template' && (
            <p className="field-hint">
              Use{' '}
              {
                '{{candidate}}, {{job}}, {{amount}}, {{currency}}, {{pay_period}}, {{start_date}}, {{company}}'
              }
              . Download letters from the reviewed offer record.
            </p>
          )}
          <div className="form-grid">
            <label>
              Search records
              <input
                type="search"
                maxLength={200}
                value={search}
                onChange={(e) => {
                  setSearch(e.target.value);
                  setOffset(0);
                }}
              />
            </label>
            <label>
              Filter by status
              <select
                value={status}
                onChange={(e) => {
                  setStatus(e.target.value);
                  setOffset(0);
                }}
              >
                <option value="">All statuses</option>
                {['open', 'pending', 'approved', 'rejected', 'withdrawn', 'completed', 'archived'].map(
                  (value) => (
                    <option key={value} value={value}>
                      {label(value)}
                    </option>
                  ),
                )}
              </select>
            </label>
          </div>
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={mine}
              onChange={(e) => {
                setMine(e.target.checked);
                setOffset(0);
              }}
            />
            Assigned to me
          </label>
          {rows.isPending && <Spinner text="Loading records…" />}
          {rows.data?.total === 0 && (
            <Empty title="No matching records">Add a record or adjust the filters.</Empty>
          )}
          <div className="library-list">
            {rows.data?.items.map((row) => (
              <article className="ats-activity" key={row.id}>
                <div className="section-heading">
                  <h3>{row.title}</h3>
                  <Badge value={row.status} />
                </div>
                <p className="field-hint">
                  Updated {timestamp(row.updated_at)} · Owner:{' '}
                  {team.data?.find((member) => member.id === row.assigned_to)?.label || 'Unassigned'}
                </p>
                {spec.fields.map((f) =>
                  row.data[f.name] ? (
                    <p className="ats-notes" key={f.name}>
                      <strong>{f.label}:</strong> {String(row.data[f.name])}
                    </p>
                  ) : null,
                )}
                {row.data.offer_snapshot !== undefined && (
                  <p>
                    Offer snapshot:{' '}
                    {Object.entries(row.data.offer_snapshot as Record<string, unknown>)
                      .map(([key, value]) => label(key) + ': ' + value)
                      .join(' · ')}
                  </p>
                )}
                {row.candidate_id && (
                  <Link className="text-button" to={'/candidates/' + row.candidate_id}>
                    Open candidate profile
                  </Link>
                )}
                {row.job_id && (
                  <Link className="text-button" to={'/jobs/' + row.job_id}>
                    Open job
                  </Link>
                )}
                {row.kind === 'onboarding' && (
                  <div className="stack">
                    {String(row.data.checklist)
                      .split('\n')
                      .filter((task) => task.trim())
                      .map((task, index) => (
                        <label className="checkbox-label" key={index}>
                          <input
                            type="checkbox"
                            disabled={action.isPending || !canManage}
                            checked={(row.data.completed_items as number[]).includes(index)}
                            onChange={(event) =>
                              action.mutate({
                                row,
                                action: 'checklist',
                                reason: 'Handover checklist updated',
                                completed_items: event.target.checked
                                  ? [...(row.data.completed_items as number[]), index]
                                  : (row.data.completed_items as number[]).filter((i) => i !== index),
                              })
                            }
                          />
                          {task}
                        </label>
                      ))}
                  </div>
                )}
                <div className="inline-actions">
                  {row.status === 'pending' &&
                    row.assigned_to === user.user_id &&
                    row.created_by !== user.user_id &&
                    ['approve', 'reject'].map((value) => (
                      <button
                        className="button secondary compact"
                        key={value}
                        onClick={() => {
                          action.reset();
                          setDeciding({ row, action: value });
                        }}
                      >
                        {label(value)}
                      </button>
                    ))}
                  {row.status === 'pending' && row.created_by === user.user_id && (
                    <button className="text-button" onClick={() => setDeciding({ row, action: 'withdraw' })}>
                      Withdraw request
                    </button>
                  )}
                  {!approvalKinds.includes(kind) && row.status === 'open' && canManage && (
                    <>
                      <button
                        className="button secondary compact"
                        onClick={() => {
                          edit.reset();
                          setEditing(row);
                        }}
                      >
                        Edit record
                      </button>
                      <button
                        className="button secondary compact"
                        onClick={() => setDeciding({ row, action: 'complete' })}
                      >
                        Mark complete
                      </button>
                    </>
                  )}
                </div>
                {!!row.history.length && (
                  <details>
                    <summary>Action history · {row.history.length}</summary>
                    {row.history.map((item, index) => (
                      <p key={index}>
                        {label(item.action)} · {timestamp(item.at)} ·{' '}
                        {team.data?.find((member) => member.id === item.actor_id)?.label ||
                          shortId(item.actor_id)}{' '}
                        · {item.reason}
                      </p>
                    ))}
                  </details>
                )}
              </article>
            ))}
          </div>
          {rows.data && rows.data.total > 25 && (
            <div className="upload-pagination">
              <span>
                {offset + 1}–{Math.min(offset + 25, rows.data.total)} of {rows.data.total}
              </span>
              <button className="button secondary" disabled={!offset} onClick={() => setOffset(offset - 25)}>
                Previous records
              </button>
              <button
                className="button secondary"
                disabled={offset + 25 >= rows.data.total}
                onClick={() => setOffset(offset + 25)}
              >
                Next records
              </button>
            </div>
          )}
        </section>
      )}
      {(adding || editing) && spec && (
        <ConfirmDialog
          title={(editing ? 'Edit ' : 'Add ') + spec.label.toLowerCase() + ' record'}
          onClose={() => {
            if (!create.isPending && !edit.isPending) {
              setAdding(false);
              setEditing(null);
            }
          }}
          initialFocus='[name="title"]'
        >
          <form className="hr-form" key={editing?.id || 'new'} onSubmit={submit}>
            <fieldset disabled={create.isPending || edit.isPending}>
              <label>
                Record title
                <input name="title" defaultValue={editing?.title} minLength={2} maxLength={300} required />
              </label>
              {spec.fields.map((f) => (
                <label key={f.name}>
                  {f.label}
                  {f.type === 'textarea' ? (
                    <textarea
                      name={f.name}
                      defaultValue={String(editing?.data[f.name] || '')}
                      rows={5}
                      maxLength={30000}
                      required={f.required}
                    />
                  ) : (
                    <input
                      name={f.name}
                      defaultValue={String(editing?.data[f.name] || '')}
                      type={f.type}
                      required={f.required}
                      step={f.type === 'number' ? 'any' : undefined}
                      min={f.type === 'number' ? 0 : undefined}
                    />
                  )}
                </label>
              ))}
              {editing ? (
                <label>
                  Reason for this edit
                  <textarea name="edit_reason" minLength={2} maxLength={2000} required />
                </label>
              ) : (
                <>
                  {['onboarding', 'offer_approval'].includes(kind) && (
                    <>
                      <label>
                        Job
                        <select
                          value={jobId}
                          onChange={(event) => {
                            setJobId(event.target.value);
                            setChoiceOffset(0);
                          }}
                          required={kind === 'onboarding'}
                        >
                          <option value="">Choose job</option>
                          {jobs.data?.map((job) => (
                            <option key={job.id} value={job.id}>
                              {job.title}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label>
                        Find {kind === 'onboarding' ? 'hired candidate' : 'offer'}
                        <input
                          type="search"
                          value={choiceSearch}
                          onChange={(event) => {
                            setChoiceSearch(event.target.value);
                            setChoiceOffset(0);
                          }}
                        />
                      </label>
                      <label>
                        {kind === 'onboarding' ? 'Hired candidate' : 'Open offer'}
                        <select
                          key={path}
                          name={kind === 'onboarding' ? 'candidate' : 'offer'}
                          required
                          disabled={kind === 'onboarding' && !jobId}
                        >
                          <option value="">Choose</option>
                          {choices.data?.items.map((row) => (
                            <option
                              key={row.id || row.candidate_id}
                              value={kind === 'onboarding' ? row.candidate_id : row.id}
                            >
                              {row.name || row.title || row.candidate_id}
                            </option>
                          ))}
                        </select>
                      </label>
                      {choices.isPending && (kind === 'offer_approval' || jobId) && (
                        <Spinner text="Loading choices…" />
                      )}
                      <ErrorNotice error={choices.error} />
                      {choices.data && choices.data.total > 50 && (
                        <div className="inline-actions">
                          <button
                            type="button"
                            className="text-button"
                            disabled={!choiceOffset}
                            onClick={() => setChoiceOffset(choiceOffset - 50)}
                          >
                            Previous choices
                          </button>
                          <button
                            type="button"
                            className="text-button"
                            disabled={choiceOffset + 50 >= choices.data.total}
                            onClick={() => setChoiceOffset(choiceOffset + 50)}
                          >
                            Next choices
                          </button>
                        </div>
                      )}
                    </>
                  )}
                  <label>
                    {approvalKinds.includes(kind) ? 'Approval reviewer' : 'Assigned owner (optional)'}
                    <select name="reviewer" required={approvalKinds.includes(kind)}>
                      <option value="">Choose team member</option>
                      {team.data
                        ?.filter(
                          (member) =>
                            !approvalKinds.includes(kind) ||
                            (member.id !== user.user_id &&
                              (kind === 'requisition' || ['admin', 'recruiter'].includes(member.role))),
                        )
                        .map((member) => (
                          <option key={member.id} value={member.id}>
                            {member.label}
                          </option>
                        ))}
                    </select>
                  </label>
                  {approvalKinds.includes(kind) && (
                    <p className="field-hint">
                      The requester cannot approve their own request. Add another authorized reviewer in Team
                      if necessary.
                    </p>
                  )}
                </>
              )}
              <ErrorNotice error={editing ? edit.error : create.error} />
              <div className="dialog-actions">
                <button
                  type="button"
                  className="button secondary"
                  onClick={() => {
                    setAdding(false);
                    setEditing(null);
                  }}
                >
                  Cancel
                </button>
                <button className="button primary">
                  {create.isPending || edit.isPending ? 'Saving…' : editing ? 'Save changes' : 'Save record'}
                </button>
              </div>
            </fieldset>
          </form>
        </ConfirmDialog>
      )}
      {deciding && (
        <ConfirmDialog
          title={label(deciding.action) + ' record'}
          onClose={() => {
            if (!action.isPending) setDeciding(null);
          }}
        >
          <form
            className="hr-form"
            onSubmit={(event) => {
              event.preventDefault();
              action.mutate({ ...deciding, reason: String(new FormData(event.currentTarget).get('reason')) });
            }}
          >
            <p>{deciding.row.title}. This action is recorded in history.</p>
            <label>
              Reason
              <textarea name="reason" minLength={2} maxLength={2000} required disabled={action.isPending} />
            </label>
            <ErrorNotice error={action.error} />
            <button className="button primary" disabled={action.isPending}>
              {label(deciding.action)}
            </button>
          </form>
        </ConfirmDialog>
      )}
    </div>
  );
}
