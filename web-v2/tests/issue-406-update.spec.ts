import { test, expect } from '@playwright/test';

function mockApi(page: any, counts: Record<string, number>, role = 'admin') {
  return page.route('**/api/**', async (route: any) => {
    const path = new URL(route.request().url()).pathname;
    counts[path] = (counts[path] || 0) + 1;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, username: role, user: role, role } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL', reason: null }, version: '0' } });
    if (path.endsWith('/system/update')) return route.fulfill({ json: { state: 'RUNNING', phase: 'UPDATING', progress: 60 } });
    if (path.includes('/rolling')) {
      return route.fulfill({ json: route.request().method() === 'GET' ? { state: 'AWAITING_PROMOTION' } : {} });
    }
    if (path.endsWith('/jobs/j1/retry')) return route.fulfill({ status: 500, body: 'boom' });
    if (path.endsWith('/jobs/j1')) {
      return route.fulfill({ json: { job_id: 'j1', topic: 'Тема', status: 'FAILED', character_id: 'c', events: [], event_log: [], scenes: [], artifacts: [], published_to: [], publication_results: {}, stages: {} } });
    }
    return route.fulfill({ json: route.request().method() === 'GET' ? [] : {} });
  });
}

test('активне оновлення опитується, а таймер хедера оновлює policy', async ({ page }) => {
  const counts: Record<string, number> = {};
  await page.clock.install();
  await mockApi(page, counts);
  await page.goto('/settings?tab=update');
  await expect(page.getByTestId('update-promote-canary')).toBeVisible({ timeout: 20000 });
  const updates = counts['/api/system/update'];
  const statuses = counts['/api/status'];
  await page.clock.runFor(31_000);
  await expect.poll(() => counts['/api/system/update']).toBeGreaterThan(updates);
  // Header timer asks twice: its own label and the policy.
  await expect.poll(() => counts['/api/status']).toBeGreaterThanOrEqual(statuses + 2);
});

test('дія canary виконується лише після підтвердження', async ({ page }) => {
  const posts: string[] = [];
  await mockApi(page, {});
  page.on('request', (request: any) => { if (request.method() === 'POST' && request.url().includes('rolling')) posts.push(request.url()); });
  await page.goto('/settings?tab=update');
  await page.getByTestId('update-rollback-canary').click();
  await expect(page.locator('#confirm-title')).toContainText('Відкотити canary');
  expect(posts).toEqual([]);
  await page.getByRole('button', { name: 'Підтвердити', exact: true }).click();
  await expect.poll(() => posts.length).toBe(1);
});

test('помилка дії показує одне українське повідомлення', async ({ page }) => {
  await mockApi(page, {});
  await page.goto('/jobs/j1');
  await page.locator('button', { hasText: /Повторити/ }).first().click();
  await page.locator('app-confirm-dialog button').last().click();
  await expect(page.getByText('Сервер тимчасово недоступний')).toHaveCount(1, { timeout: 20000 });
  await expect(page.getByText('Error Code:')).toHaveCount(0);
});

test('viewer не може редагувати job', async ({ page }) => {
  await mockApi(page, {}, 'viewer');
  await page.goto('/jobs/j1');
  await expect(page.getByTestId('edit-job-button')).toBeDisabled({ timeout: 20000 });
});
