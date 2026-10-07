import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { job, mock, signIn, stamp } from './fixtures';

async function recruitingRoutes(page: Page) {
  let stage = 'shortlisted';
  const changes: unknown[] = [];
  const records: Record<string, unknown>[] = [];
  await page.route('**/v1/recruiting/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/team')) return route.fulfill({ json: [{ id: 'owner-1', label: 'Recruiter One' }] });
    if (path.endsWith('/reports'))
      return route.fulfill({
        json: {
          stages: { [stage]: 1 },
          applications: 1,
          jobs: 1,
          open_tasks: 0,
          open_interviews: records.filter((r) => r.kind === 'interview').length,
          open_offers: 0,
          overdue_tasks: 0,
          hires_last_30_days: stage === 'hired' ? 1 : 0,
          average_days_to_hire: null,
          by_job: [{ id: job.id, title: job.title, stages: { [stage]: 1 } }],
        },
      });
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON();
      const record = {
        ...body,
        id: `activity-${records.length + 1}`,
        status: body.kind === 'feedback' ? 'completed' : 'open',
        created_at: stamp,
        updated_at: stamp,
        candidate_id: body.candidate_id || (body.application_id ? 'candidate-1' : null),
        candidate_name: 'Alex Example',
        job_title: job.title,
        details: {
          location: body.location,
          technical: body.technical,
          communication: body.communication,
          experience: body.experience,
          recommendation: body.recommendation,
          amount: body.amount,
          currency: body.currency,
          pay_period: body.pay_period,
          start_date: body.start_date,
        },
      };
      records.push(record);
      return route.fulfill({ status: 201, json: record });
    }
    if (route.request().method() === 'PATCH') {
      const id = path.split('/')[4];
      const record = records.find((r) => r.id === id)!;
      const body = route.request().postDataJSON();
      Object.assign(record, body, { updated_at: '2026-10-07T12:00:00Z' });
      record.details = { ...(record.details as Record<string, unknown>), ...body };
      return route.fulfill({ json: record });
    }
    const params = new URL(route.request().url()).searchParams;
    const selected = records.filter(
      (r) =>
        (!params.get('kind') || r.kind === params.get('kind')) &&
        (!params.get('status') || r.status === params.get('status')) &&
        (!params.get('assigned_to') || r.assigned_to === params.get('assigned_to')) &&
        (!params.get('q') || String(r.title).toLowerCase().includes(params.get('q')!.toLowerCase())),
    );
    const offset = Number(params.get('offset') || 0);
    return route.fulfill({ json: { total: selected.length, items: selected.slice(offset, offset + 50) } });
  });
  await page.route('**/v1/jobs/*/pipeline?**', (route) => {
    const params = new URL(route.request().url()).searchParams;
    const found =
      (!params.get('stage') || params.get('stage') === stage) &&
      (!params.get('q') || 'alex example candidate-1'.includes(params.get('q')!.toLowerCase()));
    return route.fulfill({
      json: {
        total: found ? 1 : 0,
        counts: { [stage]: 1 },
        items: found
          ? [
              {
                id: 'application-1',
                candidate_id: 'candidate-1',
                name: 'Alex Example',
                stage,
                created_at: stamp,
              },
            ]
          : [],
      },
    });
  });
  await page.route('**/v1/applications/*/stage', async (route) => {
    const body = route.request().postDataJSON();
    changes.push(body);
    stage = body.stage;
    await route.fulfill({ json: { stage } });
  });
  return { records, changes };
}

test('recruiters move stages, schedule an interview, and export hiring reports', async ({ page }) => {
  await mock(page, 'admin', true);
  const state = await recruitingRoutes(page);
  await signIn(page);
  await page.goto(`/recruiting?tab=pipeline&job=${job.id}`);
  await expect(page.getByText('Alex Example')).toBeVisible();
  await page.getByLabel('Search pipeline candidates').fill('no match');
  await expect(page.getByText('No applications in this view')).toBeVisible();
  await page.getByLabel('Search pipeline candidates').fill('Alex');
  await expect(page.getByText('Alex Example')).toBeVisible();
  await page.getByRole('button', { name: 'Change stage', exact: true }).click();
  await page.getByLabel('New stage').selectOption('interview');
  await page.getByLabel('Reason', { exact: true }).fill('Team approved the next round');
  await page.getByRole('button', { name: 'Save stage change' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Hiring stage updated' })).toBeVisible();
  expect(state.changes).toEqual([
    { stage: 'interview', expected_stage: 'shortlisted', reason: 'Team approved the next round' },
  ]);
  await page.getByRole('button', { name: 'Rejected 0', exact: true }).click();
  await expect(page.getByText('No applications in this view')).toBeVisible();
  await page.getByRole('button', { name: 'Interviews', exact: true }).click();
  await page.getByRole('button', { name: 'Add interview', exact: true }).click();
  await page.getByLabel('Title', { exact: true }).fill('Technical round');
  await page.getByLabel('Find a candidate').fill('Alex');
  await page.getByLabel('Candidate application').selectOption('application-1');
  await page.getByLabel('Assigned team member').selectOption('owner-1');
  await page.getByLabel('Interview start', { exact: true }).fill('2026-10-10T10:00');
  await page.getByLabel('Interview end', { exact: true }).fill('2026-10-10T11:00');
  await page.getByLabel('Location or meeting link').fill('Meeting room A');
  await page.getByRole('button', { name: 'Save record', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Technical round', exact: true })).toBeVisible();
  expect(state.records[0]).toMatchObject({
    kind: 'interview',
    application_id: 'application-1',
    assigned_to: 'owner-1',
    job_id: job.id,
    location: 'Meeting room A',
  });
  expect(String(state.records[0].due_at)).toMatch(/Z$/);
  await expect(page.getByRole('link', { name: 'Alex Example · Open profile' })).toBeVisible();
  await page.getByLabel('Filter by status').selectOption('completed');
  await expect(page.getByText('No matching records', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Clear record filters' }).click();
  await page.getByRole('button', { name: 'Edit / reschedule' }).click();
  await expect(page.getByLabel('Title', { exact: true })).toHaveValue('Technical round');
  await expect(page.getByLabel('Assigned team member')).toHaveValue('owner-1');
  await page.getByLabel('Title', { exact: true }).fill('Technical round rescheduled');
  await page.getByLabel('Interview start', { exact: true }).fill('2026-10-11T10:00');
  await page.getByLabel('Interview end', { exact: true }).fill('2026-10-11T11:00');
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Technical round rescheduled', exact: true })).toBeVisible();
  expect(state.records[0].expected_updated_at).toBe(stamp);
  await page.getByLabel('Status for Technical round rescheduled').selectOption('completed');
  await expect(page.getByLabel('Status for Technical round rescheduled')).toHaveValue('completed');
  await page.getByLabel('Filter by owner').selectOption('owner-1');
  await expect(page.getByRole('heading', { name: 'Technical round rescheduled', exact: true })).toBeVisible();
  await page.getByLabel('Search records').fill('No matching title');
  await expect(page.getByText('No matching records', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Clear record filters' }).click();
  await page.getByRole('button', { name: 'Reports', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Hiring reports' })).toBeVisible();
  const downloading = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Export report CSV' }).click();
  expect((await downloading).suggestedFilename()).toBe('hiring-report.csv');
  expect((await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()).violations).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('hiring managers submit complete feedback while recruiting actions and offers stay hidden', async ({
  page,
}) => {
  await mock(page, 'hiring_manager', true);
  const state = await recruitingRoutes(page);
  await signIn(page);
  await page.goto(`/recruiting?tab=feedback&job=${job.id}`);
  await expect(page.getByRole('button', { name: 'Offers', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Add interview feedback' }).click();
  await page.getByLabel('Title', { exact: true }).fill('Panel review');
  await page.getByLabel('Candidate application').selectOption('application-1');
  await page.getByLabel('Technical / role skills').selectOption('4');
  await page.getByLabel('Communication', { exact: true }).selectOption('5');
  await page.getByLabel('Relevant experience', { exact: true }).selectOption('4');
  await page.getByLabel('Recommendation', { exact: true }).selectOption('hire');
  await page
    .getByLabel('Evidence and feedback')
    .fill('Explained trade-offs and answered technical questions');
  await page.getByRole('button', { name: 'Save record' }).click();
  await expect(page.getByRole('heading', { name: 'Panel review' })).toBeVisible();
  expect(state.records[0]).toMatchObject({
    kind: 'feedback',
    technical: 4,
    communication: 5,
    experience: 4,
    recommendation: 'hire',
  });
  await page.getByRole('button', { name: 'Tasks', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Add recruiter task' })).toHaveCount(0);
  await page.goto('/candidates/candidate-1');
  const history = page.getByRole('region', { name: 'Candidate recruiting history' });
  await expect(history.getByRole('heading', { name: 'Panel review', exact: true })).toBeVisible();
  await expect(history.getByText('Explained trade-offs and answered technical questions')).toBeVisible();
  await expect(history.getByLabel('Activity type').locator('option[value="offer"]')).toHaveCount(0);
  await history.getByLabel('Activity type').selectOption('task');
  await expect(history.getByText('No recruiting activity recorded yet')).toBeVisible();
  await history.getByLabel('Activity type').selectOption('feedback');
  await expect(history.getByRole('heading', { name: 'Panel review', exact: true })).toBeVisible();
  expect((await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()).violations).toEqual([]);
});

test('recruiters correct task deadlines and offer compensation without changing candidate links', async ({
  page,
}) => {
  await mock(page, 'admin', true);
  const state = await recruitingRoutes(page);
  await signIn(page);
  await page.goto(`/recruiting?tab=task&job=${job.id}`);
  await page.getByRole('button', { name: 'Add recruiter task' }).click();
  await page.getByLabel('Title', { exact: true }).fill('Follow up with panel');
  await page.getByLabel('Due date and time (optional)').fill('2026-10-12T09:00');
  await page.getByRole('button', { name: 'Save record', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Follow up with panel', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Edit record', exact: true }).click();
  await page.getByLabel('Due date and time (optional)').fill('');
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Edit record', exact: true })).toBeVisible();
  expect(state.records[0].due_at).toBeNull();
  await page.getByRole('button', { name: 'Offers', exact: true }).click();
  await page.getByRole('button', { name: 'Add offer record' }).click();
  await page.getByLabel('Title', { exact: true }).fill('Engineer offer');
  await page.getByLabel('Candidate application').selectOption('application-1');
  await page.getByLabel('Compensation amount').fill('1500000');
  await page.getByLabel('Intended start date').fill('2026-11-01');
  await page.getByRole('button', { name: 'Save record', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Engineer offer', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Edit record', exact: true }).click();
  await expect(page.getByLabel('Compensation amount')).toHaveValue('1500000');
  await expect(page.getByLabel('Intended start date')).toHaveValue('2026-11-01');
  await expect(page.getByLabel('Candidate application')).toHaveCount(0);
  await page.getByLabel('Compensation amount').fill('1600000');
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expect(page.getByText('INR 1600000 / year', { exact: false })).toBeVisible();
  expect(state.records[1]).toMatchObject({
    candidate_id: 'candidate-1',
    application_id: 'application-1',
    job_id: job.id,
  });
});

test('candidate recruiting history pages through records and filters across activity types', async ({
  page,
}) => {
  await mock(page, 'admin', true);
  const state = await recruitingRoutes(page);
  for (let index = 0; index < 51; index++)
    state.records.push({
      id: `note-${index}`,
      candidate_id: 'candidate-1',
      job_id: job.id,
      job_title: job.title,
      kind: 'note',
      title: `History note ${index + 1}`,
      status: 'completed',
      notes: 'Recorded recruiter conversation',
      created_at: stamp,
      updated_at: stamp,
      details: {},
    });
  await signIn(page);
  await page.goto('/candidates/candidate-1');
  const history = page.getByRole('region', { name: 'Candidate recruiting history' });
  await expect(history.getByRole('heading', { name: 'History note 1', exact: true })).toBeVisible();
  await expect(history.getByText('1–50 of 51', { exact: true })).toBeVisible();
  await history.getByRole('button', { name: 'Next activities' }).click();
  await expect(history.getByRole('heading', { name: 'History note 51', exact: true })).toBeVisible();
  await history.getByLabel('Activity type').selectOption('interview');
  await expect(history.getByText('No recruiting activity recorded yet')).toBeVisible();
  await history.getByLabel('Activity type').selectOption('note');
  await expect(history.getByRole('heading', { name: 'History note 1', exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
