import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { job, mock, signIn } from './fixtures';

for (const [label, source] of [
  ['Saved resumes', 'database'],
  ['Uploaded resumes', 'uploads'],
  ['Both sources', 'both'],
]) {
  test(`asks before screening and uses ${source}`, async ({ page }) => {
    await mock(page, 'admin', true);
    await signIn(page);
    await page.goto(`/jobs/${job.id}?tab=results`);
    const starts: unknown[] = [];
    page.on('request', (request) => {
      if (request.method() === 'POST' && request.url().endsWith('/screening-runs'))
        starts.push(request.postDataJSON());
    });
    await page.getByRole('button', { name: 'Start screening', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Choose resumes for screening' });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole('radio')).toHaveCount(3);
    await expect(dialog.getByRole('button', { name: 'Screen selected resumes' })).toBeDisabled();
    expect(starts).toEqual([]);
    await dialog.getByRole('radio', { name: new RegExp(`^${label}`) }).check();
    await expect(dialog.getByRole('status')).toContainText('ready to screen');
    await dialog.getByRole('button', { name: 'Screen selected resumes' }).click();
    await expect(dialog).not.toBeVisible();
    expect(starts).toEqual([{ source }]);
  });
}

test('empty sources explain the workspace and retain both when going to upload', async ({ page }) => {
  await mock(page, 'admin', true);
  await page.route('**/screening-sources', (route) =>
    route.fulfill({
      json: {
        database: { total: 0, screenable: 0, blocked: 0 },
        uploads: { total: 0, screenable: 0, blocked: 0 },
        both: { total: 0, screenable: 0, blocked: 0 },
        pending_uploads: 0,
        database_excluded: 0,
      },
    }),
  );
  await signIn(page);
  await page.goto(`/jobs/${job.id}?tab=results`);
  await page.getByRole('button', { name: 'Start screening', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('radio', { name: /^Both sources/ }).check();
  await expect(dialog.getByText('Saved resumes come from your current company workspace.')).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Screen selected resumes' })).toBeDisabled();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: test.info().outputPath('source-picker-mobile.png'), fullPage: true });
  expect((await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()).violations).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await dialog.getByRole('button', { name: 'Upload new resumes' }).click();
  await expect(page.getByLabel('Choose resumes')).toBeVisible();
  await page.getByRole('link', { name: 'Continue to screening' }).click();
  await page.getByRole('button', { name: 'Start screening', exact: true }).click();
  await expect(page.getByRole('radio', { name: /^Both sources/ })).toBeChecked();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await expect(page.getByRole('button', { name: 'Start screening', exact: true })).toBeFocused();
});

test('waits for uploads to process and recovers from preview failures', async ({ page }) => {
  await mock(page, 'admin', true);
  let pending = 1;
  let failed = true;
  await page.route('**/screening-sources', (route) =>
    route.fulfill(
      failed
        ? { status: 503, json: { detail: 'Temporarily unavailable' } }
        : {
            json: {
              database: { total: 2, screenable: 2, blocked: 0 },
              uploads: { total: 1, screenable: 0, blocked: 1 },
              both: { total: 3, screenable: 2, blocked: 1 },
              pending_uploads: pending,
              database_excluded: 0,
            },
          },
    ),
  );
  await signIn(page);
  await page.goto(`/jobs/${job.id}?tab=results`);
  await page.getByRole('button', { name: 'Start screening', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('alert')).toContainText('Temporarily unavailable');
  failed = false;
  await dialog.getByRole('button', { name: 'Try again' }).click();
  await dialog.getByRole('radio', { name: /^Both sources/ }).check();
  await expect(dialog.getByRole('button', { name: 'Screen selected resumes' })).toBeDisabled();
  await expect(dialog.getByText('Uploaded resumes are still processing.', { exact: false })).toBeVisible();
  await dialog.getByRole('radio', { name: /^Saved resumes/ }).check();
  await expect(dialog.getByRole('button', { name: 'Screen selected resumes' })).toBeEnabled();
  pending = 0;
  await dialog.getByRole('radio', { name: /^Both sources/ }).check();
  await expect(dialog.getByRole('button', { name: 'Screen selected resumes' })).toBeEnabled();
});
