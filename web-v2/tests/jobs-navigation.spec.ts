import { test, expect } from '@playwright/test';

test('Меню та вкладки перемикають список і виконання без подвійного виділення', async ({ page }) => {
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/session') return route.fulfill({ json: { authenticated: true, user: 'admin', role: 'admin' } });
    if (path === '/api/status') return route.fulfill({ json: { system: { state: 'NORMAL' } } });
    if (path === '/api/queue') return route.fulfill({ json: { ready: [], inflight: [], dead_letter: [] } });
    return route.fulfill({ json: [] });
  });
  await page.goto('/jobs');
  const nav = page.getByTestId('sidebar-nav');
  const jobs = nav.getByText('Завдання', { exact: true });
  const queue = nav.getByText('Виконання', { exact: true });
  await expect(jobs.locator('..')).toHaveAttribute('aria-current', 'page');
  await queue.click();
  await expect(page).toHaveURL(/\/queue$/);
  await expect(page.getByTestId('queue-page')).toBeVisible();
  await expect(queue.locator('..')).toHaveAttribute('aria-current', 'page');
  await expect(jobs.locator('..')).not.toHaveAttribute('aria-current', 'page');
  await jobs.click();
  await expect(page).toHaveURL(/\/jobs$/);
  await expect(page.getByTestId('queue-page')).toHaveCount(0);
  await page.getByRole('button', { name: 'Виконання', exact: true }).click();
  await expect(page).toHaveURL(/\/queue$/);
  await expect(page.getByTestId('queue-page')).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/\/jobs$/);
  await expect(page.getByTestId('queue-page')).toHaveCount(0);
});
