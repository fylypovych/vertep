import { test, expect } from '@playwright/test';

const mockWorkers = [
  { node_id: 'node_1', node_name: 'gpu-worker-1', role: 'gpu', status: 'FREE', capabilities: ['image_generation', 'video_generation'], vram_mb: 8192, cpu_load: 10 },
  { node_id: 'node_2', node_name: 'text-worker-1', role: 'text', status: 'ONLINE', capabilities: ['text_generation'], vram_mb: 0, cpu_load: 5 },
];

function mockApi(page: any) {
  return page.route('**/api/**', async (route: any) => {
    const r = route.request();
    const p = new URL(r.url()).pathname;
    if (p.endsWith('/session')) return route.fulfill({ json: { username: 'admin', role: 'admin' } });
    if (p.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL' }, version: '0.0.1.30' } });
    if (p === '/api/workers' && r.method() === 'GET') return route.fulfill({ json: mockWorkers });
    if (p.endsWith('/settings/secrets')) return route.fulfill({ json: { secrets: {} } });
    if (p.endsWith('/settings/logo')) return route.fulfill({ status: 404 });
    return route.fulfill({ json: [] });
  });
}

test.describe('Workers', () => {
  test('Список воркерів відображається', async ({ page }) => {
    await mockApi(page);
    await page.goto('/workers');
    await expect(page.getByTestId('workers-page')).toBeVisible();
    await expect(page.getByText('gpu-worker-1')).toBeVisible();
    await expect(page.getByText('text-worker-1')).toBeVisible();
  });

  test('Деталі воркера', async ({ page }) => {
    await page.route('**/api/**', async (route: any) => {
      const p = new URL(route.request().url()).pathname;
      if (p.endsWith('/session')) return route.fulfill({ json: { username: 'admin', role: 'admin' } });
      if (p.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL' } } });
      if (p === '/api/nodes/node_1') return route.fulfill({ json: { ...mockWorkers[0], hardware: { cpu_count: 8, ram_total_mb: 16384, hostname: 'gpu-box' }, runtime: {}, self_test: null, update_state: {} } });
      if (p.endsWith('/system/roles')) return route.fulfill({ json: { active_roles: [], available_roles: [] } });
      return route.fulfill({ json: [] });
    });
    await page.goto('/workers/node_1');
    await expect(page.getByText('gpu-worker-1')).toBeVisible();
  });

  test('Пошук воркерів', async ({ page }) => {
    await mockApi(page);
    await page.goto('/workers');
    await page.getByTestId('workers-search').fill('gpu');
    await expect(page.getByText('gpu-worker-1')).toBeVisible();
  });
});
