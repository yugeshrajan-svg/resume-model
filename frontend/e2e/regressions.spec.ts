import { expect, test } from '@playwright/test';
import { job, mock, signIn } from './fixtures';

test('edits requirements whose analyzed location contains a comma', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await mock(page, 'admin', false, { locations: ['Bengaluru, India (Hybrid) / Remote'] });
  await signIn(page);
  await page.getByRole('button', { name: 'Create job', exact: true }).click();
  await page.getByLabel('Job title').fill(job.title);
  await page.getByLabel('Job description').fill(job.jd_text);
  await page.getByRole('button', { name: 'Create and review requirements' }).click();
  await page.getByRole('button', { name: 'Analyze job description' }).click();
  await page.getByRole('button', { name: 'Edit as new version' }).click();
  const locations = page.getByLabel('Accepted locations');
  await expect(locations).toHaveValue('Bengaluru, India (Hybrid) / Remote');
  await locations.fill('Bengaluru, Remote, ');
  await expect(locations).toHaveValue('Bengaluru, Remote, ');
  const saveRequest = page.waitForRequest(
    (request) => request.method() === 'PUT' && request.url().endsWith('/requirements'),
  );
  await page.getByRole('button', { name: 'Save new version' }).click();
  expect((await saveRequest).postDataJSON().locations).toEqual(['Bengaluru', 'Remote']);
  expect(errors).toEqual([]);
});

test('shows knockout reasons on an ineligible candidate profile', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const detail = 'notice period 90 days exceeds maximum 60 days';
  await mock(page, 'admin', true, { knockout: [{ rule: 'notice_period', detail }] });
  await signIn(page);
  await page.goto(`/candidates/candidate-1?job=${job.id}&run=run-1`);
  await expect(page.getByRole('heading', { name: 'Alex Example' })).toBeVisible();
  await expect(page.getByText('Eligibility checks')).toBeVisible();
  await expect(page.getByRole('listitem').filter({ hasText: detail })).toBeVisible();
  expect(errors).toEqual([]);
});
