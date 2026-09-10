import { test, expect } from '@playwright/test';

test('Завдання з Telegram з’являється без перезавантаження сторінки', async ({ page }) => {
  let created = false;
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown = [];
    if (path.endsWith('/session')) body = { username: 'admin', role: 'admin' };
    if (path === '/api/jobs' && created) body = [{
      job_id: 'telegram-audit', topic: 'Тема з Telegram', status: 'STORYBOARD_QUEUED',
      source: 'telegram:42:1', created_at: '2026-09-10T18:00:00Z',
    }];
    await route.fulfill({ json: body });
  });
  await page.goto('/jobs');
  await expect(page.getByTestId('jobs-empty')).toBeVisible();
  created = true;
  await expect(page.getByRole('cell', { name: 'telegram-audit', exact: true })).toBeVisible({ timeout: 12000 });
});
