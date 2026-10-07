import { useState } from 'react';
import type { FormEvent } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { ArrowRight, CheckCheck, Pencil, Plus, Sparkles, Trash2 } from 'lucide-react';
import { api } from '../api/client';
import type { Job, Requirements, RequirementsVersion, Skill } from '../api/types';
import { useSession } from '../hooks/useSession';
import { date, featureLabels, label } from '../lib/format';
import { Badge, Empty, ErrorNotice, Spinner } from './ui';

export default function RequirementsPanel({
  job,
  versions,
  loading,
  error,
  retry,
}: {
  job: Job;
  versions: RequirementsVersion[];
  loading: boolean;
  error: unknown;
  retry: () => void;
}) {
  const canWrite = useSession()!.role !== 'hiring_manager';
  const client = useQueryClient();
  const [selected, setSelected] = useState<number | null>(null);
  const [editing, setEditing] = useState(false);
  const active = versions.find((version) => version.version === selected) || versions.at(-1);
  const analyze = useMutation({
    mutationFn: () => api.request<RequirementsVersion>(`/jobs/${job.id}/analyze`, { method: 'POST' }),
    onSuccess: (version) => {
      setSelected(version.version);
      setEditing(false);
      void client.invalidateQueries({ queryKey: ['requirements', job.id] });
    },
  });
  const confirm = useMutation({
    mutationFn: (version: number) =>
      api.request<RequirementsVersion>(`/jobs/${job.id}/requirements/confirm`, {
        method: 'POST',
        body: { version },
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['requirements', job.id] });
    },
  });
  const saving = analyze.isPending || confirm.isPending;
  if (loading) return <Spinner text="Loading requirements…" />;
  if (error) return <ErrorNotice error={error} retry={retry} />;
  return (
    <section className="panel requirements-panel">
      <div className="section-heading">
        <div>
          <h2>What does a great fit look like?</h2>
          <p>Check the role and skills, then confirm when you’re ready.</p>
        </div>
        {!!versions.length && (
          <label className="inline-label">
            Version
            <select
              aria-label="Requirements version"
              value={active?.version || ''}
              disabled={editing || saving}
              onChange={(event) => {
                setSelected(Number(event.target.value));
                confirm.reset();
              }}
            >
              {[...versions].reverse().map((version) => (
                <option key={version.id} value={version.version}>
                  v{version.version} · {version.confirmed_at ? 'Confirmed' : 'Draft'}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      <ErrorNotice error={analyze.error || confirm.error} />
      {!active && !editing ? (
        <Empty
          title="Start with the requirements"
          action={
            canWrite ? (
              <div className="button-row">
                <button className="button primary" disabled={saving} onClick={() => analyze.mutate()}>
                  <Sparkles size={17} />
                  {analyze.isPending ? 'Analyzing…' : 'Analyze job description'}
                </button>
                <button className="button secondary" disabled={saving} onClick={() => setEditing(true)}>
                  Enter manually
                </button>
              </div>
            ) : undefined
          }
        >
          Extract requirements from the job description, then review and confirm them. You can also enter them
          manually.
        </Empty>
      ) : editing ? (
        <RequirementsForm
          key={active?.id || 'new'}
          jobId={job.id}
          initial={active?.requirements || blankRequirements(job.title)}
          cancel={() => setEditing(false)}
          saved={(version) => {
            setSelected(version.version);
            setEditing(false);
          }}
        />
      ) : (
        active && (
          <>
            <div className={`requirements-banner ${active.confirmed_at ? 'confirmed' : ''}`}>
              <CheckCheck size={20} />
              <div>
                <strong>
                  {active.confirmed_at ? `Version ${active.version} is confirmed` : 'Ready for your review'}
                </strong>
                <p>
                  {active.confirmed_at
                    ? `Confirmed ${date(active.confirmed_at)}. Edits create a new version.`
                    : 'Check the requirements below. Confirming makes this version available for screening.'}
                </p>
              </div>
              <Badge value={active.confirmed_at ? 'confirmed' : 'draft'} />
            </div>
            <RequirementsSummary requirements={active.requirements} />
            {canWrite && (
              <div className="form-actions split">
                <button
                  className="button secondary"
                  disabled={saving}
                  onClick={() => {
                    confirm.reset();
                    setEditing(true);
                  }}
                >
                  <Pencil size={16} />
                  Edit as new version
                </button>
                <div className="button-row">
                  <button className="text-button" disabled={saving} onClick={() => analyze.mutate()}>
                    {analyze.isPending ? 'Analyzing…' : 'Analyze again'}
                  </button>
                  {!active.confirmed_at && (
                    <button
                      className="button primary"
                      disabled={saving}
                      onClick={() => confirm.mutate(active.version)}
                    >
                      <CheckCheck size={17} />
                      {confirm.isPending ? 'Confirming…' : `Confirm version ${active.version}`}
                    </button>
                  )}
                  {active.confirmed_at && (
                    <Link className="button primary" to="?tab=resumes">
                      Continue to resumes <ArrowRight size={16} />
                    </Link>
                  )}
                </div>
              </div>
            )}
          </>
        )
      )}
    </section>
  );
}
function blankRequirements(role: string): Requirements {
  return {
    role,
    seniority: null,
    experience_min_years: null,
    experience_domain: null,
    required_skills: [],
    preferred_skills: [],
    locations: [],
    max_notice_period_days: null,
    required_skills_min_match: null,
    score_weights: null,
  };
}
function RequirementsSummary({ requirements: req }: { requirements: Requirements }) {
  return (
    <div className="requirements-summary">
      <div className="detail-grid">
        {[
          ['Role', req.role],
          ['Seniority', req.seniority ? label(req.seniority) : 'Not specified'],
          [
            'Minimum experience',
            req.experience_min_years === null ? 'Not specified' : `${req.experience_min_years} years`,
          ],
          ['Experience domain', req.experience_domain || 'Not specified'],
          ['Location', req.locations.join(', ') || 'Not specified'],
          [
            'Maximum notice',
            req.max_notice_period_days === null ? 'Not specified' : `${req.max_notice_period_days} days`,
          ],
        ].map(([name, value]) => (
          <div key={name}>
            <span>{name}</span>
            <strong>{value}</strong>
          </div>
        ))}
      </div>
      {[
        ['Required skills', req.required_skills],
        ['Preferred skills', req.preferred_skills],
      ].map(([title, skills]) => (
        <div className="skill-summary" key={String(title)}>
          <h3>{String(title)}</h3>
          {!(skills as Skill[]).length ? (
            <p className="muted">None specified.</p>
          ) : (
            <div className="skill-chips">
              {(skills as Skill[]).map((skill, index) => (
                <div className="skill-chip" key={index}>
                  <strong>{[skill.name, ...skill.alternatives].join(' or ')}</strong>
                  {skill.evidence && <p>“{skill.evidence}”</p>}
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
      <details className="advanced-settings requirement-scoring-details">
        <summary>
          Screening settings <span className="muted">Thresholds & scoring weights</span>
        </summary>
        <div className="detail-grid">
          <div>
            <span>Required skill threshold</span>
            <strong>
              {req.required_skills_min_match === null
                ? 'Configured default'
                : req.required_skills_min_match === 'all'
                  ? 'All required skills'
                  : `At least ${req.required_skills_min_match} skills`}
            </strong>
          </div>
          <div>
            <span>Score weights</span>
            <strong>{req.score_weights ? 'Custom weights' : 'Configured defaults'}</strong>
          </div>
        </div>
        {req.score_weights && (
          <div className="weights-summary">
            {Object.entries(req.score_weights).map(([name, weight]) => (
              <span key={name}>
                {featureLabels[name] || label(name)} <strong>{weight}</strong>
              </span>
            ))}
          </div>
        )}
      </details>
    </div>
  );
}
function RequirementsForm({
  jobId,
  initial,
  saved,
  cancel,
}: {
  jobId: string;
  initial: Requirements;
  saved: (version: RequirementsVersion) => void;
  cancel: () => void;
}) {
  const client = useQueryClient();
  const [req, setReq] = useState<Requirements>(() => structuredClone(initial));
  const [validation, setValidation] = useState<unknown>(null);
  const save = useMutation({
    mutationFn: (body: Requirements) =>
      api.request<RequirementsVersion>(`/jobs/${jobId}/requirements`, { method: 'PUT', body }),
    onSuccess: (version) => {
      client.setQueryData<RequirementsVersion[]>(['requirements', jobId], (previous) => [
        ...(previous || []),
        version,
      ]);
      saved(version);
    },
  });
  function set<K extends keyof Requirements>(key: K, value: Requirements[K]) {
    setReq((previous) => ({ ...previous, [key]: value }));
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    if (save.isPending) return;
    setValidation(null);
    if (
      !req.role.trim() ||
      [...req.required_skills, ...req.preferred_skills].some((skill) => !skill.name.trim())
    ) {
      setValidation(new Error('Enter a role and a name for every skill.'));
      return;
    }
    if (
      typeof req.required_skills_min_match === 'number' &&
      req.required_skills_min_match > req.required_skills.length
    ) {
      setValidation(new Error('The required skill threshold cannot exceed the number of required skills.'));
      return;
    }
    if (req.score_weights && Object.values(req.score_weights).reduce((a, b) => a + b, 0) <= 0) {
      setValidation(new Error('At least one score weight must be greater than zero.'));
      return;
    }
    save.mutate({
      ...req,
      role: req.role.trim(),
      required_skills: req.required_skills.map((skill) => ({ ...skill, name: skill.name.trim() })),
      preferred_skills: req.preferred_skills.map((skill) => ({ ...skill, name: skill.name.trim() })),
    });
  }
  const number = (value: string) => (value === '' ? null : Number(value));
  return (
    <form className="requirements-form" onSubmit={submit}>
      <fieldset disabled={save.isPending}>
        <div className="notice">Saving creates a new draft version. Review and confirm it before use.</div>
        <div className="form-grid">
          <label>
            Role
            <input value={req.role} required onChange={(event) => set('role', event.target.value)} />
          </label>
          <label>
            Seniority
            <select
              value={req.seniority || ''}
              onChange={(event) => set('seniority', event.target.value || null)}
            >
              <option value="">Not specified</option>
              {['intern', 'junior', 'mid', 'senior', 'lead', 'principal'].map((level) => (
                <option key={level} value={level}>
                  {label(level)}
                </option>
              ))}
            </select>
          </label>
          <label>
            Minimum years of experience
            <input
              type="number"
              min={0}
              step="0.1"
              value={req.experience_min_years ?? ''}
              onChange={(event) => set('experience_min_years', number(event.target.value))}
              placeholder="Not specified"
            />
          </label>
          <label>
            Experience domain
            <input
              value={req.experience_domain || ''}
              onChange={(event) => set('experience_domain', event.target.value || null)}
              placeholder="e.g. Backend engineering"
            />
          </label>
          <label>
            Accepted locations <span className="field-hint">Separate with commas</span>
            <CommaInput
              values={req.locations}
              update={(values) => set('locations', values)}
              placeholder="e.g. Bengaluru, Remote"
            />
          </label>
          <label>
            Maximum notice period (days)
            <input
              type="number"
              min={0}
              step={1}
              value={req.max_notice_period_days ?? ''}
              onChange={(event) => set('max_notice_period_days', number(event.target.value))}
              placeholder="Not specified"
            />
          </label>
        </div>
        <SkillEditor
          title="Required skills"
          skills={req.required_skills}
          update={(skills) => set('required_skills', skills)}
        />
        <SkillEditor
          title="Preferred skills"
          skills={req.preferred_skills}
          update={(skills) => set('preferred_skills', skills)}
        />
        <label>
          Required skill threshold
          <select
            value={req.required_skills_min_match === null ? 'default' : String(req.required_skills_min_match)}
            onChange={(event) =>
              set(
                'required_skills_min_match',
                event.target.value === 'default'
                  ? null
                  : event.target.value === 'all'
                    ? 'all'
                    : Number(event.target.value),
              )
            }
          >
            <option value="default">Use configured default</option>
            <option value="all">Match all required skills</option>
            {req.required_skills.map((_, index) => (
              <option key={index} value={index + 1}>
                Match at least {index + 1}
              </option>
            ))}
          </select>
        </label>
        <details className="advanced-settings">
          <summary>Scoring weights</summary>
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={req.score_weights !== null}
              onChange={(event) =>
                set(
                  'score_weights',
                  event.target.checked
                    ? Object.fromEntries(Object.keys(featureLabels).map((name) => [name, 1]))
                    : null,
                )
              }
            />
            Use custom weights for this role
          </label>
          <p className="field-hint">
            Larger weights give a feature more influence. Weights are normalized when scoring.
          </p>
          {req.score_weights && (
            <div className="form-grid">
              {Object.keys(featureLabels).map((name) => (
                <label key={name}>
                  {featureLabels[name]}
                  <input
                    type="number"
                    min={0}
                    step="0.01"
                    required
                    value={req.score_weights?.[name] ?? 0}
                    onChange={(event) =>
                      set('score_weights', { ...req.score_weights, [name]: Number(event.target.value) })
                    }
                  />
                </label>
              ))}
            </div>
          )}
        </details>
        <ErrorNotice error={validation || save.error} />
        <div className="form-actions">
          <button className="button secondary" type="button" onClick={cancel}>
            Cancel
          </button>
          <button className="button primary" type="submit">
            {save.isPending ? 'Saving…' : 'Save new version'}
          </button>
        </div>
      </fieldset>
    </form>
  );
}
function SkillEditor({
  title,
  skills,
  update,
}: {
  title: string;
  skills: Skill[];
  update: (skills: Skill[]) => void;
}) {
  function change(index: number, patch: Partial<Skill>) {
    update(skills.map((skill, position) => (position === index ? { ...skill, ...patch } : skill)));
  }
  return (
    <div className="skill-editor">
      <div className="section-heading">
        <h3>{title}</h3>
        <button
          type="button"
          className="text-button"
          onClick={() => update([...skills, { name: '', alternatives: [], evidence: null }])}
        >
          <Plus size={15} />
          Add skill
        </button>
      </div>
      {skills.map((skill, index) => (
        <div className="skill-editor-row" key={index}>
          <label>
            Skill
            <input
              aria-label={`${title} ${index + 1} name`}
              value={skill.name}
              required
              onChange={(event) => change(index, { name: event.target.value, evidence: null })}
            />
          </label>
          <label>
            Accepted alternatives
            <CommaInput
              ariaLabel={`${title} ${index + 1} alternatives`}
              values={skill.alternatives}
              update={(values) => change(index, { alternatives: values, evidence: null })}
              placeholder="Optional, comma separated"
            />
          </label>
          <button
            type="button"
            className="icon-button"
            aria-label={`Remove ${title.toLowerCase()} ${index + 1}`}
            onClick={() => update(skills.filter((_, position) => position !== index))}
          >
            <Trash2 size={17} />
          </button>
          {skill.evidence && <p className="skill-evidence">Job description: “{skill.evidence}”</p>}
        </div>
      ))}
    </div>
  );
}
function CommaInput({
  values,
  update,
  placeholder,
  ariaLabel,
}: {
  values: string[];
  update: (values: string[]) => void;
  placeholder?: string;
  ariaLabel?: string;
}) {
  const [text, setText] = useState(values.join(', '));
  // The values this text last produced. Resync only when values change from outside: a value that
  // itself contains a comma never round-trips through the text, so comparing against parse(text) loops.
  const [synced, setSynced] = useState(values.join('\0'));
  const parse = (value: string) =>
    value
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean);
  if (values.join('\0') !== synced) {
    setSynced(values.join('\0'));
    setText(values.join(', '));
  }
  return (
    <input
      aria-label={ariaLabel}
      placeholder={placeholder}
      value={text}
      onChange={(event) => {
        const parsed = parse(event.target.value);
        setText(event.target.value);
        setSynced(parsed.join('\0'));
        update(parsed);
      }}
    />
  );
}
