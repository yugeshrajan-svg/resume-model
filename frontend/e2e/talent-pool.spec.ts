import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { job, mock, signIn } from './fixtures';

const found = {
  model: 'ollama:nomic-embed-text:v1.5',
  query_source: 'query',
  job_id: null,
  indexed_candidates: 12,
  resumes_pending_index: 2,
  results: [
    {
      candidate_id: 'candidate-1',
      similarity: 0.82,
      name: 'Alex Example',
      matched_resume: { resume_id: 'resume-1', version: 2, created_at: '2026-10-05T12:00:00Z' },
      applications: 1,
      in_job: null,
    },
    {
      candidate_id: 'candidate-2',
      similarity: 0.61,
      matched_resume: { resume_id: 'resume-2', version: 1, created_at: '2026-10-04T12:00:00Z' },
      applications: 0,
      in_job: null,
    },
  ],
};

async function talentRoutes(page: Page, role: string) {
  const searches: unknown[] = [];
  const added: unknown[] = [];
  await page.route('**/v1/talent-pool/search', async (route) => {
    searches.push(route.request().postDataJSON());
    const results =
      role === 'hiring_manager' ? found.results.map((item) => ({ ...item, name: undefined })) : found.results;
    await route.fulfill({ json: { ...found, results } });
  });
  await page.route('**/v1/jobs/*/applications', async (route) => {
    added.push({ url: route.request().url(), body: route.request().postDataJSON() });
    await route.fulfill({
      status: 201,
      json: {
        application_id: 'app-9',
        job_id: job.id,
        candidate_id: 'candidate-1',
        stage: 'applied',
        created: true,
      },
    });
  });
  return { searches, added };
}

test('searches the talent pool and adds a candidate to a job', async ({ page }) => {
  await mock(page, 'admin', true);
  const calls = await talentRoutes(page, 'admin');
  await signIn(page);
  await page.getByRole('link', { name: 'Talent pool', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Talent pool.' })).toBeVisible();
  const search = page.getByRole('button', { name: 'Search', exact: true });
  await expect(search).toBeDisabled();
  await page.getByLabel('Describe who you are looking for').fill('python services engineer');
  await search.click();
  await expect(page.getByText('Searched 12 candidates · 2 resumes are still being indexed.')).toBeVisible();
  expect(calls.searches).toEqual([{ query: 'python services engineer', limit: 30 }]);
  const rows = page.locator('.talent-row');
  await expect(rows).toHaveCount(2);
  await expect(rows.first()).toContainText('Alex Example');
  await expect(rows.first()).toContainText('82%');
  await expect(rows.nth(1)).toContainText('Candidate CANDIDAT');

  await rows.first().getByLabel('Job to add Alex Example to').selectOption(job.id);
  await rows.first().getByRole('button', { name: 'Add to job' }).click();
  await expect(rows.first().getByRole('status')).toHaveText('Added to job');
  expect(calls.added).toEqual([
    {
      url: expect.stringContaining(`/v1/jobs/${job.id}/applications`),
      body: { candidate_id: 'candidate-1' },
    },
  ]);
  expect((await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()).violations).toEqual([]);
});

test('hiring managers search without names or add-to-job controls', async ({ page }) => {
  await mock(page, 'hiring_manager', true);
  await talentRoutes(page, 'hiring_manager');
  await signIn(page);
  await page.goto('/talent-pool');
  await page.getByLabel('Or match a job').selectOption(job.id);
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await expect(page.locator('.talent-row')).toHaveCount(2);
  await expect(page.getByText('Alex Example')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Add to job' })).toHaveCount(0);
});
