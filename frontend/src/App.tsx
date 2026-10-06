import { Link, Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom';
import {
  ArrowUpRight,
  BookOpen,
  BriefcaseBusiness,
  LogOut,
  PanelLeftClose,
  X,
  Users,
  Settings,
  ChartColumn,
  ChevronRight,
  CircleHelp,
  Sparkles,
  UserSearch,
} from 'lucide-react';
import { useSession } from './hooks/useSession';
import { session } from './api/session';
import { label } from './lib/format';
import AuthPage from './pages/AuthPage';
import JobsPage from './pages/JobsPage';
import JobPage from './pages/JobPage';
import CandidatePage from './pages/CandidatePage';
import TeamPage from './pages/TeamPage';
import SettingsPage from './pages/SettingsPage';
import UsagePage from './pages/UsagePage';
import TalentPoolPage from './pages/TalentPoolPage';
import { useEffect, useRef, useState } from 'react';

function Workspace() {
  const current = useSession();
  const location = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const sidebar = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!menuOpen) return;
    const previous = document.activeElement as HTMLElement | null;
    sidebar.current?.querySelector<HTMLButtonElement>('button')?.focus();
    return () => previous?.focus();
  }, [menuOpen]);
  if (!current) return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <aside
        ref={sidebar}
        className={`sidebar ${menuOpen ? 'open' : ''}`}
        role={menuOpen ? 'dialog' : undefined}
        aria-modal={menuOpen || undefined}
        aria-label={menuOpen ? 'Workspace navigation' : undefined}
        onKeyDown={(event) => {
          if (!menuOpen) return;
          if (event.key === 'Escape') {
            event.preventDefault();
            setMenuOpen(false);
          }
          if (event.key === 'Tab') {
            const elements = sidebar.current?.querySelectorAll<HTMLElement>('a[href], button:not(:disabled)');
            if (!elements?.length) return;
            const first = elements[0];
            const last = elements[elements.length - 1];
            if (event.shiftKey && document.activeElement === first) {
              event.preventDefault();
              last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
              event.preventDefault();
              first.focus();
            }
          }
        }}
      >
        <Link className="brand" to="/jobs" onClick={() => setMenuOpen(false)}>
          <span className="brand-mark">
            <Sparkles size={17} />
          </span>
          Shortlist
        </Link>
        <button
          className="icon-button sidebar-close"
          aria-label="Close menu"
          onClick={() => setMenuOpen(false)}
        >
          <X size={19} />
        </button>
        <div className="nav-caption">Hiring</div>
        <nav aria-label="Main navigation">
          <Link
            className={`nav-item ${location.pathname.startsWith('/jobs') || location.pathname.startsWith('/candidates') ? 'active' : ''}`}
            to="/jobs"
            onClick={() => setMenuOpen(false)}
          >
            <BriefcaseBusiness size={18} />
            Jobs
          </Link>
          <Link
            className={`nav-item ${location.pathname === '/talent-pool' ? 'active' : ''}`}
            to="/talent-pool"
            onClick={() => setMenuOpen(false)}
          >
            <UserSearch size={18} />
            Talent pool
          </Link>
          {current.role === 'admin' && (
            <>
              <div className="nav-caption nav-group-caption">Workspace</div>
              <Link
                className={`nav-item ${location.pathname === '/team' ? 'active' : ''}`}
                to="/team"
                onClick={() => setMenuOpen(false)}
              >
                <Users size={18} />
                Team
              </Link>
              <Link
                className={`nav-item ${location.pathname === '/settings' ? 'active' : ''}`}
                to="/settings"
                onClick={() => setMenuOpen(false)}
              >
                <Settings size={18} />
                Retention & settings
              </Link>
              <Link
                className={`nav-item ${location.pathname === '/usage' ? 'active' : ''}`}
                to="/usage"
                onClick={() => setMenuOpen(false)}
              >
                <ChartColumn size={18} />
                Usage & cost
              </Link>
            </>
          )}
          <div className="nav-caption nav-group-caption">Help</div>
          <Link
            className={`nav-item ${location.pathname === '/guide' ? 'active' : ''}`}
            to="/guide"
            onClick={() => setMenuOpen(false)}
          >
            <BookOpen size={18} />
            How it works
          </Link>
        </nav>
        <div className="sidebar-footer">
          <span className="user-avatar">{label(current.role).charAt(0)}</span>
          <div>
            <strong>{label(current.role)}</strong>
            <small>Signed in</small>
          </div>
          <button
            className="icon-button"
            aria-label="Sign out"
            title="Sign out"
            onClick={() => {
              session.set(null);
              setMenuOpen(false);
            }}
          >
            <LogOut size={18} />
          </button>
        </div>
      </aside>
      {menuOpen && (
        <button className="menu-backdrop" aria-label="Close navigation" onClick={() => setMenuOpen(false)} />
      )}
      <div className="workspace-content" inert={menuOpen}>
        <header className="topbar">
          <div className="topbar-left">
            <button
              className="icon-button mobile-menu"
              aria-label="Toggle navigation"
              onClick={() => setMenuOpen(!menuOpen)}
            >
              <PanelLeftClose size={20} />
            </button>
            <span>Workspace</span>
            <ChevronRight className="breadcrumb-slash" size={14} />
            <strong>
              {location.pathname.startsWith('/candidates')
                ? 'Candidate review'
                : location.pathname === '/talent-pool'
                  ? 'Talent pool'
                  : location.pathname === '/guide'
                    ? 'How it works'
                    : location.pathname === '/team'
                      ? 'Team'
                      : location.pathname === '/settings'
                        ? 'Retention & settings'
                        : location.pathname === '/usage'
                          ? 'Usage & cost'
                          : 'Jobs'}
            </strong>
          </div>
          <div className="topbar-right">
            <Link className="icon-button" to="/guide" aria-label="Open hiring guide" title="Hiring guide">
              <CircleHelp size={19} />
            </Link>
            <span className="topbar-divider" />
            <span className="role-chip">{label(current.role)}</span>
            <span className="topbar-avatar" aria-hidden="true">
              {label(current.role).charAt(0)}
            </span>
          </div>
        </header>
        <main id="main-content" tabIndex={-1}>
          <Outlet />
        </main>
        <footer className="page-footer">
          Shortlist · Evidence-based screening. People make the decisions.
        </footer>
      </div>
    </div>
  );
}
function Guide() {
  return (
    <div className="page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">How it works</span>
          <h1>Your shortlist, in three steps.</h1>
          <p>A simple path from a job description to a considered hiring decision.</p>
        </div>
      </div>
      <div className="guide-grid">
        {[
          [
            '01',
            'Define your role',
            'Create a job and paste its description. Review the skills and experience, then confirm the requirements. Use “Continue to resumes” when you’re ready.',
          ],
          [
            '02',
            'Add your candidates',
            'Drop in PDF, DOCX or TXT resumes. Record consent only when the candidates have given it. Wait for processing, then continue to screening.',
          ],
          [
            '03',
            'Review and decide',
            'Start screening, then open candidates to review their strengths, gaps and evidence. Compare up to five people within one run and record your decision.',
          ],
        ].map(([step, title, text]) => (
          <article className="panel guide-card" key={step}>
            <span className="step-number">{step}</span>
            <h2>{title}</h2>
            <p>{text}</p>
          </article>
        ))}
      </div>
      <div className="guide-hint">
        <CircleHelp size={19} />
        <p>
          You can revisit any step. Use <strong>Filters</strong> to narrow your results,{' '}
          <strong>Run details</strong> to retry failed candidates, and <strong>Feature breakdown</strong> for
          detailed evidence.
        </p>
      </div>
      <Link className="button primary" to="/jobs">
        Go to jobs
        <ArrowUpRight size={16} />
      </Link>
    </div>
  );
}
export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<AuthPage />} />
      <Route path="/signup" element={<AuthPage register />} />
      <Route element={<Workspace />}>
        <Route path="/jobs" element={<JobsPage />} />
        <Route path="/jobs/:jobId" element={<JobPage />} />
        <Route path="/candidates/:candidateId" element={<CandidatePage />} />
        <Route path="/talent-pool" element={<TalentPoolPage />} />
        <Route path="/guide" element={<Guide />} />
        <Route element={<AdminOnly />}>
          <Route path="/team" element={<TeamPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/usage" element={<UsagePage />} />
        </Route>
        <Route path="/" element={<Navigate to="/jobs" replace />} />
        <Route
          path="*"
          element={
            <div className="page">
              <h1>Page not found</h1>
              <p>This page is unavailable.</p>
              <Link className="button primary" to="/jobs">
                Back to jobs
              </Link>
            </div>
          }
        />
      </Route>
    </Routes>
  );
}
function AdminOnly() {
  return useSession()?.role === 'admin' ? (
    <Outlet />
  ) : (
    <div className="page">
      <h1>Admin access required</h1>
      <p>Your role cannot manage company settings, team accounts, or usage.</p>
      <Link className="button primary" to="/jobs">
        Back to jobs
      </Link>
    </div>
  );
}
