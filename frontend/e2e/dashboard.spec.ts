import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { job, mock, signIn, startScreening } from './fixtures';

test('creates, analyzes, edits and confirms requirements, then reviews ranked evidence', async ({ page }) => {
  await mock(page);
  await signIn(page);
  await page.getByRole('button', { name: 'Create job', exact: true }).click();
  await page.getByLabel('Job title').fill(job.title);
  await page.getByLabel('Job description').fill(job.jd_text);
  await page.getByRole('button', { name: 'Create and review requirements' }).click();
  await page.getByRole('button', { name: 'Analyze job description' }).click();
  await expect(page.getByText('Ready for your review')).toBeVisible();
  await page.getByRole('button', { name: 'Edit as new version' }).click();
  await page.getByLabel('Accepted locations').fill('Bengaluru, Remote');
  await page.getByLabel('Required skills 1 alternatives').pressSequentially('Java, Go');
  const saveRequest = page.waitForRequest(
    (request) => request.method() === 'PUT' && request.url().endsWith('/requirements'),
  );
  await page.getByRole('button', { name: 'Save new version' }).click();
  const payload = (await saveRequest).postDataJSON();
  expect(payload.locations).toEqual(['Bengaluru', 'Remote']);
  expect(payload.required_skills[0].alternatives).toEqual(['Java', 'Go']);
  expect(payload.required_skills[0].evidence).toBeNull();
  await page.getByRole('button', { name: 'Confirm version 2' }).click();
  await expect(page.getByText('Version 2 is confirmed')).toBeVisible();
  await page.getByRole('tab', { name: 'Screening & results' }).click();
  await startScreening(page);
  await expect(
    page.getByRole('region', { name: 'Candidate rankings' }).getByText('87.2', { exact: true }),
  ).toBeVisible();
  await page.getByRole('link', { name: 'Review candidate' }).click();
  await expect(page.getByRole('heading', { name: 'Alex Example' })).toBeVisible();
  await expect(page.getByText('Built Python services.', { exact: true })).toHaveCount(2);
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Alex Example' })).toBeVisible();
  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByRole('heading', { name: 'Welcome back.' })).toBeVisible();
});
test('limits hiring managers to review controls and omits contacts', async ({ page }) => {
  await mock(page, 'hiring_manager', true);
  await signIn(page);
  await expect(page.getByRole('button', { name: 'Create job', exact: true })).toHaveCount(0);
  await page.getByRole('link', { name: /Senior backend engineer/ }).click();
  await expect(page.getByRole('button', { name: 'Edit as new version' })).toHaveCount(0);
  await page.getByRole('tab', { name: 'Resumes', exact: false }).click();
  await expect(page.getByLabel('Choose resumes')).toHaveCount(0);
  await page.getByRole('tab', { name: 'Screening & results' }).click();
  await expect(page.getByRole('button', { name: 'Start screening' })).toHaveCount(0);
  await page.getByRole('link', { name: 'Review candidate' }).click();
  await expect(page.getByText('alex@example.com')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Feature breakdown' })).toBeVisible();
});
test('shows authentication errors and recovers from an API outage', async ({ page }) => {
  await mock(page);
  await page.route('**/v1/auth/login', (route) =>
    route.fulfill({ status: 401, json: { detail: 'Invalid credentials' } }),
  );
  await page.goto('/login');
  await page.getByLabel('Work email').fill('admin@example.com');
  await page.getByLabel('Password', { exact: true }).fill('wrong');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Invalid credentials');
  await page.unroute('**/v1/auth/login');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.route('**/v1/jobs', (route) =>
    route.fulfill({ status: 503, json: { detail: 'Database unavailable' } }),
  );
  await page.reload();
  await expect(page.getByRole('alert')).toContainText('Database unavailable');
  await page.unroute('**/v1/jobs');
  await page.getByRole('button', { name: 'Try again' }).click();
  await expect(page.getByRole('heading', { name: 'Your next hire starts here' })).toBeVisible();
});
test('renders desktop and mobile without page overflow and passes accessibility checks', async ({
  page,
}, info) => {
  await mock(page, 'admin', true);
  await signIn(page);
  await page.screenshot({ path: info.outputPath('jobs-desktop.png'), fullPage: true });
  expect(
    (await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()).violations.map((item) => ({
      id: item.id,
      nodes: item.nodes.map((node) => ({ target: node.target, summary: node.failureSummary })),
    })),
  ).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: info.outputPath('jobs-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole('button', { name: 'Toggle navigation' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  await page.getByRole('button', { name: 'Close menu', exact: true }).click();
  await page.getByRole('link', { name: /Senior backend engineer/ }).click();
  await page.getByRole('tab', { name: 'Screening & results' }).click();
  await page.getByRole('link', { name: 'Review candidate' }).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('candidate-mobile.png'), fullPage: true });
});
test('validates files and requires consent details when consent is selected', async ({ page }) => {
  await mock(page, 'admin', true);
  await signIn(page);
  await page.goto('/jobs/job-1?tab=resumes');
  await page.getByLabel('Choose resumes').setInputFiles([
    { name: 'wrong.png', mimeType: 'image/png', buffer: Buffer.from('image') },
    { name: 'candidate.txt', mimeType: 'text/plain', buffer: Buffer.from('Python experience') },
  ]);
  await expect(page.getByText('Choose a nonempty PDF, DOCX or TXT file.')).toBeVisible();
  await page.getByLabel('These candidates have given consent').check();
  await page.getByRole('button', { name: 'Upload and register' }).click();
  await expect(page.getByRole('alert')).toContainText('Enter the consent source and purpose');
});
test('supports keyboard workflow tabs and checks accessibility across the main screens', async ({
  page,
}, info) => {
  test.setTimeout(60_000);
  await mock(page, 'admin', true);
  await page.goto('/login');
  expect(
    (await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()).violations.map((item) => ({
      id: item.id,
      nodes: item.nodes.map((node) => ({ target: node.target, summary: node.failureSummary })),
    })),
  ).toEqual([]);
  await page.screenshot({ path: info.outputPath('login-desktop.png'), fullPage: true });
  await signIn(page);
  await page.getByRole('link', { name: /Senior backend engineer/ }).click();
  for (const name of ['Requirements', 'Resumes', 'Screening & results']) {
    await page.getByRole('tab', { name, exact: false }).click();
    await expect(page.getByRole('tabpanel')).toBeVisible();
    const accessibility = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze();
    expect(
      accessibility.violations.map((item) => ({
        id: item.id,
        nodes: item.nodes.map((node) => ({ target: node.target, summary: node.failureSummary })),
      })),
    ).toEqual([]);
  }
  await page.getByRole('tab', { name: 'Requirements', exact: false }).click();
  await page.getByRole('tab', { name: 'Requirements', exact: false }).press('ArrowRight');
  await expect(page.getByRole('tab', { name: 'Resumes', exact: false })).toHaveAttribute(
    'aria-selected',
    'true',
  );
  await page.getByRole('tab', { name: 'Resumes', exact: false }).press('End');
  await expect(page.getByRole('tab', { name: 'Screening & results' })).toHaveAttribute(
    'aria-selected',
    'true',
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Toggle navigation' }).click();
  await expect(page.getByRole('button', { name: 'Close menu' })).toBeFocused();
  await page.getByRole('button', { name: 'Close menu' }).press('Escape');
  await expect(page.getByRole('button', { name: 'Toggle navigation' })).toBeFocused();
});
