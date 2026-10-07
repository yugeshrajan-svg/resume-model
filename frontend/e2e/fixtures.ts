import { expect } from '@playwright/test';
import type { Page } from '@playwright/test';

export const stamp = '2026-10-05T12:00:00Z';
export const job = {
  id: 'job-1',
  title: 'Senior backend engineer',
  jd_text: 'Build reliable Python and PostgreSQL services. Three years of backend experience required.',
  created_at: stamp,
};
const req = {
  role: 'Backend engineer',
  seniority: 'senior',
  experience_min_years: 3,
  experience_domain: 'backend',
  required_skills: [{ name: 'Python', evidence: 'Python', alternatives: [] }],
  preferred_skills: [],
  locations: ['Remote'],
  max_notice_period_days: null,
  required_skills_min_match: null,
  score_weights: null,
};
export const version = {
  id: 'req-1',
  version: 1,
  requirements: req,
  source: 'rules',
  created_at: stamp,
  confirmed_by: null,
  confirmed_at: null,
  analysis_meta: null,
};
export const completed = {
  id: 'run-1',
  job_id: job.id,
  requirements_version: 1,
  reference_date: '2026-10-05',
  retry_of_run_id: null,
  status: 'completed',
  total: 1,
  done: 1,
  blocked: [],
  created_at: stamp,
  finished_at: stamp,
};
export const explanation = {
  strengths: [
    {
      feature: 'required_skill_coverage',
      text: 'Matches the required Python skill.',
      quote: 'Built Python services.',
    },
  ],
  gaps: [],
  warnings: [],
};

export type MockOptions = {
  /** Locations returned by analysis, e.g. one value containing a comma. */
  locations?: string[];
  /** Knockout reasons on the candidate's assessment (makes the candidate ineligible). */
  knockout?: { rule: string; detail: string }[];
  /** Talent-pool matches for the job (default: an empty pool). */
  poolMatches?: unknown;
};

export async function mock(page: Page, role = 'admin', populated = false, options: MockOptions = {}) {
  const knockout = options.knockout ?? [];
  const jobs = populated
    ? [
        job,
        { ...job, id: 'job-2', title: 'Product designer' },
        { ...job, id: 'job-3', title: 'Data analyst' },
      ]
    : [];
  let versions: (typeof version)[] = populated ? [{ ...version, confirmed_at: stamp }] : [];
  let runs: (typeof completed)[] = populated ? [completed] : [];
  await page.route('**/v1/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    let body: unknown;
    let status = 200;
    if (path === '/v1/auth/login' || path === '/v1/auth/register-company')
      body = {
        access_token: 'test-access',
        refresh_token: 'test-refresh-token-123456',
        expires_in: 3600,
        company_id: 'company-1',
        user_id: role,
        role,
        token_type: 'bearer',
      };
    else if (path === '/v1/jobs' && request.method() === 'POST') {
      body = { ...job, ...request.postDataJSON() };
      jobs.push(body as typeof job);
      status = 201;
    } else if (path === '/v1/jobs') body = jobs;
    else if (path === '/v1/jobs/job-1') body = job;
    else if (path.endsWith('/requirements/confirm')) {
      versions = versions.map((item) =>
        item.version === request.postDataJSON().version ? { ...item, confirmed_at: stamp } : item,
      );
      body = versions.at(-1);
    } else if (path.endsWith('/analyze')) {
      versions.push({
        ...version,
        id: `req-${versions.length + 1}`,
        version: versions.length + 1,
        requirements: { ...req, locations: options.locations ?? req.locations },
      });
      body = versions.at(-1);
    } else if (path.endsWith('/requirements') && request.method() === 'PUT') {
      versions.push({
        ...version,
        id: `req-${versions.length + 1}`,
        version: versions.length + 1,
        requirements: request.postDataJSON(),
      });
      body = versions.at(-1);
    } else if (path.endsWith('/requirements')) body = versions;
    else if (path.endsWith('/screening-sources'))
      body = {
        database: { total: 2, screenable: 2, blocked: 0 },
        uploads: { total: 1, screenable: 1, blocked: 0 },
        both: { total: 3, screenable: 3, blocked: 0 },
        pending_uploads: 0,
        database_excluded: 0,
      };
    else if (path.endsWith('/screening-runs') && request.method() === 'POST') {
      runs = [completed];
      body = completed;
    } else if (path.endsWith('/screening-runs')) body = runs;
    else if (path.endsWith('/inputs'))
      body = [
        {
          candidate_id: 'candidate-1',
          resume_id: 'resume-1',
          resume_version: 1,
          processing_generation: 1,
          application_id: 'app-1',
          content_hash: 'hash',
        },
      ];
    else if (path.endsWith('/candidates'))
      body = {
        run_id: completed.id,
        run_status: 'completed',
        total: 1,
        limit: 20,
        offset: 0,
        items: [
          {
            application_id: 'app-1',
            candidate_id: 'candidate-1',
            rank: 1,
            score: 87.2,
            eligible: knockout.length === 0,
            status: 'scored',
            stage: 'screened',
            reprocess_reason: null,
            knockout_reasons: knockout.map((reason) => reason.detail),
            explanation,
            explanation_source: 'template',
          },
        ],
      };
    else if (path === '/v1/candidates/candidate-1')
      body = {
        candidate_id: 'candidate-1',
        created_at: stamp,
        ...(role === 'hiring_manager'
          ? {}
          : { contact: { name: 'Alex Example', email: 'alex@example.com', phone: null } }),
        resumes: [
          {
            resume_id: 'resume-1',
            filename: role === 'hiring_manager' ? null : 'candidate.txt',
            version: 1,
            status: 'ready',
            created_at: stamp,
          },
        ],
        applications: [{ application_id: 'app-1', job_id: job.id, stage: 'screened' }],
        consents: [],
        screening: [
          {
            run_id: completed.id,
            job_id: job.id,
            requirements_version: 1,
            run_created_at: stamp,
            status: 'scored',
            score: 87.2,
            eligible: knockout.length === 0,
            knockout: { reasons: knockout },
            explanation,
            explanation_source: 'template',
            features: [
              {
                name: 'required_skill_coverage',
                applicable: true,
                value: 1,
                detail: 'Python verified',
                evidence: ['Built Python services.'],
              },
            ],
          },
        ],
      };
    else if (path.endsWith('/hiring-plan')) body = { version: 0, rounds: [] };
    else if (path === '/v1/recruiting/activities') body = { total: 0, items: [] };
    else if (path.endsWith('/consents'))
      body = { candidate_id: 'candidate-1', has_active_consent: false, consents: [] };
    else if (path.endsWith('/pool-matches'))
      body = options.poolMatches ?? {
        job_id: job.id,
        requirements_version: 1,
        confirmed: true,
        pool_size: 0,
        progress: { total: 0, checked: 0, pending: 0, failed: 0 },
        eligible: [],
        ineligible: [],
        failed: [],
      };
    else if (path.endsWith('/decisions'))
      body = {
        application_id: 'app-1',
        candidate_id: 'candidate-1',
        job_id: job.id,
        stage: 'screened',
        decisions: [],
      };
    else {
      status = 404;
      body = { detail: `Unexpected test request: ${path}` };
    }
    await route.fulfill({ status, json: body });
  });
}
export async function signIn(page: Page) {
  await page.goto('/login');
  await page.getByLabel('Work email').fill('admin@example.com');
  await page.getByLabel('Password', { exact: true }).fill('Example-password-123');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Your jobs.' })).toBeVisible();
}
export async function startScreening(page: Page, source = 'Uploaded resumes') {
  await page.getByRole('button', { name: 'Start screening', exact: true }).click();
  await page.getByRole('radio', { name: new RegExp(`^${source}`) }).check();
  await page.getByRole('button', { name: 'Screen selected resumes', exact: true }).click();
}
