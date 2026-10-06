import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { mock, signIn } from './fixtures';

test('searches and sorts jobs, and restores focus after dismissing the new-job dialog', async ({
  page,
}, info) => {
  await mock(page, 'admin', true);
  await signIn(page);
  await page.getByLabel('Sort jobs').selectOption('name');
  await expect(page.locator('.job-row h3')).toHaveText([
    'Data analyst',
    'Product designer',
    'Senior backend engineer',
  ]);
  await page.getByLabel('Search jobs').fill('backend');
  await expect(page.locator('.job-row')).toHaveCount(1);
  await page.getByRole('button', { name: 'Clear job search' }).click();
  await expect(page.locator('.job-row')).toHaveCount(3);
  await page.getByRole('button', { name: 'Create job', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByLabel('Job title')).toBeFocused();
  expect((await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()).violations).toEqual([]);
  await page.screenshot({ path: info.outputPath('create-job-desktop.png'), fullPage: true });
  await page.getByLabel('Job title').press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Create job', exact: true })).toBeFocused();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Create job', exact: true }).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('create-job-mobile.png'), fullPage: true });
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(page.locator('.job-row')).toHaveCount(3);
  const firstJob = await page.locator('.job-row').first().boundingBox();
  expect(firstJob?.y).toBeLessThan(844);
  await page.getByRole('link', { name: 'Open hiring guide' }).click();
  await expect(page.getByRole('heading', { name: 'Your shortlist, in three steps.' })).toBeVisible();
  await expect(page.locator('.guide-card')).toHaveCount(3);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect((await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()).violations).toEqual([]);
  await page.screenshot({ path: info.outputPath('guide-mobile.png'), fullPage: true });
});

test('keeps screening filters explicit and preserves access to run details and evidence', async ({
  page,
}, info) => {
  await mock(page, 'admin', true);
  await signIn(page);
  await page.goto('/jobs/job-1?tab=results');
  await expect(page.getByRole('link', { name: 'Review candidate' })).toBeVisible();
  await expect(page.getByLabel('Minimum score')).toHaveCount(0);
  await page.screenshot({ path: info.outputPath('results-desktop.png'), fullPage: true });
  await page.getByRole('button', { name: /^Filters/ }).click();
  const filtered = page.waitForRequest(
    (request) => new URL(request.url()).searchParams.get('score_min') === '50',
  );
  await page.getByLabel('Minimum score').fill('50');
  await filtered;
  await page.getByLabel('Eligibility', { exact: true }).selectOption('true');
  await page.getByRole('button', { name: /^Filters/ }).click();
  await expect(page.getByRole('button', { name: /^Filters/ })).toHaveAttribute('aria-expanded', 'false');
  await expect(page.getByRole('button', { name: 'Clear filters' })).toBeVisible();
  await page.getByRole('button', { name: /^Filters/ }).click();
  await expect(page.getByLabel('Minimum score')).toHaveValue('50');
  await expect(page.getByLabel('Eligibility', { exact: true })).toHaveValue('true');
  const cleared = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return (
      url.pathname.endsWith('/candidates') &&
      !url.searchParams.has('eligible') &&
      !url.searchParams.has('score_min')
    );
  });
  await page.getByRole('button', { name: 'Clear filters' }).click();
  await page.getByRole('button', { name: 'Refresh results' }).click();
  await cleared;
  await expect(page.getByLabel('Minimum score')).toHaveValue('');
  await page.getByText('Run details', { exact: true }).click();
  await expect(page.getByText('Reference date 2026-10-05')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Retry failed candidates' })).toBeVisible();
  await page.getByRole('link', { name: 'Review candidate' }).click();
  await expect(page.locator('.profile-aside .decision-panel')).toBeVisible();
  await expect(page.locator('.evidence-details')).not.toHaveAttribute('open', '');
  await page.getByRole('heading', { name: 'Feature breakdown' }).click();
  await expect(
    page.locator('.feature-card').getByText('Built Python services.', { exact: true }),
  ).toBeVisible();
  await page.screenshot({ path: info.outputPath('candidate-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('candidate-mobile.png'), fullPage: true });
});
