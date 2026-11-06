import { test, expect } from '@playwright/test';

function mockApi(page: any, session: () => number, system: () => Record<string, unknown>, extra: Record<string, () => unknown> = {}) {
  return page.route('**/api/**', async (route: any) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/session')) {
      const status = session();
      return route.fulfill(status === 200
        ? { json: { authenticated: true, username: 'admin', user: 'admin', role: 'admin' } }
        : { status, json: { detail: 'x' } });
    }
    if (path.endsWith('/status')) return route.fulfill({ json: { system: system(), version: '0' } });
    for (const [suffix, answer] of Object.entries(extra)) {
      if (path.endsWith(suffix)) return route.fulfill({ json: answer() });
    }
    return route.fulfill({ json: route.request().method() === 'GET' ? [] : {} });
  });
}

const normal = () => ({ state: 'NORMAL', reason: null });

for (const status of [403, 404, 409, 500, 503]) {
  test(`сесія з відповіддю ${status} не викидає на login`, async ({ page }) => {
    await mockApi(page, () => status, normal);
    await page.goto('/jobs');
    await page.waitForLoadState('networkidle').catch(() => {});
    expect(new URL(page.url()).pathname).toBe('/jobs');
  });
}

test('сесія з 401 веде на login', async ({ page }) => {
  await mockApi(page, () => 401, normal);
  await page.goto('/jobs');
  await expect(page).toHaveURL(/\/login/, { timeout: 20000 });
});

test('обрив зʼєднання не викидає на login', async ({ page }) => {
  await mockApi(page, () => 200, normal);
  await page.route('**/api/session', (route: any) => route.abort());
  await page.goto('/jobs');
  await page.waitForLoadState('networkidle').catch(() => {});
  expect(new URL(page.url()).pathname).toBe('/jobs');
});

test('під час оновлення створити job не можна, а в MAINTENANCE без оновлення можна', async ({ page }) => {
  let system: Record<string, unknown> = { state: 'MAINTENANCE', reason: 'drain', operation_id: 'a'.repeat(32) };
  await mockApi(page, () => 200, () => system);
  await page.goto('/jobs');
  await expect(page.getByTestId('create-job-button')).toBeDisabled({ timeout: 20000 });
  system = { state: 'MAINTENANCE', reason: 'drain', operation_id: 'restore:snap' };
  await page.reload();
  await expect(page.getByTestId('create-job-button')).toBeEnabled({ timeout: 20000 });
});

test('статичний режим блокується, поки є динамічні завдання, і стає доступним після них', async ({ page }) => {
  let active = 2;
  const saved: unknown[] = [];
  await mockApi(page, () => 200, normal, {
    '/settings/workflow-mode': () => ({ mode: 'dynamic', default: 'static', dynamic_available: true, active_dynamic_jobs: active }),
  });
  await page.route('**/api/settings/workflow-mode', async (route: any) => {
    if (route.request().method() === 'GET') {
      return route.fulfill({ json: { mode: saved.length ? 'static' : 'dynamic', default: 'static', dynamic_available: true, active_dynamic_jobs: active } });
    }
    saved.push(route.request().postDataJSON());
    return route.fulfill({ json: { mode: 'static', default: 'static', dynamic_available: true, active_dynamic_jobs: 0 } });
  });
  await page.goto('/settings?tab=workflow-mode');
  await page.getByTestId('workflow-mode-select').selectOption('static', { timeout: 20000 });
  await expect(page.getByTestId('workflow-mode-blocked')).toContainText('2');
  await expect(page.getByTestId('workflow-mode-apply')).toBeDisabled();
  active = 0;
  await page.reload();
  await page.getByTestId('workflow-mode-select').selectOption('static', { timeout: 20000 });
  await expect(page.getByTestId('workflow-mode-blocked')).toHaveCount(0);
  await page.getByTestId('workflow-mode-apply').click();
  await page.getByRole('button', { name: /Підтвердити|Так|Застосувати|Змінити/ }).last().click({ timeout: 3000 }).catch(() => {});
  await expect.poll(() => saved.length, { timeout: 10000 }).toBe(1);
});
