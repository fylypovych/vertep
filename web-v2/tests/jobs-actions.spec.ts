import { test, expect } from '@playwright/test';

const STATUS_GROUPS: Record<string, string[]> = {
  active: ['SCRIPT_GENERATING', 'STORYBOARD_GENERATING', 'ASSET_GENERATION', 'VIDEO_GENERATION', 'ASSEMBLY', 'PUBLISHING', 'TTS_GENERATING'],
  queued: ['NEW', 'SCRIPT_QUEUED', 'STORYBOARD_QUEUED'],
  waiting: ['WAITING_FOR_SYSTEM', 'PENDING_APPROVAL', 'SCRIPT_PENDING_APPROVAL', 'SCRIPT_REVISION_REQUESTED', 'STORYBOARD_PENDING_APPROVAL', 'STORYBOARD_REVISION_REQUESTED', 'VIDEO_PENDING_APPROVAL', 'VIDEO_REVISION_REQUESTED'],
  completed: ['SCRIPT_READY', 'SCRIPT_APPROVED', 'STORYBOARD_APPROVED', 'ASSETS_READY', 'VIDEO_READY', 'VIDEO_APPROVED', 'READY', 'PUBLISHED'],
  failed: ['FAILED', 'SCRIPT_FAILED', 'STORYBOARD_FAILED', 'VIDEO_FAILED'],
};

function paginated(jobs: any[], url: string) {
  const u = new URL(url);
  const statusGroup = u.searchParams.get('status_group') || '';
  const search = (u.searchParams.get('search') || '').toLowerCase();
  const page = Number(u.searchParams.get('page') || '1');
  const perPage = Number(u.searchParams.get('per_page') || '10');
  let items = jobs;
  if (statusGroup) {
    const allowed = STATUS_GROUPS[statusGroup] || [];
    items = items.filter(j => allowed.includes(j.status));
  }
  if (search) {
    items = items.filter(j => (j.job_id + ' ' + j.topic).toLowerCase().includes(search));
  }
  const total = items.length;
  const pages = Math.max(1, Math.ceil(total / perPage));
  const chunk = items.slice((page - 1) * perPage, page * perPage);
  return { items: chunk, total, page, per_page: perPage, pages, has_more: page < pages };
}

function mockApi(page: any, actions: { method: string; url: string; handler: (route: any) => void }[] = []) {
  const mutable: any[] = [];
  return page.route('**/api/**', async (route: any) => {
    const r = route.request();
    const p = new URL(r.url()).pathname;
    const m = r.method();
    const action = actions.find(a => a.url === p && a.method === m);
    if (action) { action.handler(route); return; }
    if (p.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: 'admin', role: 'admin' } });
    if (p.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL' } } });
    if (p === '/api/jobs' && m === 'GET') return route.fulfill({ json: paginated(mutable, r.url()) });
    if (p === '/api/jobs' && m === 'POST') {
      const created = { job_id: 'new_1', ...r.postDataJSON(), status: 'READY', created_at: new Date().toISOString() };
      mutable.push(created);
      return route.fulfill({ json: created });
    }
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
