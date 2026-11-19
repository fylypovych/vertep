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

// #504 AC3: re-check while the panel stays open (no page reload).
async function openLive(page: any, state: { up: boolean; apiMajor: number }) {
  await page.clock.install();
  await page.route('**/ui-version.json', (route: any) =>
    route.fulfill({ json: { version: '9.9.9-panel', commit_sha: 'abcdef1234567890', api_major: 1 } }));
  await page.route('**/api/**', async (route: any) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: 'admin', username: 'admin', role: 'admin' } });
    if (path.endsWith('/status')) {
      if (!state.up) return route.fulfill({ status: 502, body: 'Bad Gateway' });
      return route.fulfill({ json: { version: '0.0.4.13', api_major: state.apiMajor, system: { state: 'NORMAL' } } });
    }
    return route.fulfill({ json: route.request().method() === 'GET' ? [] : {} });
  });
  await page.goto('/jobs');
}

test('панель показує SHA своєї збірки', async ({ page }) => {
  await openLive(page, { up: true, apiMajor: 1 });
  await expect(page.getByTestId('panel-version')).toContainText('abcdef123456', { timeout: 20000 });
});

test('після недоступності CORE панель сама відновлює зʼєднання без перезавантаження', async ({ page }) => {
  const state = { up: false, apiMajor: 1 };
  await openLive(page, state);
  await expect(page.getByText('Vertep v...')).toBeVisible({ timeout: 20000 });
  state.up = true;
  await page.clock.fastForward('00:31');
  await expect(page.getByText('Vertep v0.0.4.13')).toBeVisible({ timeout: 20000 });
  await expect(page.getByTestId('panel-incompatible')).toHaveCount(0);
});

test('оновлення CORE до несумісного API під час сесії показує попередження', async ({ page }) => {
  const state = { up: true, apiMajor: 1 };
  await openLive(page, state);
  await expect(page.getByText('Vertep v0.0.4.13')).toBeVisible({ timeout: 20000 });
  await expect(page.getByTestId('panel-incompatible')).toHaveCount(0);
  state.apiMajor = 2;
  await page.clock.fastForward('00:31');
  await expect(page.getByTestId('panel-incompatible')).toBeVisible({ timeout: 20000 });
});
