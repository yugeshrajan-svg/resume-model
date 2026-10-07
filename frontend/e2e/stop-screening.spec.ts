import { expect, test } from '@playwright/test';
import { completed, job, mock, signIn } from './fixtures';

test('stops an active screening, retains results and persists the stopped state', async ({ page }, info) => {
  await mock(page, 'admin', true);
  let run = { ...completed, status: 'running', total: 5, done: 1 };
  await page.route('**/v1/jobs/*/screening-runs', (route) => route.fulfill({ json: [run] }));
  let stopRequests = 0;
  await page.route('**/v1/screening-runs/*/stop', async (route) => {
    stopRequests++;
    run = { ...run, status: 'stopped' };
    await route.fulfill({ json: run });
  });
  await signIn(page);
  await page.goto(`/jobs/${job.id}?tab=results`);
  await expect(page.getByText('1 of 5 candidates processed')).toBeVisible();
  await page.locator('.run-progress').screenshot({ path: info.outputPath('stop-screening-button.png') });
  await page.getByRole('button', { name: 'Stop screening', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Screening stopped.' })).toBeVisible();
  await page.locator('.run-progress').screenshot({ path: info.outputPath('screening-stopped.png') });
  await expect(page.getByRole('button', { name: 'Stop screening', exact: true })).toHaveCount(0);
  await expect(page.locator('.candidate-table tbody tr')).toHaveCount(1);
  await page.reload();
  await expect(page.getByText('1 of 5 candidates processed')).toBeVisible();
  await expect(page.getByText('Screening stopped.', { exact: false })).toBeVisible();
  expect(stopRequests).toBe(1);
});

test('hiring managers cannot stop screening', async ({ page }) => {
  await mock(page, 'hiring_manager', true);
  await page.route('**/v1/jobs/*/screening-runs', (route) =>
    route.fulfill({
      json: [{ ...completed, status: 'running', total: 5, done: 1 }],
    }),
  );
  await signIn(page);
  await page.goto(`/jobs/${job.id}?tab=results`);
  await expect(page.getByText('1 of 5 candidates processed')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Stop screening', exact: true })).toHaveCount(0);
});
