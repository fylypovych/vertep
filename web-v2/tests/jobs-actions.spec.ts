import { test, expect } from '@playwright/test';

function mockApi(page: any, actions: { method: string; url: string; handler: (route: any) => void }[] = []) {
  return page.route('**/api/**', async (route: any) => {
    const r = route.request();
    const p = new URL(r.url()).pathname;
    const m = r.method();
    const action = actions.find(a => a.url === p && a.method === m);
    if (action) { action.handler(route); return; }
    if (p.endsWith('/session')) return route.fulfill({ json: { username: 'admin', role: 'admin' } });
    if (p.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL' } } });
    if (p.endsWith('/tasks/queue')) return route.fulfill({ json: { ready: [], inflight: [] } });
    if (p.endsWith('/tasks/dead-letter')) return route.fulfill({ json: [] });
    if (p.endsWith('/characters')) return route.fulfill({ json: [] });
    if (p.endsWith('/brands')) return route.fulfill({ json: [] });
    if (p.endsWith('/workflows')) return route.fulfill({ json: [] });
    if (p.endsWith('/settings/secrets')) return route.fulfill({ json: { secrets: {} } });
    return route.fulfill({ json: [] });
  });
}

const newJob = { job_id: 'j1', topic: 'Тест', status: 'READY', priority: 5, created_at: '2025-01-01T00:00:00Z', character_id: '', source: 'manual', retries: 0, approved: false, approval_status: 'none', published_to: [], publication_results: {}, version: 1, active_task_ids: [], completed_task_ids: [], stages: {}, scenes: [], artifacts: [], events: [], brand_id: '', aspect_ratio: '16:9', output_preset: 'youtube', task_type: 'image', min_vram_mb: 4096, max_retries: 3 };

test.describe('Jobs Actions', () => {
  let apiCalls: string[] = [];

  test.beforeEach(async ({ page }) => {
    apiCalls = [];
    await mockApi(page, [
      { method: 'GET', url: '/api/jobs', handler: (route) => route.fulfill({ json: [newJob] }) },
      { method: 'POST', url: '/api/jobs/j1/actions', handler: (route) => { apiCalls.push(route.request().postDataJSON().action); route.fulfill({ json: { ...newJob, status: 'PAUSED' } }); } },
      { method: 'POST', url: '/api/jobs/j1/publish', handler: (route) => { apiCalls.push('publish'); route.fulfill({ json: { ...newJob, status: 'PUBLISHING' } }); } },
    ]);
  });

  test('Створення завдання', async ({ page }) => {
    await page.goto('/jobs');
    await page.getByTestId('create-job-button').click();
    await page.getByPlaceholder('Тема завдання').fill('Нове завдання');
    await page.getByRole('button', { name: 'Створити', exact: true }).click();
    await expect(page.getByText('Нове завдання')).toBeVisible();
  });

  test('Видалення завдання', async ({ page }) => {
    await page.goto('/jobs');
    page.on('dialog', dialog => dialog.accept());
    const deleteBtn = page.locator('[data-testid="jobs-table"] tr').first().locator('button:has-text("Видалити")');
    await deleteBtn.click();
    await page.waitForTimeout(500);
  });
});
