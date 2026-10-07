import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { job, mock, signIn } from './fixtures';

const match = (id: string, extra: object) => ({
  match_id: `match-${id}`,
  candidate_id: `candidate-${id}`,
  resume_id: `resume-${id}`,
  similarity: 0.7,
  status: 'checked',
  eligible: true,
  reasons: [],
  warnings: [],
  failure_reason: null,
  checked_at: '2026-10-05T12:00:00Z',
  in_job: null,
  ...extra,
});
const pool = {
  job_id: job.id,
  requirements_version: 2,
  confirmed: true,
  pool_size: 3,
  progress: { total: 3, checked: 3, pending: 0, failed: 0 },
  eligible: [
    match('a', { name: 'Asha Iyer', similarity: 0.81 }),
    match('b', { name: 'Ravi Menon', similarity: 0.64, in_job: 'applied' }),
  ],
  ineligible: [
    match('c', {
      name: 'Sam Patel',
      eligible: false,
      similarity: 0.4,
      reasons: ['has 1 of 6 required skills (needs 5)'],
    }),
  ],
  failed: [],
};

test('shows eligible talent-pool candidates for the confirmed version and adds them', async ({ page }) => {
  await mock(page, 'admin', true, { poolMatches: pool });
  const added: unknown[] = [];
  await page.route('**/v1/jobs/*/applications/bulk', async (route) => {
    added.push(route.request().postDataJSON());
    await route.fulfill({
      json: { job_id: job.id, added: ['candidate-a'], already_applied: [], failed: [] },
    });
  });
  await signIn(page);
  await page.goto(`/jobs/${job.id}?tab=resumes`);
  const panel = page.getByRole('region', { name: 'Eligible from your talent pool' });
  await expect(panel.getByText('requirements v2', { exact: false })).toBeVisible();
  const rows = panel.locator('.pool-list').first().locator('.pool-row');
  await expect(rows).toHaveCount(2);
  await expect(rows.first()).toContainText('Asha Iyer');
  await expect(rows.first()).toContainText('81% match');
  await expect(rows.nth(1)).toContainText('In job · Applied');
  await expect(panel.getByText('Sam Patel')).toHaveCount(0);
  await panel.getByRole('button', { name: 'Show 1 not eligible' }).click();
  await expect(panel.getByText('has 1 of 6 required skills (needs 5)')).toBeVisible();

  await panel.getByRole('button', { name: 'Add all 1 eligible' }).click();
  await expect(panel.getByRole('status')).toContainText('1 added to this job');
  expect(added).toEqual([{ candidate_ids: ['candidate-a'] }]);
  expect((await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()).violations).toEqual([]);
});

test('records talent-pool consent at upload', async ({ page }) => {
  await mock(page, 'admin', true);
  await signIn(page);
  await page.goto(`/jobs/${job.id}?tab=resumes`);
  await expect(page.getByText('No candidates are in your talent pool yet.', { exact: false })).toBeVisible();
  await page.getByLabel('These candidates have given consent').check();
  await page.getByLabel('Consent source').fill('Careers form');
  const pool = page.getByLabel('They also agreed to be considered for future roles (talent pool)');
  await expect(pool).not.toBeChecked();
  await pool.check();
  await expect(pool).toBeChecked();
});

test('hiring managers review pool matches without add or refresh controls', async ({ page }) => {
  await mock(page, 'hiring_manager', true, { poolMatches: pool });
  await signIn(page);
  await page.goto(`/jobs/${job.id}?tab=resumes`);
  const panel = page.getByRole('region', { name: 'Eligible from your talent pool' });
  await expect(panel.locator('.pool-row')).toHaveCount(2);
  await expect(panel.getByRole('button', { name: /Add/ })).toHaveCount(0);
  await expect(panel.getByRole('button', { name: 'Refresh' })).toHaveCount(0);
});
