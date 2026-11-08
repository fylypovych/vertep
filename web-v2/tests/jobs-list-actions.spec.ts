import { test, expect } from '@playwright/test';

const base = { topic: 'Тема', character_id: 'c', created_at: '2026-11-08T08:00:00Z', events: [], event_log: [], scenes: [], artifacts: [], published_to: [], publication_results: {}, stages: {}, channels: [] };

async function mock(page: any, role = 'admin') {
  const calls: string[] = [];
  const jobs = [
    { ...base, job_id: 'running', status: 'SCRIPT_GENERATING' },
    { ...base, job_id: 'paused', status: 'PAUSED' },
    { ...base, job_id: 'done', status: 'PUBLISHED' },
  ];
  await page.route('**/api/**', async (route: any) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: role, username: role, role } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL' } } });
    if (path === '/api/jobs' && request.method() === 'GET') return route.fulfill({ json: { items: jobs, total: 3, page: 1, per_page: 20, pages: 1, has_more: false } });
    if (request.method() === 'POST' && /\/api\/jobs\/[^/]+\/(pause|resume|cancel)$/.test(path)) {
      calls.push(path);
      return route.fulfill({ json: jobs[0] });
    }
    return route.fulfill({ json: request.method() === 'GET' ? [] : {} });
  });
  return calls;
}

test('список job має редагування, паузу, продовження та скасування за станом', async ({ page }) => {
  const calls = await mock(page);
  await page.goto('/jobs');
  const running = page.locator('tr', { hasText: 'running' });
  await expect(running.getByTestId('list-edit-job')).toBeEnabled({ timeout: 20000 });
  await expect(running.getByTestId('list-pause-job')).toBeVisible();
  await expect(running.getByTestId('list-cancel-job')).toBeVisible();
  await expect(page.locator('tr', { hasText: 'paused' }).getByTestId('list-resume-job')).toBeVisible();
  const done = page.locator('tr', { hasText: 'done' });
  await expect(done.getByTestId('list-pause-job')).toHaveCount(0);
  await expect(done.getByTestId('list-cancel-job')).toHaveCount(0);

  await running.getByTestId('list-pause-job').click();
  await expect.poll(() => calls).toContain('/api/jobs/running/pause');
  await page.locator('tr', { hasText: 'paused' }).getByTestId('list-resume-job').click();
  await expect.poll(() => calls).toContain('/api/jobs/paused/resume');
  await running.getByTestId('list-cancel-job').click();
  await page.locator('app-confirm-dialog button').last().click();
  await expect.poll(() => calls).toContain('/api/jobs/running/cancel');
});

test('редагування зі списку відкриває форму job', async ({ page }) => {
  await mock(page);
  await page.route('**/api/jobs/running', (route: any) => route.fulfill({ json: { ...base, job_id: 'running', status: 'SCRIPT_GENERATING' } }));
  await page.goto('/jobs');
  await page.locator('tr', { hasText: 'running' }).getByTestId('list-edit-job').click({ timeout: 20000 });
  await expect(page).toHaveURL(/\/jobs\/running\?edit=1/);
  await expect(page.getByTestId('edit-job-button')).toHaveCount(0, { timeout: 20000 });
});

test('viewer бачить дії вимкненими', async ({ page }) => {
  await mock(page, 'viewer');
  await page.goto('/jobs');
  const running = page.locator('tr', { hasText: 'running' });
  await expect(running.getByTestId('list-edit-job')).toBeDisabled({ timeout: 20000 });
  await expect(running.getByTestId('list-pause-job')).toBeDisabled();
  await expect(running.getByTestId('list-cancel-job')).toBeDisabled();
});
