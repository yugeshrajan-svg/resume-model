import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import type { Role, User } from '../api/types';
import { date, label } from '../lib/format';
import { Badge, ErrorNotice, Spinner } from '../components/ui';

const permissions: Record<Role, string> = {
  admin: 'Manage hiring, team accounts, usage, retention, and candidate data.',
  recruiter: 'Manage jobs, resumes, screening, consent, and decisions. View candidate contacts.',
  hiring_manager: 'Review anonymized candidate profiles, compare results, and record decisions.',
};
export default function TeamPage() {
  const client = useQueryClient();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [role, setRole] = useState<Role>('recruiter');
  const [saved, setSaved] = useState(false);
  const users = useQuery({
    queryKey: ['users'],
    queryFn: ({ signal }) => api.request<User[]>('/users', { signal }),
  });
  const create = useMutation({
    mutationFn: () =>
      api.request<User>('/users', { method: 'POST', body: { email: email.trim(), password, role } }),
    onSuccess: async () => {
      setEmail('');
      setPassword('');
      setSaved(true);
      await client.invalidateQueries({ queryKey: ['users'] });
    },
  });
  return (
    <div className="page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Workspace admin</span>
          <h1>Your hiring team.</h1>
          <p>Give each teammate access that fits their work.</p>
        </div>
      </div>
      <div className="admin-layout">
        <section className="panel">
          <h2>Team members</h2>
          <ErrorNotice
            error={users.error}
            retry={() => {
              void users.refetch();
            }}
          />
          {users.isPending ? (
            <Spinner text="Loading team…" />
          ) : (
            users.data && (
              <div className="table-scroll" tabIndex={0} role="region" aria-label="Team members">
                <table className="candidate-table">
                  <thead>
                    <tr>
                      <th>Email</th>
                      <th>Role</th>
                      <th>Status</th>
                      <th>Joined</th>
                    </tr>
                  </thead>
                  <tbody>
                    {users.data.map((user) => (
                      <tr key={user.id}>
                        <td>{user.email}</td>
                        <td>{label(user.role)}</td>
                        <td>
                          <Badge value={user.is_active ? 'active' : 'inactive'} />
                        </td>
                        <td>{date(user.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          )}
        </section>
        <section className="panel">
          <h2>Add a teammate</h2>
          <form
            className="hr-form"
            onSubmit={(event) => {
              event.preventDefault();
              setSaved(false);
              create.mutate();
            }}
          >
            <label>
              Teammate email
              <input
                type="email"
                required
                autoComplete="off"
                maxLength={254}
                value={email}
                disabled={create.isPending}
                onChange={(event) => {
                  setEmail(event.target.value);
                  setSaved(false);
                }}
              />
            </label>
            <label>
              Initial password
              <input
                type="password"
                required
                minLength={12}
                maxLength={256}
                autoComplete="new-password"
                value={password}
                disabled={create.isPending}
                onChange={(event) => setPassword(event.target.value)}
              />
            </label>
            <p className="field-hint">
              At least 12 characters. Share the credentials securely with your teammate.
            </p>
            <label>
              Teammate role
              <select
                value={role}
                disabled={create.isPending}
                onChange={(event) => setRole(event.target.value as Role)}
              >
                <option value="recruiter">Recruiter</option>
                <option value="hiring_manager">Hiring manager</option>
                <option value="admin">Admin</option>
              </select>
            </label>
            <p className="role-description">{permissions[role]}</p>
            <ErrorNotice error={create.error} />
            {saved && (
              <p role="status" className="success-message">
                Teammate account created.
              </p>
            )}
            <button className="button primary" disabled={create.isPending}>
              {create.isPending ? 'Creating…' : 'Create teammate'}
            </button>
          </form>
        </section>
      </div>
    </div>
  );
}
