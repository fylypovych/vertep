import { test, expect } from '@playwright/test';

test('Логи: курсор "старіші" довантажує сторінку та веде до Job/вузла', async ({ page }) => {
  const requested: string[] = [];
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/session') return route.fulfill({ json: { authenticated: true, user: 'admin', role: 'admin' } });
    if (url.pathname === '/api/status') return route.fulfill({ json: { system: { state: 'NORMAL' } } });
    if (url.pathname === '/api/logs') {
      const before = url.searchParams.get('before');
      requested.push(before ?? '');
      if (before) {
        return route.fulfill({ json: [{ timestamp: '2026-01-01T10:00:00+00:00', level: 'ERROR', message: 'older', job_id: 'job-1', node_name: 'gpu-1' }] });
      }
      const limit = Number(url.searchParams.get('limit') ?? '200');
      const rows = Array.from({ length: limit }, (_, i) => ({
        timestamp: `2026-01-02T10:00:${String(59 - (i % 60)).padStart(2, '0')}+00:00`,
        level: 'INFO',
        message: i === 0 ? 'newest' : `row-${i}`,
        job_id: 'job-1',
        node_name: 'gpu-1',
        action: 'render',
      }));
      return route.fulfill({ json: rows });
    }
    return route.fulfill({ json: [] });
  });

  await page.goto('/logs');
  await expect(page.getByText('newest', { exact: true })).toBeVisible();
  await expect(page.getByTestId('logs-load-older')).toBeVisible();

  await expect(page.getByRole('link', { name: 'gpu-1' }).first()).toHaveAttribute('href', '/workers/gpu-1');
  await expect(page.getByRole('link', { name: 'Job job-1' }).first()).toHaveAttribute('href', '/jobs/job-1');

  await page.getByTestId('logs-load-older').click();
  await expect(page.getByText('older', { exact: true })).toBeVisible();
  await expect(page.getByText('newest', { exact: true })).toBeVisible();
  expect(requested.some(value => value !== '')).toBe(true);
});