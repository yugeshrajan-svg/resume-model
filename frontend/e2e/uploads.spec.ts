import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { mock, signIn, stamp } from './fixtures';

test('accepts over 500 resumes, paginates the list, registers all files and bounds status polling', async ({
  page,
}) => {
  test.setTimeout(90_000);
  await mock(page, 'admin', true);
  let registered = 0;
  const requested = new Set<string>();
  await page.route('**/v1/uploads/sas', async (route) => {
    const { filename } = route.request().postDataJSON();
    await route.fulfill({
      json: {
        blob_name: `company-1/${filename}`,
        upload_url: `${new URL(route.request().url()).origin}/test-blob/${filename}`,
        headers: {},
        method: 'PUT',
        expires_at: stamp,
      },
    });
  });
  await page.route('**/test-blob/**', (route) => route.fulfill({ status: 201 }));
  await page.route('**/v1/resumes/register-bulk', async (route) => {
    const { items } = route.request().postDataJSON() as { items: { blob_name: string; filename: string }[] };
    expect(items.length).toBeLessThanOrEqual(3);
    registered += items.length;
    await route.fulfill({
      json: items.map((item) => ({
        blob_name: item.blob_name,
        created: true,
        status_code: 201,
        error: null,
        resume: { id: item.filename, filename: item.filename, status: 'ready', size: 12, candidate_id: null },
      })),
    });
  });
  await page.route('**/v1/resumes/*.txt', async (route) => {
    const id = new URL(route.request().url()).pathname.split('/').at(-1)!;
    requested.add(id);
    await route.fulfill({ json: { id, filename: id, status: 'ready', size: 12, candidate_id: null } });
  });
  await signIn(page);
  await page.goto('/jobs/job-1?tab=resumes');
  const files = Array.from({ length: 501 }, (_, i) => ({
    name: `resume-${i}.txt`,
    mimeType: 'text/plain',
    buffer: Buffer.from('Skills: Python'),
  }));
  await page.getByLabel('Choose resumes').setInputFiles(files);
  await expect(page.locator('.upload-row')).toHaveCount(25);
  await expect(page.getByText('Page 1 of 21', { exact: true })).toBeVisible();
  await expect(page.getByText('0 of 501 files registered', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Upload and register', exact: true }).click();
  await expect(page.getByText('501 of 501 files registered', { exact: true })).toBeVisible({
    timeout: 75_000,
  });
  expect(registered).toBe(501);
  expect(requested.size).toBeLessThanOrEqual(25);
  await page.getByRole('button', { name: 'Next files', exact: true }).click();
  await expect(page.getByText('Page 2 of 21', { exact: true })).toBeVisible();
  await expect(page.locator('.upload-row strong').first()).toHaveText('resume-25.txt');
  await expect(page.locator('.upload-row')).toHaveCount(25);
  await page.reload();
  await expect(page.getByText('501 of 501 files registered', { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect((await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()).violations).toEqual([]);
});

test('retains more than 500 waiting resumes across selections and allows clearing them', async ({ page }) => {
  await mock(page, 'admin', true);
  await signIn(page);
  await page.goto('/jobs/job-1?tab=resumes');
  await page.getByLabel('Choose resumes').setInputFiles(
    Array.from({ length: 501 }, (_, i) => ({
      name: `${i}.txt`,
      mimeType: 'text/plain',
      buffer: Buffer.from('resume'),
    })),
  );
  await expect(page.getByText('0 of 501 files registered', { exact: true })).toBeVisible();
  await expect(page.locator('.upload-row')).toHaveCount(25);
  await page
    .getByLabel('Choose resumes')
    .setInputFiles({ name: 'one.txt', mimeType: 'text/plain', buffer: Buffer.from('resume') });
  await expect(page.getByText('0 of 502 files registered', { exact: true })).toBeVisible();
  await expect(page.getByText('Page 21 of 21', { exact: true })).toBeVisible();
  await expect(page.locator('.upload-row')).toHaveCount(2);
  await expect(page.locator('.upload-row strong').last()).toHaveText('one.txt');
  await page.getByRole('button', { name: 'Clear waiting files', exact: true }).click();
  await expect(page.locator('.upload-row')).toHaveCount(0);
});
