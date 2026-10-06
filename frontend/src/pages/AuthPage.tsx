import { useState } from 'react';
import type { FormEvent } from 'react';
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom';
import {
  ArrowRight,
  Check,
  CheckCheck,
  Eye,
  EyeOff,
  FileText,
  LoaderCircle,
  ShieldCheck,
  Sparkles,
} from 'lucide-react';
import { api } from '../api/client';
import { session } from '../api/session';
import type { Tokens } from '../api/types';
import { useSession } from '../hooks/useSession';
import { ErrorNotice } from '../components/ui';

export default function AuthPage({ register = false }: { register?: boolean }) {
  const current = useSession();
  const location = useLocation();
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [showPassword, setShowPassword] = useState(false);
  const from = (location.state as { from?: string } | null)?.from;
  const destination =
    from?.startsWith('/') &&
    !from.startsWith('//') &&
    !from.startsWith('/login') &&
    !from.startsWith('/signup')
      ? from
      : '/jobs';
  if (current) return <Navigate to={destination} replace />;
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError(null);
    try {
      const tokens = await api.request<Tokens>(register ? '/auth/register-company' : '/auth/login', {
        method: 'POST',
        public: true,
        body: register
          ? {
              company_name: String(form.get('company')).trim(),
              admin_email: form.get('email'),
              admin_password: form.get('password'),
            }
          : { email: form.get('email'), password: form.get('password') },
      });
      session.set(tokens);
      navigate(destination, { replace: true });
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="auth-layout">
      <section className="auth-story">
        <Link className="brand" to="/login">
          <span className="brand-mark">
            <Sparkles size={17} />
          </span>
          Shortlist
        </Link>
        <div className="auth-story-content">
          <span className="eyebrow">
            <Sparkles size={14} /> Evidence-based screening
          </span>
          <h1>
            Great people.
            <br />
            <em>Less guesswork.</em>
          </h1>
          <p>
            From the first resume to the right shortlist. A thoughtful workspace that makes every hiring
            decision clearer.
          </p>
          <div className="auth-illustration" aria-hidden="true">
            <div className="illustration-head">
              <FileText size={18} />
              <span>Candidate review</span>
              <CheckCheck size={18} />
            </div>
            <div className="auth-preview-title">A clearer picture of every candidate.</div>
            <div className="auth-preview-row">
              <span className="preview-avatar">01</span>
              <div>
                <strong>Required skills</strong>
                <div className="preview-track">
                  <span />
                </div>
              </div>
              <Check size={17} />
            </div>
            <div className="auth-preview-row">
              <span className="preview-avatar">02</span>
              <div>
                <strong>Relevant experience</strong>
                <div className="preview-track">
                  <span />
                </div>
              </div>
              <Check size={17} />
            </div>
            <div className="auth-preview-row">
              <span className="preview-avatar">03</span>
              <div>
                <strong>Resume evidence</strong>
                <div className="preview-track">
                  <span />
                </div>
              </div>
              <Check size={17} />
            </div>
            <div className="illustration-quote">
              <ShieldCheck size={17} />
              Your judgment. Supported by evidence.
            </div>
          </div>
        </div>
        <div className="auth-story-footer">Built for a more considered shortlist.</div>
      </section>
      <section className="auth-form-area">
        <div className="auth-form">
          <span className="eyebrow">Your recruiting workspace</span>
          <h2>{register ? 'Let’s get you started.' : 'Welcome back.'}</h2>
          <p>
            {register
              ? 'Create your company workspace and admin account.'
              : 'Sign in to pick up where you left off.'}
          </p>
          <form onSubmit={submit}>
            <fieldset disabled={busy}>
              {register && (
                <label>
                  Company name
                  <input
                    name="company"
                    required
                    minLength={2}
                    maxLength={200}
                    autoComplete="organization"
                    placeholder="Your company"
                  />
                </label>
              )}
              <label>
                Work email
                <input
                  name="email"
                  type="email"
                  required
                  autoComplete="username"
                  placeholder="you@company.com"
                />
              </label>
              <label>
                Password
                <div className="password-field">
                  <input
                    name="password"
                    type={showPassword ? 'text' : 'password'}
                    required
                    minLength={register ? 12 : 1}
                    maxLength={256}
                    autoComplete={register ? 'new-password' : 'current-password'}
                    placeholder={register ? 'At least 12 characters' : 'Enter your password'}
                  />
                  <button
                    className="icon-button"
                    type="button"
                    aria-label={showPassword ? 'Hide password' : 'Show password'}
                    aria-pressed={showPassword}
                    onClick={() => setShowPassword(!showPassword)}
                  >
                    {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                  </button>
                </div>
              </label>
              {register && <p className="field-hint">Use at least 12 characters for your admin password.</p>}
              <ErrorNotice error={error} />
              <button className="button primary full-width" type="submit">
                {busy ? <LoaderCircle size={18} className="spin" /> : null}
                {register ? 'Create workspace' : 'Sign in'}
                <ArrowRight size={18} />
              </button>
            </fieldset>
          </form>
          <p className="auth-switch">
            {register ? 'Already have a workspace?' : 'Starting a new workspace?'}{' '}
            <Link to={register ? '/login' : '/signup'} onClick={() => setError(null)}>
              {register ? 'Sign in' : 'Create an account'}
            </Link>
          </p>
          <p className="auth-footnote">
            <ShieldCheck size={15} /> Your company’s private hiring workspace.
          </p>
        </div>
      </section>
    </div>
  );
}
