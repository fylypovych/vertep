import { test, expect } from '@playwright/test';

// #504 AC3: the separately deployed panel shows its build and warns honestly
// when CORE speaks another API major instead of silently misbehaving.
async function open(page: any, apiMajor: number | undefined) {
  await page.route('**/ui-version.json', (route: any) => route.fulfill({ json: { version: '9.9.9-panel', api_major: 1 } }));
  await page.route('**/api/**', async (route: any) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: 'admin', username: 'admin', role: 'admin' } });
    if (path.endsWith('/status')) return route.fulfill({ json: { version: '0.0.4.11', api_major: apiMajor, system: { state: 'NORMAL' } } });
    return route.fulfill({ json: route.request().method() === 'GET' ? [] : {} });
  });
  await page.goto('/jobs');
}

test('панель показує свою збірку і не попереджає за сумісного API', async ({ page }) => {
  await open(page, 1);
  await expect(page.getByTestId('panel-version')).toContainText('9.9.9-panel', { timeout: 20000 });
  await expect(page.getByTestId('panel-incompatible')).toHaveCount(0);
});

test('несумісна версія API CORE показує зрозуміле попередження', async ({ page }) => {
  await open(page, 2);
  await expect(page.getByTestId('panel-incompatible')).toBeVisible({ timeout: 20000 });
});
