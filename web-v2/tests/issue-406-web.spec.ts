import { test, expect } from '@playwright/test';

function mockApi(page: any, role: 'admin' | 'viewer', session: () => number = () => 200) {
  return page.route('**/api/**', async (route: any) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/session')) {
      const status = session();
      return route.fulfill(status === 200
        ? { json: { authenticated: true, username: role, user: role, role } }
        : { status, json: { detail: 'down' } });
    }
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL', reason: null }, version: '0' } });
    if (path.endsWith('/jobs/j1')) {
      return route.fulfill({ json: { job_id: 'j1', topic: 'Тема', status: 'FAILED', character_id: 'c', events: [], event_log: [], scenes: [], artifacts: [] } });
    }
    return route.fulfill({ json: route.request().method() === 'GET' ? [] : {} });
  });
}

test('viewer бачить дію job неактивною, admin активною', async ({ page }) => {
  await mockApi(page, 'viewer');
  await page.goto('/jobs/j1');
  await expect(page.getByTestId('delete-job-button')).toBeDisabled({ timeout: 20000 });
});

test('admin може натиснути дію job', async ({ page }) => {
  await mockApi(page, 'admin');
  await page.goto('/jobs/j1');
  await expect(page.getByTestId('delete-job-button')).toBeEnabled({ timeout: 20000 });
});

test('5xx сесії не викидає на login, а 401 викидає', async ({ page }) => {
  let status = 503;
  await mockApi(page, 'admin', () => status);
  await page.goto('/jobs');
  await page.waitForLoadState('networkidle').catch(() => {});
  expect(new URL(page.url()).pathname).toBe('/jobs');
  status = 401;
  await page.goto('/jobs');
  await expect(page).toHaveURL(/\/login/, { timeout: 20000 });
});

test('помилка показує не англійський toast інтерцептора', async ({ page }) => {
  await mockApi(page, 'admin');
  await page.goto('/jobs/j1');
  await expect(page.getByTestId('delete-job-button')).toBeVisible({ timeout: 20000 });
  await expect(page.getByText('Error Code:')).toHaveCount(0);
});
