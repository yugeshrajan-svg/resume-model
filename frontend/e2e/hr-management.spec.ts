import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { readFile } from 'node:fs/promises';
import type { ConsentRecord, DecisionRecord, Role } from '../src/api/types';
import { completed, explanation, job, mock, signIn, stamp } from './fixtures';

async function hrMock(page: Page, role: Role = 'admin') {
  await mock(page, role, true);
  const state = {
    stage: 'screened',
    deleted: false,
    deleteAttempts: 0,
    compareRequests: 0,
    compareComplete: false,
    retention: 365,
    resumeStatus: 'ready',
  };
  let decisions: DecisionRecord[] = [];
  let grants: ConsentRecord[] = [
    {
      consent_id: 'consent-1',
      source: 'Application form',
      purpose: 'Recruitment',
      granted_at: stamp,
      revoked_at: null,
      recorded_by: 'admin',
      active: true,
    },
  ];
  const users = [
    { id: 'admin', email: 'admin@example.com', role: 'admin', is_active: true, created_at: stamp },
  ];
  await page.route('**/v1/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    let body: unknown;
    let status = 200;
    if (path === '/v1/jobs/job-1/screening-runs' && method === 'GET')
      body = [
        { ...completed, total: 6, done: 6 },
        { ...completed, id: 'run-old', reference_date: '2025-10-05' },
      ];
    else if (path === '/v1/jobs/job-1/candidates')
      body = {
        run_id: url.searchParams.get('run_id'),
        run_status: 'completed',
        total: state.deleted ? 5 : 6,
        limit: 20,
        offset: 0,
        items: Array.from({ length: 6 }, (_, i) => i + 1)
          .filter((i) => !state.deleted || i !== 1)
          .map((i) => ({
            application_id: `app-${i}`,
            candidate_id: `candidate-${i}`,
            rank: i,
            score: 88 - i,
            eligible: true,
            status: 'scored',
            stage: i === 1 ? state.stage : 'screened',
            reprocess_reason: null,
            knockout_reasons: [],
            explanation,
          })),
      };
    else if (path === '/v1/applications/app-1/decision') {
      const payload = request.postDataJSON();
      const next = { shortlist: 'shortlisted', reject: 'rejected', review: 'review' }[
        payload.decision as 'shortlist' | 'reject' | 'review'
      ];
      const record = {
        decision_id: `decision-${decisions.length + 1}`,
        decision: payload.decision,
        previous_stage: state.stage,
        new_stage: next,
        reason: payload.reason,
        decided_at: stamp,
        reviewer: { user_id: role, email: `${role}@example.com`, role },
      };
      decisions = [record, ...decisions];
      state.stage = next;
      body = { ...record, stage: next, application_id: 'app-1' };
      status = 201;
    } else if (path === '/v1/applications/app-1/decisions')
      body = {
        application_id: 'app-1',
        candidate_id: 'candidate-1',
        job_id: job.id,
        stage: state.stage,
        decisions,
      };
    else if (path === '/v1/candidates/candidate-1/consents/revoke') {
      grants = grants.map((grant) => ({ ...grant, active: false, revoked_at: stamp }));
      body = { candidate_id: 'candidate-1', revoked: 1 };
    } else if (path === '/v1/candidates/candidate-1/consents') {
      if (method === 'POST') {
        const grant = {
          consent_id: 'consent-2',
          ...request.postDataJSON(),
          granted_at: stamp,
          revoked_at: null,
          active: true,
          recorded_by: role,
        };
        grants = [grant, ...grants];
        body = { consent_id: grant.consent_id, candidate_id: 'candidate-1' };
        status = 201;
      } else
        body = {
          candidate_id: 'candidate-1',
          has_active_consent: grants.some((grant) => grant.active),
          consents: grants,
        };
    } else if (path === '/v1/resumes/resume-1/reprocess') {
      state.resumeStatus = 'ready';
      body = {
        id: 'resume-1',
        candidate_id: 'candidate-1',
        filename: 'Alex-private.txt',
        status: 'registered',
        version: 1,
      };
    } else if (path === '/v1/candidates/candidate-1/export')
      body = {
        candidate: { id: 'candidate-1' },
        resumes: [{ filename: 'Alex-private.txt', content_base64: 'YQ==' }],
        decisions,
        consents: grants,
        audit_events: [],
      };
    else if (path === '/v1/candidates/compare') {
      state.compareRequests++;
      const payload = request.postDataJSON();
      const complete = state.compareComplete;
      body = {
        job_id: payload.job_id,
        run_id: payload.run_id,
        run_status: complete ? 'completed' : 'running',
        run_complete: complete,
        done: complete ? 6 : 1,
        total: 6,
        candidates: payload.candidate_ids.map((id: string, index: number) => ({
          candidate_id: id,
          status: !complete && index > 0 ? 'pending' : 'scored',
          screened: complete || index === 0,
          reason: null,
          reprocess_reason: null,
          score: complete || index === 0 ? 82.4 : null,
          eligible: complete || index === 0 ? true : null,
          features: complete || index === 0 ? { required_skill_coverage: 1, seniority_fit: null } : {},
          strengths: [],
          gaps: [],
        })),
      };
    } else if (/^\/v1\/candidates\/candidate-\d$/.test(path)) {
      const id = path.split('/').at(-1)!;
      if (method === 'DELETE') {
        state.deleteAttempts++;
        if (state.deleteAttempts === 1) {
          status = 503;
          body = { detail: 'Storage temporarily unavailable' };
        } else {
          state.deleted = true;
          body = { deleted: true, candidate_id: id };
        }
      } else if (state.deleted && id === 'candidate-1') {
        status = 404;
        body = { detail: 'candidate not found' };
      } else
        body = {
          candidate_id: id,
          created_at: stamp,
          ...(role === 'hiring_manager'
            ? {}
            : {
                contact: {
                  name: id === 'candidate-1' ? 'Alex Example' : id,
                  email: 'alex@example.com',
                  phone: null,
                },
              }),
          resumes: [
            {
              resume_id: `resume-${id.slice(-1)}`,
              filename: role === 'hiring_manager' ? null : 'Alex-private.txt',
              version: 1,
              status: state.resumeStatus,
              created_at: stamp,
            },
          ],
          applications: [{ application_id: `app-${id.slice(-1)}`, job_id: job.id, stage: state.stage }],
          consents: grants,
          screening: [
            {
              run_id: completed.id,
              job_id: job.id,
              requirements_version: 1,
              run_created_at: stamp,
              status: 'scored',
              score: 87,
              eligible: true,
              knockout: { reasons: [] },
              explanation,
              features: [
                {
                  name: 'required_skill_coverage',
                  value: 1,
                  applicable: true,
                  detail: 'Python verified',
                  evidence: ['Built Python services.'],
                },
              ],
            },
          ],
        };
    } else if (['/v1/company', '/v1/users', '/v1/usage'].includes(path) && role !== 'admin') {
      status = 403;
      body = { detail: 'Permission denied' };
    } else if (path === '/v1/company') {
      if (method === 'PATCH') state.retention = request.postDataJSON().retention_days;
      body = {
        id: 'company-1',
        name: 'Example company',
        retention_days: state.retention,
        retention_sweep_hour_utc: 3,
        candidates_total: 6,
        candidates_past_retention: 2,
      };
    } else if (path === '/v1/users') {
      if (method === 'POST') {
        const input = request.postDataJSON();
        body = { id: 'new-user', email: input.email, role: input.role, is_active: true, created_at: stamp };
        users.push(body as (typeof users)[number]);
        status = 201;
      } else body = users;
    } else if (path === '/v1/usage') {
      const metrics = { prompt_tokens: 10, completion_tokens: 3, ocr_pages: 1, cost_usd: 0.000004 };
      body = {
        from: url.searchParams.get('from'),
        to: url.searchParams.get('to'),
        totals: metrics,
        by_kind: { extraction: metrics },
        runs: [{ run_id: 'run-1', job_id: job.id, ...metrics }],
        by_day: [{ date: '2026-10-04', ...metrics }],
      };
    } else {
      await route.fallback();
      return;
    }
    await route.fulfill({ status, json: body });
  });
  return state;
}
async function checkAccessibility(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze();
  expect(
    results.violations.map((item) => ({ id: item.id, nodes: item.nodes.map((node) => node.target) })),
  ).toEqual([]);
}

test('recruiters can reprocess a failed resume directly from its candidate profile', async ({ page }) => {
  const state = await hrMock(page, 'recruiter');
  state.resumeStatus = 'failed';
  await signIn(page);
  await page.goto('/candidates/candidate-1');
  const request = page.waitForRequest(
    (request) => request.method() === 'POST' && request.url().endsWith('/resumes/resume-1/reprocess'),
  );
  await page.getByRole('button', { name: 'Reprocess version 1' }).click();
  await request;
  await expect(page.locator('.resume-history .badge')).toHaveText('Ready');
  await expect(page.getByRole('button', { name: 'Reprocess version 1' })).toHaveCount(0);
});

test('hiring managers record decisions and see history while contacts and admin actions remain hidden', async ({
  page,
}) => {
  await hrMock(page, 'hiring_manager');
  await signIn(page);
  await page.goto('/candidates/candidate-1?job=job-1&run=run-1');
  await expect(page.getByText('Filename hidden for your role')).toBeVisible();
  await expect(page.getByText('Alex-private.txt')).toHaveCount(0);
  await expect(page.getByText('alex@example.com')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Export candidate data' })).toHaveCount(0);
  await expect(page.getByText('Record a consent grant')).toHaveCount(0);
  await page.getByRole('combobox', { name: 'Decision', exact: true }).selectOption('shortlist');
  await page.getByLabel('Decision reason').fill('Strong evidence for a panel interview.');
  await page.getByRole('button', { name: 'Record decision' }).click();
  await expect(page.locator('.history-reason')).toHaveText('Strong evidence for a panel interview.');
  await expect(page.locator('.decision-panel .section-heading .badge')).toHaveText('Shortlisted');
  await page.reload();
  await expect(page.locator('.history-reason')).toHaveText('Strong evidence for a panel interview.');
  await checkAccessibility(page);
  const adminRequests: string[] = [];
  page.on('request', (request) => {
    if (/\/v1\/(company|users|usage)/.test(request.url())) adminRequests.push(request.url());
  });
  for (const path of ['/team', '/settings', '/usage']) {
    await page.goto(path);
    await expect(page.getByRole('heading', { name: 'Admin access required' })).toBeVisible();
  }
  expect(adminRequests).toEqual([]);
});

test('recruiters revoke all active consent and record a new grant without losing history', async ({
  page,
}) => {
  await hrMock(page, 'recruiter');
  await signIn(page);
  await page.goto('/candidates/candidate-1?job=job-1');
  await page.getByRole('button', { name: 'Revoke active consent' }).click();
  await checkAccessibility(page);
  await page.getByRole('button', { name: 'Confirm revocation' }).click();
  await expect(
    page.getByRole('status').filter({ hasText: 'All active consent grants revoked.' }),
  ).toBeVisible();
  await expect(page.getByText('Revoked', { exact: true })).toBeVisible();
  await page.getByText('Record a consent grant', { exact: true }).click();
  await page.getByLabel('Grant source').fill('Candidate email');
  await page.getByLabel('Grant purpose').fill('Backend engineer recruitment');
  await expect(page.getByRole('button', { name: 'Record consent', exact: true })).toBeDisabled();
  await page.getByLabel('The candidate has given this consent').check();
  const request = page.waitForRequest(
    (request) => request.method() === 'POST' && request.url().endsWith('/consents'),
  );
  await page.getByRole('button', { name: 'Record consent', exact: true }).click();
  expect((await request).postDataJSON()).toEqual({
    source: 'Candidate email',
    purpose: 'Backend engineer recruitment',
    scope: 'job',
  });
  await expect(page.getByText('Candidate email', { exact: true })).toBeVisible();
  await expect(page.getByText('Application form', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Delete candidate', exact: true })).toHaveCount(0);
});

test('comparison uses the selected run, polls pending results, caps selection, and resets on run changes', async ({
  page,
}) => {
  const state = await hrMock(page, 'hiring_manager');
  await signIn(page);
  await page.goto('/jobs/job-1?tab=results&run=run-old');
  const boxes = page.getByRole('checkbox', { name: /for comparison/ });
  await boxes.nth(0).check();
  await boxes.nth(1).check();
  const request = page.waitForRequest((request) => request.url().endsWith('/candidates/compare'));
  await page.getByRole('button', { name: 'Compare selected' }).click();
  expect((await request).postDataJSON()).toEqual({
    job_id: 'job-1',
    run_id: 'run-old',
    candidate_ids: ['candidate-1', 'candidate-2'],
  });
  await expect(page.locator('.comparison .badge').filter({ hasText: 'Pending' })).toBeVisible();
  await expect(page.locator('.comparison').getByText('Not applicable')).toHaveCount(1);
  state.compareComplete = true;
  await expect(page.locator('.comparison').getByText('Run complete', { exact: false })).toBeVisible();
  for (let index = 2; index < 5; index++) await boxes.nth(index).check();
  await expect(boxes.nth(5)).toBeDisabled();
  await checkAccessibility(page);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByLabel('Screening run', { exact: true }).selectOption('run-1');
  await expect(page.getByRole('button', { name: 'Compare selected' })).toBeDisabled();
  await expect(page.locator('.comparison')).toHaveCount(0);
  await expect(page.getByRole('checkbox', { checked: true })).toHaveCount(0);
});

test('admin exports JSON, confirms deletion, retries failures, and removes erased uploads from all jobs', async ({
  page,
}) => {
  const state = await hrMock(page);
  await signIn(page);
  await page.evaluate(() => {
    for (const job of ['job-1', 'job-2'])
      sessionStorage.setItem(
        `shortlist.uploads.company-1.${job}`,
        JSON.stringify([
          {
            id: 'erased',
            name: 'Alex-private.txt',
            status: 'ready',
            resume: { id: 'resume-1', candidate_id: null },
          },
          {
            id: 'retained',
            name: 'keep.txt',
            status: 'ready',
            resume: { id: 'resume-2', candidate_id: 'candidate-2' },
          },
        ]),
      );
  });
  await page.goto('/candidates/candidate-1?job=job-1&run=run-1');
  const downloaded = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Export candidate data' }).click();
  const file = await downloaded;
  expect(file.suggestedFilename()).toBe('candidate-candidate-1.json');
  const exported = JSON.parse(await readFile((await file.path())!, 'utf8'));
  expect(exported.resumes[0].content_base64).toBe('YQ==');
  await page.getByRole('button', { name: 'Delete candidate', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Permanently delete', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  expect(state.deleteAttempts).toBe(0);
  await page.getByRole('button', { name: 'Delete candidate', exact: true }).click();
  await page.getByLabel('I understand this permanently erases the candidate').check();
  await checkAccessibility(page);
  await page.getByRole('button', { name: 'Permanently delete', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Storage temporarily unavailable');
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('button', { name: 'Permanently delete', exact: true }).click();
  await expect(page).toHaveURL(/\/jobs\/job-1\?tab=results&run=run-1$/);
  await expect(page.locator('.candidate-table tbody tr')).toHaveCount(5);
  const manifests = await page.evaluate(() =>
    ['job-1', 'job-2'].map((job) =>
      JSON.parse(sessionStorage.getItem(`shortlist.uploads.company-1.${job}`)!),
    ),
  );
  expect(manifests.map((rows) => rows.map((row: { id: string }) => row.id))).toEqual([
    ['retained'],
    ['retained'],
  ]);
});

test('admin creates a teammate with the selected role and confirms shorter retention', async ({ page }) => {
  await hrMock(page);
  await signIn(page);
  await page.getByRole('link', { name: 'Team', exact: true }).click();
  await page.getByLabel('Teammate email').fill('manager@example.com');
  await page.getByLabel('Initial password').fill('Safe-test-password-123');
  await page.getByLabel('Teammate role').selectOption('hiring_manager');
  const created = page.waitForRequest(
    (request) => request.method() === 'POST' && request.url().endsWith('/users'),
  );
  await page.getByRole('button', { name: 'Create teammate' }).click();
  expect((await created).postDataJSON().role).toBe('hiring_manager');
  await expect(page.getByRole('cell', { name: 'manager@example.com' })).toBeVisible();
  await expect(page.getByLabel('Initial password')).toHaveValue('');
  await checkAccessibility(page);
  await page.getByRole('link', { name: 'Retention & settings', exact: true }).click();
  await expect(page.getByText('The retention worker runs daily at 03:00 UTC.')).toBeVisible();
  await page.getByLabel('Retention days').fill('30');
  await page.getByRole('button', { name: 'Save retention settings' }).click();
  await expect(page.getByRole('dialog')).toContainText('2 candidates are past the current 365-day limit');
  await checkAccessibility(page);
  await page.getByRole('button', { name: 'Confirm shorter retention' }).click();
  await expect(page.getByLabel('Retention days')).toHaveValue('30');
  await expect(page.getByRole('status')).toContainText('Retention settings saved');
  await page.reload();
  await expect(page.getByLabel('Retention days')).toHaveValue('30');
});

test('usage preserves small USD costs, UTC dates, exclusive end bounds, and mobile accessibility', async ({
  page,
}, info) => {
  await hrMock(page);
  await signIn(page);
  await page.getByRole('link', { name: 'Usage & cost', exact: true }).click();
  await page.getByLabel('Start date').fill('2026-10-04');
  await page.getByLabel('End date').fill('2026-10-04');
  await expect(page.getByRole('button', { name: 'Apply dates' })).toBeDisabled();
  await expect(page.getByRole('alert')).toContainText('start earlier');
  await page.getByLabel('End date').fill('2026-10-06');
  const request = page.waitForRequest((request) => new URL(request.url()).pathname === '/v1/usage');
  await page.getByRole('button', { name: 'Apply dates' }).click();
  const url = new URL((await request).url());
  expect(url.searchParams.get('from')).toBe('2026-10-04T00:00:00Z');
  expect(url.searchParams.get('to')).toBe('2026-10-06T00:00:00Z');
  await expect(page.locator('.usage-metrics strong').first()).toHaveText('$0.000004');
  await expect(page.getByText('Days without usage are shown as zero.', { exact: false })).toBeVisible();
  await checkAccessibility(page);
  await page.screenshot({ path: info.outputPath('usage-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await checkAccessibility(page);
  await page.screenshot({ path: info.outputPath('usage-mobile.png'), fullPage: true });
});
