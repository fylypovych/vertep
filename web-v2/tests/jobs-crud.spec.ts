import { test, expect } from '@playwright/test';

function mockApi(page: any, jobs: any[]) {
  return page.route('**/api/**', async (route: any) => {
    const r = route.request();
    const p = new URL(r.url()).pathname;
    const m = r.method();
    if (p.endsWith('/session')) return route.fulfill({ json: { username: 'admin', role: 'admin' } });
    if (p.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL' } } });
    if (p === '/api/jobs' && m === 'GET') return route.fulfill({ json: jobs });
    if (p === '/api/jobs' && m === 'POST') return route.fulfill({ json: { job_id: 'new_1', ...r.postDataJSON(), status: 'NEW', created_at: new Date().toISOString() } });
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
