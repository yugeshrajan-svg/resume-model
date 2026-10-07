import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { mock, signIn, stamp } from './fixtures';

test('saved resumes exposes consent, profiles and filtering before screening', async ({ page }) => {
  await mock(page, 'admin', true);
  await page.route('**/v1/talent-pool/resumes?**', async (route) => {
    const url = new URL(route.request().url());
    const items = [
      {
        resume_id: 'resume-1',
        candidate_id: 'candidate-1',
        filename: 'Alex.pdf',
        name: 'Alex Example',
        status: 'ready',
        version: 1,
        created_at: stamp,
        has_consent: false,
        has_pool_consent: false,
      },
      {
        resume_id: 'resume-2',
        candidate_id: null,
        filename: 'New.txt',
        name: null,
        status: 'registered',
        version: null,
        created_at: stamp,
        has_consent: false,
        has_pool_consent: false,
      },
    ];
    const filtered = url.searchParams.get('q')
      ? []
      : url.searchParams.get('view') === 'consent'
        ? items.slice(0, 1)
        : items;
    await route.fulfill({
      json: {
        summary: { total: 2, processing: 1, attention: 0, needs_consent: 1 },
        total: filtered.length,
        items: filtered,
      },
    });
  });
  await signIn(page);
  await page.goto('/talent-pool?tab=library');
  await expect(page.locator('.library-row')).toHaveCount(2);
  await expect(page.getByText('Consent needed before screening', { exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Profile & consent' })).toHaveAttribute(
    'href',
    '/candidates/candidate-1?from=library',
  );
  await expect(page.getByText('Profile available after processing')).toBeVisible();
  await page.getByRole('button', { name: '1 need future-role consent' }).click();
  await expect(page.locator('.library-row')).toHaveCount(1);
  await page.getByLabel('Find a saved resume').fill('not found');
  await expect(page.getByRole('heading', { name: 'No files match these filters' })).toBeVisible();
  await page.getByRole('button', { name: 'Reset filters' }).click();
  await expect(page.locator('.library-row')).toHaveCount(2);
  expect((await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()).violations).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('upload methods are separate and switching preserves the Drive preview', async ({ page }) => {
  await mock(page, 'admin', true);
  await page.route('**/v1/drive/preview', (route) =>
    route.fulfill({ json: { files: [{ id: 'abcdefghijk', filename: 'Shared.pdf' }] } }),
  );
  await signIn(page);
  await page.goto('/talent-pool?tab=uploads');
  await expect(page.getByLabel('Choose resumes')).toBeVisible();
  await expect(page.getByLabel('Google Drive link')).not.toBeVisible();
  await page.getByRole('button', { name: 'From Google Drive' }).click();
  await page.getByLabel('Google Drive link').fill('https://drive.google.com/drive/folders/abcdefghijk');
  await page.getByRole('button', { name: 'Preview files' }).click();
  await expect(page.getByText(/1 resumes found/)).toBeVisible();
  await page.getByRole('tab', { name: 'Search resumes', exact: true }).click();
  await page.getByRole('tab', { name: 'Upload resumes', exact: true }).click();
  await expect(page.getByText(/1 resumes found/)).toBeVisible();
  await expect(page.getByText(/Consent hasn’t been recorded for this upload/)).toBeVisible();
  expect((await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()).violations).toEqual([]);
});
