import { test, expect } from '@playwright/test';

async function mockCommon(page: import('@playwright/test').Page, alerts: () => unknown) {
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/session') return route.fulfill({ json: { authenticated: true, user: 'admin', role: 'admin' } });
    if (path === '/api/status') return route.fulfill({ json: { system: { state: 'NORMAL' } } });
    if (path === '/api/alerts') {
      const value = alerts();
      if (value instanceof Error) return route.fulfill({ status: 500, json: { detail: 'boom' } });
      return route.fulfill({ json: value });
    }
    return route.fulfill({ json: [] });
  });
}

test('Алерти: порожній стан, помилка та підтвердження firing-алерту', async ({ page }) => {
  let body: unknown = [];
  await mockCommon(page, () => body);

  await page.goto('/alerts');
  await expect(page.getByTestId('empty-state')).toBeVisible();

  // Failure surfaces as a firing alert bound to the node entity.
  body = [{ id: '7', severity: 'error', type: 'WORKER_OFFLINE', node_name: 'gpu-1', state: 'firing', message: 'node down' }];
  await page.getByRole('button', { name: 'Оновити' }).click();
  await expect(page.getByText('WORKER_OFFLINE')).toBeVisible();
  await expect(page.getByText('Node: gpu-1')).toBeVisible();

  const ackCalls: string[] = [];
  page.on('request', req => { if (req.url().includes('/acknowledge')) ackCalls.push(req.url()); });

  // After ack the node alert is acknowledged: reload returns the acknowledged state.
  body = [{ id: '7', severity: 'error', type: 'WORKER_OFFLINE', node_name: 'gpu-1', state: 'acknowledged', message: 'node down' }];
  await page.getByTestId('alert-ack-7').click();
  await expect(page.getByTestId('alert-ack-7')).toHaveCount(0);
  await expect(page.getByText('Стан: acknowledged')).toBeVisible();
  expect(ackCalls.some(url => url.includes('/api/alerts/7/acknowledge'))).toBe(true);

  // Backend error surfaces the error state with a retry action.
  body = new Error('down');
  await page.getByRole('button', { name: 'Оновити' }).click();
  await expect(page.getByTestId('error-state')).toBeVisible();
});