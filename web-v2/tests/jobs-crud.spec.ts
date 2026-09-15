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

function mockApi(page: any, jobs: any[]) {
  const mutable = [...jobs];
  return page.route('**/api/**', async (route: any) => {
    const r = route.request();
    const p = new URL(r.url()).pathname;
    const m = r.method();
    if (p.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: 'admin', role: 'admin' } });
    if (p.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL' } } });
    if (p === '/api/jobs' && m === 'GET') return route.fulfill({ json: paginated(mutable, r.url()) });
    if (p === '/api/jobs' && m === 'POST') {
      const created = { job_id: 'new_1', ...r.postDataJSON(), status: 'NEW', created_at: new Date().toISOString() };
      mutable.push(created);
      return route.fulfill({ json: created });
    }
    if (p.match(/\/api\/jobs\/[^/]+$/) && m === 'DELETE') return route.fulfill({ json: { ok: true } });
    return route.fulfill({ json: [] });
  });
}

test.describe('Jobs CRUD', () => {
  const jobs = [
    { job_id: 'j1', topic: 'Коти', status: 'NEW', priority: 5, created_at: '2025-01-01T00:00:00Z', character_id: '', source: 'manual', retries: 0, approved: false, approval_status: 'none', published_to: [], publication_results: {}, version: 1, active_task_ids: [], completed_task_ids: [], stages: {}, scenes: [], artifacts: [], events: [], brand_id: '', aspect_ratio: '16:9', output_preset: 'youtube', task_type: 'image', min_vram_mb: 4096, max_retries: 3 },
    { job_id: 'j2', topic: 'Природа', status: 'SCRIPT_GENERATING', priority: 3, created_at: '2025-01-02T00:00:00Z', character_id: '', source: 'manual', retries: 0, approved: false, approval_status: 'none', published_to: [], publication_results: {}, version: 1, active_task_ids: [], completed_task_ids: [], stages: {}, scenes: [], artifacts: [], events: [], brand_id: '', aspect_ratio: '16:9', output_preset: 'youtube', task_type: 'image', min_vram_mb: 4096, max_retries: 3 },
    { job_id: 'j3', topic: 'Технології', status: 'FAILED', priority: 5, created_at: '2025-01-03T00:00:00Z', character_id: '', source: 'manual', retries: 3, approved: false, approval_status: 'none', published_to: [], publication_results: {}, version: 1, active_task_ids: [], completed_task_ids: [], stages: {}, scenes: [], artifacts: [], events: [], brand_id: '', aspect_ratio: '16:9', output_preset: 'youtube', task_type: 'image', min_vram_mb: 4096, max_retries: 3 },
  ];

  test('Список завдань відображається', async ({ page }) => {
    await mockApi(page, jobs);
    await page.goto('/jobs');
    await expect(page.getByTestId('jobs-page')).toBeVisible();
    await expect(page.getByText('Коти')).toBeVisible();
  });

  test('Фільтрація за статусом', async ({ page }) => {
    await mockApi(page, jobs);
    await page.goto('/jobs');
    await page.selectOption('[data-testid="jobs-status-filter"]', 'active');
    await expect(page.getByText('Природа')).toBeVisible();
  });

  test('Пошук за темою', async ({ page }) => {
    await mockApi(page, jobs);
    await page.goto('/jobs');
    await page.getByTestId('jobs-search').fill('Коти');
    await expect(page.getByText('Коти')).toBeVisible();
    await expect(page.getByText('Природа')).not.toBeVisible();
  });
});
