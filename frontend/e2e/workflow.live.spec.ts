import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { startScreening } from './fixtures';

test('live PDF, DOCX and image-only OCR uploads enter the library and talent search', async ({
  page,
}, info) => {
  await register(page);
  await createJob(page);
  const jobId = new URL(page.url()).pathname.split('/').at(-1)!;
  await page.goto('/talent-pool?tab=uploads');
  await page
    .getByLabel('Choose resumes')
    .setInputFiles([
      resolve('../data/sample/resumes/01_strong_match.pdf'),
      resolve('../data/sample/resumes/02_overlapping_jobs.docx'),
      resolve('e2e/assets/scanned-resume.pdf'),
    ]);
  await page
    .getByRole('checkbox', { name: 'These candidates agreed to be considered for future roles.' })
    .check();
  await page.getByLabel('Consent source').fill('Synthetic format and OCR test fixtures');
  await page.getByRole('button', { name: 'Upload and register', exact: true }).click();
  await expect(page.locator('.upload-row .badge.green')).toHaveCount(3);
  await page.getByRole('tab', { name: 'Saved resumes', exact: true }).click();
  await expect(page.locator('.library-row')).toHaveCount(3);
  await expect(page.locator('.library-row .badge.green')).toHaveCount(3);
  await expect(page.locator('.library-row').filter({ hasText: 'scanned-resume.pdf' })).toContainText(
    'Taylor Brooks',
  );
  await expect(page.locator('.library-row').filter({ hasText: '01_strong_match.pdf' })).toContainText(
    'Priya Raman',
  );
  await expect(page.locator('.library-row').filter({ hasText: '02_overlapping_jobs.docx' })).toContainText(
    'Daniel Okafor',
  );
  await page.reload();
  await expect(page.locator('.library-row .badge.green')).toHaveCount(3);
  await page.screenshot({ path: info.outputPath('live-formats-library.png'), fullPage: true });
  await page.getByLabel('Find a saved resume').fill('scanned-resume');
  await expect(page.locator('.library-row')).toHaveCount(1);
  await expect(page.locator('.library-row')).toContainText('Taylor Brooks');
  await page.getByRole('tab', { name: 'Search resumes', exact: true }).click();
  await page.getByLabel('Describe who you are looking for').fill('Python SQL machine learning engineer');
  await expect(async () => {
    await page.getByRole('button', { name: 'Search', exact: true }).click();
    await expect(page.locator('.talent-row')).toHaveCount(3, { timeout: 5000 });
  }).toPass({ timeout: 180_000 });
  const priya = page.locator('.talent-row').filter({ hasText: 'Priya Raman' });
  await priya.getByLabel('Job to add Priya Raman to').selectOption(jobId);
  await priya.getByRole('button', { name: 'Add to job', exact: true }).click();
  await expect(priya.getByRole('status')).toHaveText('Added to job');
  await page.goto(`/recruiting?tab=pipeline&job=${jobId}`);
  await expect(page.getByText('Priya Raman', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText('Priya Raman', { exact: true })).toBeVisible();
});

test('complete live recruiting, public application, booking, hiring templates and navigation', async ({
  page,
  browser,
}, info) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await register(page);
  await createJob(page);
  const jobId = new URL(page.url()).pathname.split('/').at(-1)!;
  await page.goto(`/recruiting?tab=setup&job=${jobId}`);
  await page.getByRole('button', { name: 'Configure interview plan' }).click();
  await page.getByRole('button', { name: 'Add interview round' }).click();
  await page.getByLabel('Round 1 name').fill('Live technical interview');
  await page.getByLabel('Round 1 criteria (one per line)').fill('Python\nProblem solving');
  await page.getByRole('button', { name: 'Save interview plan' }).click();
  await expect(page.getByText('Plan version 1', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText('Python · Problem solving', { exact: true })).toBeVisible();

  await page.goto('/portal');
  await page.getByRole('combobox', { name: 'Job', exact: true }).selectOption(jobId);
  await page.getByRole('button', { name: 'Enable application page', exact: true }).click();
  await page.getByRole('checkbox', { name: /I reviewed this description/ }).check();
  await page.getByRole('button', { name: 'Enable reviewed page', exact: true }).click();
  const applicationLink = page.getByRole('link', { name: 'Open candidate view', exact: true });
  await expect(applicationLink).toHaveCount(1);
  const applicationPath = (await applicationLink.getAttribute('href'))!;
  const publicContext = await browser.newContext({ baseURL: new URL(page.url()).origin });
  try {
    const applicant = await publicContext.newPage();
    await applicant.goto(applicationPath);
    await expect(applicant.getByRole('heading', { name: 'Backend engineer', exact: true })).toBeVisible();
    const unique = crypto.randomUUID();
    await applicant.getByLabel('Your resume').setInputFiles({
      name: 'portal-candidate.txt',
      mimeType: 'text/plain',
      buffer: resume('Portal Candidate', `portal.${unique}@example.com`),
    });
    await applicant.getByRole('checkbox', { name: /I agree/ }).check();
    await applicant.getByRole('button', { name: 'Submit application', exact: true }).click();
    await expect(applicant.getByRole('heading', { name: 'Your application was received' })).toBeVisible();

    await page.goto(`/recruiting?tab=pipeline&job=${jobId}`);
    await expect(async () => {
      await page.getByRole('button', { name: 'Refresh workspace', exact: true }).click();
      await expect(page.getByText('Portal Candidate', { exact: true })).toBeVisible({ timeout: 5000 });
    }).toPass({ timeout: 180_000 });
    await page.getByRole('button', { name: 'Change stage', exact: true }).click();
    await page.getByLabel('New stage').selectOption('interview');
    await page.getByLabel('Reason', { exact: true }).fill('Synthetic live website validation');
    await page.getByRole('button', { name: 'Save stage change' }).click();
    await expect(page.getByRole('status').filter({ hasText: 'Hiring stage updated' })).toBeVisible();
    await page.reload();
    await expect(page.getByText('Portal Candidate', { exact: true })).toBeVisible();

    await page.goto('/portal');
    await page.getByRole('combobox', { name: 'Job', exact: true }).selectOption(jobId);
    await page.getByRole('button', { name: 'Create interview booking link', exact: true }).click();
    await page
      .getByRole('combobox', { name: 'Candidate', exact: true })
      .selectOption({ label: 'Portal Candidate' });
    const interviewer = page.getByRole('combobox', { name: 'Interviewer', exact: true });
    await expect(interviewer.locator('option')).toHaveCount(2);
    await interviewer.selectOption({ index: 1 });
    await page
      .getByRole('combobox', { name: 'Interview round', exact: true })
      .selectOption({ label: 'Live technical interview' });
    await page.getByLabel('Location or meeting link').fill('Synthetic local test room');
    const slot = new Date();
    slot.setDate(slot.getDate() + 7);
    slot.setHours(10, 0, 0, 0);
    const localTime = (date: Date) =>
      `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}T${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
    await page.getByLabel('Slot 1 start').fill(localTime(slot));
    await page.getByLabel('Slot 1 end').fill(localTime(new Date(slot.getTime() + 3600_000)));
    await page.getByRole('button', { name: 'Create booking link', exact: true }).click();
    const bookingLink = page.locator('a[href^="/book/"]');
    await expect(bookingLink).toHaveCount(1);
    await applicant.goto((await bookingLink.getAttribute('href'))!);
    await applicant.getByRole('radio').check();
    await applicant.getByRole('button', { name: 'Confirm interview time', exact: true }).click();
    await expect(applicant.getByRole('heading', { name: 'Your interview is booked' })).toBeVisible();
    await expect(applicant.getByRole('button', { name: 'Confirm interview time' })).toHaveCount(0);

    await page.goto(`/recruiting?tab=interview&job=${jobId}`);
    await expect(
      page.getByRole('heading', { name: 'Interview · Backend engineer', exact: true }),
    ).toBeVisible();
    await page.reload();
    await expect(
      page.getByRole('heading', { name: 'Interview · Backend engineer', exact: true }),
    ).toBeVisible();
    await page.getByLabel('Status for Interview · Backend engineer').selectOption('completed');
    await expect(page.getByLabel('Status for Interview · Backend engineer')).toHaveValue('completed');
    const calendarDownload = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Download calendar event', exact: true }).click();
    expect(await readFile((await (await calendarDownload).path())!, 'utf8')).toContain('BEGIN:VCALENDAR');

    await page.getByRole('button', { name: 'Feedback', exact: true }).click();
    await page.getByRole('button', { name: 'Add interview feedback', exact: true }).click();
    await page.getByLabel('Title', { exact: true }).fill('Live interview feedback');
    const candidateApplication = page.getByLabel('Candidate application');
    await expect(candidateApplication.locator('option')).toHaveCount(2);
    await candidateApplication.selectOption({ index: 1 });
    await page
      .getByRole('combobox', { name: 'Interview round', exact: true })
      .selectOption({ label: 'Live technical interview' });
    for (const criterion of [
      'Python',
      'Problem solving',
      'Technical / role skills',
      'Communication',
      'Relevant experience',
    ]) {
      await page.getByRole('combobox', { name: criterion, exact: true }).selectOption('4');
    }
    await page.getByLabel('Recommendation', { exact: true }).selectOption('hire');
    await page
      .getByLabel('Evidence and feedback')
      .fill('Synthetic test feedback: demonstrated Python and problem-solving skills.');
    await page.getByRole('button', { name: 'Save record', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Live interview feedback', exact: true })).toBeVisible();
    await page.reload();
    await expect(page.getByRole('heading', { name: 'Live interview feedback', exact: true })).toBeVisible();

    await page.getByRole('button', { name: 'Tasks', exact: true }).click();
    await page.getByRole('button', { name: 'Add recruiter task', exact: true }).click();
    await page.getByLabel('Title', { exact: true }).fill('Live recruiter follow-up');
    await page.getByLabel('Notes', { exact: true }).fill('Synthetic task for website verification.');
    await page.getByRole('button', { name: 'Save record', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Live recruiter follow-up', exact: true })).toBeVisible();
    await page.getByLabel('Status for Live recruiter follow-up').selectOption('completed');
    await expect(page.getByLabel('Status for Live recruiter follow-up')).toHaveValue('completed');

    await page.getByRole('button', { name: 'Offers', exact: true }).click();
    await page.getByRole('button', { name: 'Add offer record', exact: true }).click();
    await page.getByLabel('Title', { exact: true }).fill('Synthetic live offer');
    await expect(page.getByLabel('Candidate application').locator('option')).toHaveCount(2);
    await page.getByLabel('Candidate application').selectOption({ index: 1 });
    await page.getByLabel('Compensation amount').fill('900000');
    await page.getByLabel('Intended start date').fill(localTime(slot).slice(0, 10));
    await page.getByRole('button', { name: 'Save record', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Synthetic live offer', exact: true })).toBeVisible();
    await page.reload();
    await expect(page.getByRole('heading', { name: 'Synthetic live offer', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Reports', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Hiring reports' })).toBeVisible();
    const download = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Export report CSV' }).click();
    const report = await download;
    expect(await readFile((await report.path())!, 'utf8')).toContain('Backend engineer');
    await report.saveAs(info.outputPath('hiring-report.csv'));

    await page.goto('/operations?kind=offer_template');
    await page.getByRole('button', { name: 'Add record', exact: true }).click();
    await page.getByLabel('Record title').fill('Live offer template');
    await page
      .getByLabel('Letter text')
      .fill('Dear {{candidate}}, we offer the {{job}} position at {{company}}.');
    await page.getByRole('button', { name: 'Save record', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Live offer template', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Edit record', exact: true }).click();
    await page.getByLabel('Record title').fill('Reviewed live offer template');
    await page.getByLabel('Reason for this edit').fill('Verify persisted website editing');
    await page.getByRole('button', { name: 'Save changes', exact: true }).click();
    await page.reload();
    await expect(
      page.getByRole('heading', { name: 'Reviewed live offer template', exact: true }),
    ).toBeVisible();
    await page.getByRole('button', { name: 'Mark complete', exact: true }).click();
    await page.getByLabel('Reason', { exact: true }).fill('Synthetic validation completed');
    await page.getByRole('button', { name: 'Complete', exact: true }).click();
    await expect(page.getByRole('status')).toContainText('Action recorded in history.');

    for (const [path, heading] of [
      ['/jobs', 'Your jobs.'],
      ['/talent-pool', 'Talent pool.'],
      ['/recruiting', 'Recruiting workspace.'],
      ['/portal', 'Candidate portal'],
      ['/operations', 'Hiring workflows'],
      ['/team', 'Your hiring team.'],
      ['/settings', 'Retention & settings.'],
      ['/usage', 'Usage & cost.'],
    ]) {
      await page.goto(path);
      await expect(page.getByRole('heading', { name: heading, exact: true, level: 1 })).toBeVisible();
      await expect(page.locator('.loading:visible')).toHaveCount(0);
      await expect(page.locator('[role="alert"]')).toHaveCount(0);
      await page.setViewportSize({ width: 390, height: 844 });
      const sidebarRight = () =>
        page.locator('.sidebar').evaluate((element) => element.getBoundingClientRect().right);
      await expect.poll(sidebarRight).toBeLessThanOrEqual(1);
      await page.getByRole('button', { name: 'Toggle navigation', exact: true }).click();
      await expect(page.getByRole('dialog', { name: 'Workspace navigation' })).toBeVisible();
      await page.keyboard.press('Escape');
      await expect.poll(sidebarRight).toBeLessThanOrEqual(1);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), path).toBe(true);
      await page.screenshot({
        path: info.outputPath(`${path.slice(1)}-mobile.png`),
        fullPage: true,
        animations: 'disabled',
      });
      await page.setViewportSize({ width: 1440, height: 960 });
    }
    await page.goto('/guide');
    await expect(page.locator('main h1')).toBeVisible();
    await page.goto('/talent-pool');
    await page.getByLabel('Describe who you are looking for').fill('Python backend engineer');
    await page.getByRole('button', { name: 'Search', exact: true }).click();
    await expect(page.getByRole('heading', { name: /Best matches/ })).toBeVisible();
    await expect(page.locator('[role="alert"]')).toHaveCount(0);
    await page.getByRole('button', { name: 'Sign out', exact: true }).click();
    await expect(page).toHaveURL(/\/login$/);
    expect(errors).toEqual([]);
  } finally {
    await publicContext.close();
  }
});

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
  test.setTimeout(600_000);
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
  test.setTimeout(600_000);
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
  await page.getByLabel('Consent source', { exact: true }).fill('Synthetic candidate email');
  await page.getByLabel('Consent purpose', { exact: true }).fill('Backend engineering recruitment');
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
