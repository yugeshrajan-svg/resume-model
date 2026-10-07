import type { ReactNode } from 'react';
import { AlertCircle, ArrowRight, Check, LoaderCircle } from 'lucide-react';
import { message } from '../api/client';
import { label } from '../lib/format';

export function Spinner({ text = 'Loading…' }: { text?: string }) {
  return (
    <div className="loading" role="status">
      <LoaderCircle className="spin" size={20} />
      {text}
    </div>
  );
}
export function ErrorNotice({ error, retry }: { error: unknown; retry?: () => void }) {
  if (!error) return null;
  return (
    <div className="notice error" role="alert">
      <AlertCircle size={18} />
      <div>{message(error)}</div>
      {retry && (
        <button className="text-button" onClick={retry}>
          Try again
        </button>
      )}
    </div>
  );
}
export function Empty({
  title,
  children,
  action,
}: {
  title: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <div className="empty-icon">
        <ArrowRight size={24} />
      </div>
      <h3>{title}</h3>
      <p>{children}</p>
      {action}
    </div>
  );
}
export function Badge({ value }: { value: string }) {
  const color = [
    'ready',
    'completed',
    'confirmed',
    'eligible',
    'shortlisted',
    'active',
    'scored',
    'hired',
    'accepted',
  ].includes(value)
    ? 'green'
    : ['failed', 'needs_reprocess', 'rejected', 'ineligible', 'revoked', 'blocked'].includes(value)
      ? 'red'
      : [
            'needs_ocr',
            'registered',
            'queued',
            'running',
            'draft',
            'review',
            'pending',
            'no_active_consent',
            'interview',
            'offer',
            'open',
          ].includes(value)
        ? 'amber'
        : 'gray';
  return (
    <span className={`badge ${color}`}>
      {color === 'green' && <Check size={11} />}
      {label(value)}
    </span>
  );
}
export function ExplanationView({ explanation }: { explanation: import('../api/types').Explanation | null }) {
  if (!explanation) return null;
  return (
    <div className="explanation-grid">
      <div>
        <h3>Strengths</h3>
        {explanation.strengths?.length ? (
          explanation.strengths.map((item, index) => (
            <div className="explanation-item" key={index}>
              <p>{item.text}</p>
              {item.quote && <blockquote>{item.quote}</blockquote>}
            </div>
          ))
        ) : (
          <p className="muted">No verified strengths reported.</p>
        )}
      </div>
      <div>
        <h3>Gaps to review</h3>
        {explanation.gaps?.length ? (
          explanation.gaps.map((item, index) => (
            <p className="gap" key={index}>
              {item.text}
            </p>
          ))
        ) : (
          <p className="muted">No gaps reported.</p>
        )}
        {explanation.warnings?.map((warning, index) => (
          <p className="notice" key={index}>
            {warning}
          </p>
        ))}
      </div>
    </div>
  );
}
