import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { readFile } from 'node:fs/promises';
import { startScreening } from './fixtures';

async function register(page: Page) {
  const unique = crypto.randomUUID().replaceAll('-', '');
  await page.goto('/signup');
  await page.getByLabel('Company name').fill(`Dashboard test ${unique}`);
  await page.getByLabel('Work email').fill(`dashboard.${unique}@example.com`);
  await page.getByLabel('Password', { exact: true }).fill('Dashboard-test-password-123');
  await page.getByRole('button', { name: 'Create workspace' }).click();
  await expect(page.getByRole('heading', { name: 'Your jobs.' })).toBeVisible();
  return unique;
}
async function createJob(page: Page) {
  await page.getByRole('button', { name: 'Create job', exact: true }).click();
  await page.getByLabel('Job title').fill('Backend engineer');
  await page
    .getByLabel('Job description')
    .fill(
      'Backend engineer. Required skills: Python and SQL. Minimum 2 years of backend engineering experience. Remote work is available.',
    );
  await page.getByRole('button', { name: 'Create and review requirements' }).click();
  await page.getByRole('button', { name: 'Analyze job description' }).click();
  await expect(page.getByText('Ready for your review')).toBeVisible();
  await page.getByRole('button', { name: 'Confirm version 1' }).click();
  await expect(page.getByText('Version 1 is confirmed')).toBeVisible();
}
function resume(name: string, email: string) {
  return Buffer.from(
    `${name}\n${email}\nRemote\nSkills: Python, SQL, PostgreSQL, Git\n\nExperience\nSoftware Engineer | Example Systems | Jan 2021 - Present\nBuilt reliable Python and SQL services, including PostgreSQL data pipelines.\n\nProjects\nData platform: built backend services with Python and PostgreSQL.\n\nEducation\nBSc Computer Science\n`,
  );
}
test('real screenings choose saved resumes, uploads or both across jobs without duplicate candidates', async ({
  page,
}) => {
  test.setTimeout(120_000);
  const unique = await register(page);
  await createJob(page);
  await page.getByRole('tab', { name: 'Resumes', exact: false }).click();
  await page.getByLabel('Choose resumes').setInputFiles([
    {
      name: 'saved-a.txt',
      mimeType: 'text/plain',
      buffer: resume('Saved Alpha', `alpha.${unique}@example.com`),
    },
    {
      name: 'saved-b.txt',
      mimeType: 'text/plain',
      buffer: resume('Saved Beta', `beta.${unique}@example.com`),
    },
  ]);
  await page.getByLabel('These candidates have given consent').check();
  await page.getByLabel('Consent source').fill('Synthetic careers form');
  await page.getByLabel('They also agreed to be considered for future roles (talent pool)').check();
  await page.getByRole('button', { name: 'Upload and register' }).click();
  await expect(page.locator('.upload-row .badge.green')).toHaveCount(2);
  await page.getByRole('link', { name: 'Jobs', exact: true }).click();
  await createJob(page);
  await page.getByRole('tab', { name: 'Screening & results' }).click();
  await startScreening(page, 'Saved resumes');
  await expect(page.getByText('2 of 2 candidates processed')).toBeVisible();
  await expect(page.locator('.candidate-table tbody tr')).toHaveCount(2);
  await page.getByRole('tab', { name: 'Resumes', exact: false }).click();
  await page.getByLabel('Choose resumes').setInputFiles({
    name: 'new-upload.txt',
    mimeType: 'text/plain',
    buffer: resume('New Gamma', `gamma.${unique}@example.com`),
  });
  await page.getByLabel('These candidates have given consent').check();
  await page.getByLabel('Consent source').fill('Synthetic job application');
  await page.getByRole('button', { name: 'Upload and register' }).click();
  await expect(page.locator('.upload-row .badge.green')).toHaveCount(1);
  await page.getByRole('tab', { name: 'Screening & results' }).click();
  await startScreening(page, 'Uploaded resumes');
  await expect(page.getByText('1 of 1 candidates processed')).toBeVisible();
  await expect(page.locator('.candidate-table tbody tr')).toHaveCount(1);
  await startScreening(page, 'Both sources');
  await expect(page.getByText('3 of 3 candidates processed')).toBeVisible();
  await expect(page.locator('.candidate-table tbody tr')).toHaveCount(3);
  await page.reload();
  await expect(page.locator('.candidate-table tbody tr')).toHaveCount(3);
});
test('real signup, bulk SAS uploads, duplicates, processing, screening and evidence survive reload', async ({
  page,
}, info) => {
  const unique = await register(page);
  await createJob(page);
  const first = resume('Alex Example', `alex.${unique}@example.com`);
  const second = resume('Sam Example', `sam.${unique}@example.com`);
  await page.getByRole('tab', { name: 'Resumes', exact: false }).click();
  await page.getByLabel('Choose resumes').setInputFiles([
    { name: 'alex.txt', mimeType: 'text/plain', buffer: first },
    { name: 'sam.txt', mimeType: 'text/plain', buffer: second },
    { name: 'duplicate.txt', mimeType: 'text/plain', buffer: first },
    { name: 'invalid.pdf', mimeType: 'application/pdf', buffer: Buffer.from('This is not a PDF file.') },
  ]);
  await page.getByLabel('These candidates have given consent').check();
  await page.getByLabel('Consent source').fill('Synthetic test application form');
  await page.getByRole('button', { name: 'Upload and register' }).click();
  await expect(page.locator('.upload-row .badge.green')).toHaveCount(3);
  await expect(page.getByText('Duplicate linked to this job', { exact: false })).toBeVisible();
  await expect(
    page.locator('.upload-row').filter({ hasText: 'invalid.pdf' }).locator('.file-error'),
  ).toBeVisible();
  await page.reload();
  await expect(page.locator('.upload-row .badge.green')).toHaveCount(3);
  await page.getByRole('tab', { name: 'Screening & results' }).click();
  await startScreening(page);
  await expect(page.getByText('2 of 2 candidates processed')).toBeVisible();
  await expect(page.locator('.candidate-table tbody tr')).toHaveCount(2);
  await expect(page.locator('.candidate-table .score').first()).not.toHaveText('—');
  await page.screenshot({ path: info.outputPath('screening-desktop.png'), fullPage: true });
  const resultsUrl = page.url();
  await page.reload();
  await expect(page.locator('.candidate-table tbody tr')).toHaveCount(2);
  await page
    .getByRole('link', { name: /Review candidate/ })
    .first()
    .click();
  await expect(page.getByRole('heading', { name: 'Feature breakdown' })).toBeVisible();
  await expect(page.locator('.feature-card')).toHaveCount(6);
  await page.getByRole('heading', { name: 'Feature breakdown' }).click();
  await expect(page.locator('.feature-card blockquote').first()).toBeVisible();
  await page.screenshot({ path: info.outputPath('candidate-desktop.png'), fullPage: true });
  const accessibility = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze();
  expect(
    accessibility.violations.map((item) => ({ id: item.id, nodes: item.nodes.map((node) => node.target) })),
  ).toEqual([]);
  await page.reload();
  await expect(page.locator('.feature-card')).toHaveCount(6);
  await page.goto(resultsUrl);
  await page.getByText('Run details', { exact: true }).click();
  await page.getByRole('button', { name: 'Retry failed candidates' }).click();
  await expect(page.getByRole('alert')).toContainText('No failed applications');
});
test('real workflow blocks screening when uploaded candidates have no consent', async ({ page }) => {
  const unique = await register(page);
  await createJob(page);
  await page.getByRole('tab', { name: 'Resumes', exact: false }).click();
  await page.getByLabel('Choose resumes').setInputFiles({
    name: 'no-consent.txt',
    mimeType: 'text/plain',
    buffer: resume('Robin Example', `robin.${unique}@example.com`),
  });
  await page.getByRole('button', { name: 'Upload and register' }).click();
  await expect(page.locator('.upload-row .badge.green')).toHaveCount(1);
  await page.getByRole('tab', { name: 'Screening & results' }).click();
  await startScreening(page);
  await expect(page.getByRole('alert')).toContainText('No applications can be screened');
  await expect(page.locator('.blocked-list')).toContainText('consent');
});

test('real HR dashboard records decisions, manages consent, compares, exports, erases, and administers the team', async ({
  page,
  browser,
}, info) => {
  test.setTimeout(120_000);
  const authentication = page.waitForResponse(
    (response) => response.url().endsWith('/v1/auth/register-company') && response.status() === 201,
  );
  const unique = await register(page);
  const tokens = await (await authentication).json();
  const managerEmail = `manager.${unique}@example.com`;
  await page.getByRole('link', { name: 'Team', exact: true }).click();
  await page.getByLabel('Teammate email').fill(managerEmail);
  await page.getByLabel('Initial password').fill('Dashboard-manager-password-123');
  await page.getByLabel('Teammate role').selectOption('hiring_manager');
  await page.getByRole('button', { name: 'Create teammate' }).click();
  await expect(page.getByRole('cell', { name: managerEmail })).toBeVisible();
  await page.getByRole('link', { name: 'Retention & settings', exact: true }).click();
  await page.getByLabel('Retention days').fill('730');
  await page.getByRole('button', { name: 'Save retention settings' }).click();
  await expect(page.getByRole('status')).toContainText('Retention settings saved.');
  await page.reload();
  await expect(page.getByLabel('Retention days')).toHaveValue('730');
  await page.getByRole('link', { name: 'Usage & cost', exact: true }).click();
  await expect(page.locator('.usage-metrics strong').first()).toHaveText('$0.000000');
  await page.getByRole('link', { name: 'Jobs', exact: true }).click();
  await createJob(page);
  const jobId = new URL(page.url()).pathname.split('/').at(-1)!;
  await page.getByRole('tab', { name: 'Resumes', exact: false }).click();
  await page.getByLabel('Choose resumes').setInputFiles([
    {
      name: 'alex-private.txt',
      mimeType: 'text/plain',
      buffer: resume('Alex Example', `hr.alex.${unique}@example.com`),
    },
    {
      name: 'sam-private.txt',
      mimeType: 'text/plain',
      buffer: resume('Sam Example', `hr.sam.${unique}@example.com`),
    },
  ]);
  await page.getByLabel('These candidates have given consent').check();
  await page.getByLabel('Consent source').fill('Synthetic candidate application');
  await page.getByRole('button', { name: 'Upload and register' }).click();
  await expect(page.locator('.upload-row .badge.green')).toHaveCount(2);
  await page.getByRole('tab', { name: 'Screening & results' }).click();
  await startScreening(page);
  await expect(page.getByText('2 of 2 candidates processed')).toBeVisible();
  await expect(page.locator('.candidate-table tbody tr')).toHaveCount(2);
  const resultsUrl = page.url();
  const runId = new URL(resultsUrl).searchParams.get('run')!;
  await page.getByText('Choose from the full run roster', { exact: true }).click();
  const participants = page.getByRole('checkbox', { name: /from run roster/ });
  await expect(participants).toHaveCount(2);
  await participants.nth(0).check();
  await participants.nth(1).check();
  await page.getByRole('button', { name: 'Compare selected' }).click();
  await expect(page.locator('.comparison .badge.green')).toHaveCount(2);
  await page
    .getByRole('link', { name: /Review candidate/ })
    .first()
    .click();
  const candidateUrl = page.url();
  const candidateId = new URL(candidateUrl).pathname.split('/').at(-1)!;
  const filename = await page.locator('.resume-history strong').innerText();
  await page.getByRole('combobox', { name: 'Decision', exact: true }).selectOption('shortlist');
  await page.getByLabel('Decision reason').fill('Evidence supports a panel interview.');
  await page.getByRole('button', { name: 'Record decision' }).click();
  await expect(page.locator('.history-reason')).toHaveText('Evidence supports a panel interview.');
  await page.getByRole('button', { name: 'Revoke active consent' }).click();
  await page.getByRole('button', { name: 'Confirm revocation' }).click();
  await expect(page.getByText('All active consent grants revoked.', { exact: true })).toBeVisible();
  await page.getByText('Record a consent grant', { exact: true }).click();
  await page.getByLabel('Grant source').fill('Synthetic candidate email');
  await page.getByLabel('Grant purpose').fill('Backend engineering recruitment');
  await page.getByLabel('The candidate has given this consent').check();
  await page.getByRole('button', { name: 'Record consent', exact: true }).click();
  await expect(page.getByText('Consent recorded.', { exact: true })).toBeVisible();
  await expect(page.getByText('Revoked', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.locator('.history-reason')).toHaveText('Evidence supports a panel interview.');
  const managerContext = await browser.newContext({ baseURL: new URL(page.url()).origin });
  try {
    const manager = await managerContext.newPage();
    await manager.goto('/login');
    await manager.getByLabel('Work email').fill(managerEmail);
    await manager.getByLabel('Password', { exact: true }).fill('Dashboard-manager-password-123');
    await manager.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(manager.getByRole('heading', { name: 'Your jobs.' })).toBeVisible();
    await manager.goto(candidateUrl);
    await expect(manager.getByText('Filename hidden for your role')).toBeVisible();
    await expect(manager.getByText(filename, { exact: true })).toHaveCount(0);
    await expect(manager.getByRole('button', { name: 'Export candidate data' })).toHaveCount(0);
    await expect(manager.getByText('Record a consent grant', { exact: true })).toHaveCount(0);
    await manager.getByRole('combobox', { name: 'Decision', exact: true }).selectOption('review');
    await manager.getByLabel('Decision reason').fill('Arrange technical follow-up.');
    await manager.getByRole('button', { name: 'Record decision' }).click();
    await expect(manager.locator('.history-reason')).toHaveCount(2);
  } finally {
    await managerContext.close();
  }
  await page.reload();
  await expect(page.locator('.history-reason')).toHaveCount(2);
  await expect(page.locator('.decision-panel .section-heading .badge')).toHaveText('Review');
  const exportDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Export candidate data' }).click();
  const exported = JSON.parse(await readFile((await (await exportDownload).path())!, 'utf8'));
  expect(exported.candidate.id).toBe(candidateId);
  expect(exported.decisions).toHaveLength(2);
  expect(exported.consents).toHaveLength(2);
  expect(Buffer.from(exported.resume_files[0].content_base64, 'base64').toString()).toContain(
    'Built reliable Python',
  );
  await page.screenshot({ path: info.outputPath('hr-candidate.png'), fullPage: true });
  const accessibility = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze();
  expect(
    accessibility.violations.map((item) => ({ id: item.id, nodes: item.nodes.map((node) => node.target) })),
  ).toEqual([]);
  await page.getByRole('button', { name: 'Delete candidate', exact: true }).click();
  await page.getByLabel('I understand this permanently erases the candidate').check();
  await page.getByRole('button', { name: 'Permanently delete', exact: true }).click();
  await expect(page).toHaveURL(resultsUrl);
  await expect(page.locator('.candidate-table tbody tr')).toHaveCount(1);
  const missing = await page.request.get(`/v1/candidates/${candidateId}`, {
    headers: { Authorization: `Bearer ${tokens.access_token}` },
  });
  expect(missing.status()).toBe(404);
  await page.goto(`/jobs/${jobId}?tab=resumes`);
  await expect(page.locator('.upload-row')).toHaveCount(1);
  await page.reload();
  await expect(page.locator('.upload-row')).toHaveCount(1);
  await page.goto(`/jobs/${jobId}?tab=results&run=${runId}`);
  await expect(page.locator('.candidate-table tbody tr')).toHaveCount(1);
});
