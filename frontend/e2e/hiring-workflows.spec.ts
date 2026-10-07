import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { job, mock, signIn } from './fixtures';

test('hiring navigation removes People workspace and redirects its old route', async ({ page }) => {
  await mock(page, 'admin', true);
  await page.route('**/v1/hr/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    const kinds = {
      requisition: 'Hiring requisitions',
      offer_approval: 'Offer approvals',
      offer_template: 'Offer letter templates',
      onboarding: 'New-hire handover',
    };
    await route.fulfill({
      json: path.endsWith('/catalog')
        ? Object.fromEntries(
            Object.entries(kinds).map(([key, label]) => [key, { label, area: 'hiring', fields: [] }]),
          )
        : path.endsWith('/team')
          ? []
          : { total: 0, items: [] },
    });
  });
  await signIn(page);
  await page.goto('/people');
  await expect(page).toHaveURL(/\/operations$/);
  await expect(page.getByRole('heading', { name: 'Hiring workflows', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'People workspace' })).toHaveCount(0);
  for (const name of [
    'Hiring requisitions',
    'Offer approvals',
    'Offer letter templates',
    'New-hire handover',
  ])
    await expect(page.getByRole('button', { name, exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /Payroll|Leave|Attendance|Employees/ })).toHaveCount(0);
  expect((await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()).violations).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('job interview plans save a new version with clean scorecard criteria', async ({ page }) => {
  await mock(page, 'admin', true);
  let plan = { version: 0, rounds: [] as { id: string; name: string; criteria: string[] }[] };
  await page.route('**/v1/jobs/*/hiring-plan', async (route) => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON();
      expect(body.expected_version).toBe(0);
      expect(body.rounds[0].criteria).toEqual(['Python', 'Problem solving']);
      plan = { version: 1, rounds: body.rounds };
    }
    await route.fulfill({ json: plan });
  });
  await signIn(page);
  await page.goto(`/recruiting?tab=setup&job=${job.id}`);
  await page.getByRole('button', { name: 'Configure interview plan' }).click();
  await page.getByRole('button', { name: 'Add interview round' }).click();
  await page.getByLabel('Round 1 name').fill('Technical interview');
  await page.getByLabel('Round 1 criteria (one per line)').fill(' Python \nProblem solving\n');
  await page.getByRole('button', { name: 'Save interview plan' }).click();
  await expect(page.getByText('Plan version 1', { exact: true })).toBeVisible();
  await expect(page.getByText('Python · Problem solving', { exact: true })).toBeVisible();
});

test('candidates submit a resume with their own consent and without staff authentication', async ({
  page,
}) => {
  let submitted = false;
  await page.route('**/v1/public/jobs/public-job**', async (route) => {
    if (route.request().method() === 'POST') {
      expect(route.request().headers()).not.toHaveProperty('authorization');
      expect(route.request().headers()['x-candidate-consent']).toBe('yes');
      expect(route.request().postDataBuffer()?.toString()).toBe('Applicant resume bytes');
      submitted = true;
      return route.fulfill({ status: 202, json: { received: true } });
    }
    await route.fulfill({
      json: { title: 'Python engineer', company: 'Example employer', jd_text: 'Build Python services.' },
    });
  });
  await page.goto('/apply/public-job');
  await page
    .getByLabel('Your resume')
    .setInputFiles({
      name: 'applicant.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from('Applicant resume bytes'),
    });
  await page.getByRole('button', { name: 'Submit application' }).click();
  expect(submitted).toBe(false);
  await page.getByRole('checkbox', { name: /I agree/ }).check();
  await page.getByRole('button', { name: 'Submit application' }).click();
  await expect(page.getByRole('heading', { name: 'Your application was received' })).toBeVisible();
  expect(submitted).toBe(true);
  expect((await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()).violations).toEqual([]);
});

test('candidates choose a single interview slot and see confirmation', async ({ page }) => {
  let requests = 0;
  await page.route('**/v1/public/book/public-booking', async (route) => {
    if (route.request().method() === 'POST') {
      expect(route.request().postDataJSON()).toEqual({ index: 2 });
      requests++;
      return route.fulfill({ json: { booked: true } });
    }
    await route.fulfill({
      json: {
        title: 'Technical interview',
        location: 'Office',
        slots: [{ index: 2, start: '2026-10-10T10:00:00Z', end: '2026-10-10T11:00:00Z' }],
      },
    });
  });
  await page.goto('/book/public-booking');
  await page.getByRole('radio').check();
  await page.getByRole('button', { name: 'Confirm interview time' }).click();
  await expect(page.getByRole('heading', { name: 'Your interview is booked' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Confirm interview time' })).toHaveCount(0);
  expect(requests).toBe(1);
});
