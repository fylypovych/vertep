import { test, expect } from '@playwright/test';

test('ресурси показують використане і загальне', async ({ page }) => {
  await page.route('**/api/**', async (route: any) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, username: 'admin', user: 'admin', role: 'admin' } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL' }, version: '0', resources: {
      cpu: 25, ram: 50, disk: 25, cpu_cores: 8, cpu_used_cores: 2, ram_used_bytes: 8 * 1024 ** 3, ram_total_bytes: 16 * 1024 ** 3,
      disk_used_bytes: 5 * 1024 ** 3, disk_total_bytes: 20 * 1024 ** 3 } } });
    return route.fulfill({ json: [] });
  });
  await page.goto('/');
  const resources = page.getByTestId('resources');
  await expect(resources).toContainText('(5.0 GB | 20 GB)', { timeout: 20000 });
  await expect(resources).toContainText('(8.0 GB | 16 GB)');
  await expect(resources).toContainText('(2 | 8 CPU)');
});
