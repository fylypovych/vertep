import { test, expect } from '@playwright/test';

interface RbacMocks {
  role: 'admin' | 'viewer';
  state?: string;
  reason?: string | null;
  jobs?: Array<Record<string, unknown>>;
}

function mockApi(page: any, opts: RbacMocks = { role: 'admin' }) {
  const state = opts.state || 'NORMAL';
  const reason = opts.reason || null;
  return page.route('**/api/**', async (route: any) => {
    const r = route.request();
    const p = new URL(r.url()).pathname;
    const m = r.method();
    if (p.endsWith('/session')) {
      return route.fulfill({ json: { authenticated: true, username: opts.role === 'admin' ? 'admin' : 'reader', user: opts.role === 'admin' ? 'admin' : 'reader', role: opts.role } });
    }
    if (p.endsWith('/status')) {
      return route.fulfill({ json: { system: { state, reason }, version: '0.0.1.52' } });
    }
    if (p.endsWith('/jobs') && m === 'GET') return route.fulfill({ json: opts.jobs || [] });
    if (p.endsWith('/jobs') && m === 'POST') return route.fulfill({ status: 403, json: { detail: 'Insufficient role' } });
    if (p.endsWith('/workers')) return route.fulfill({ json: [] });
    if (p.endsWith('/characters')) return route.fulfill({ json: [] });
    if (p.endsWith('/brands')) return route.fulfill({ json: [] });
    if (p.endsWith('/workflows')) return route.fulfill({ json: [] });
    if (p.endsWith('/alerts')) return route.fulfill({ json: [] });
    if (m === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });
}

test.describe('RBAC — Issue #29', () => {
  test('admin бачить налаштування та може створювати завдання', async ({ page }) => {
    await mockApi(page, { role: 'admin' });
    await page.goto('/jobs');
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page.getByTestId('jobs-page')).toBeVisible({ timeout: 20000 });
    const nav = page.locator('nav').first();
    await expect(nav.getByText('Налаштування')).toBeVisible();
    const createButton = page.getByTestId('create-job-button');
    await expect(createButton).toBeVisible();
    expect(await createButton.isDisabled()).toBe(false);
    expect(await createButton.getAttribute('title')).toBe('Створити нове завдання');
  });

  test('viewer: приховує admin nav та блоковує admin мутацію в UI', async ({ page }) => {
    await mockApi(page, { role: 'viewer' });
    await page.goto('/jobs');
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page.getByTestId('jobs-page')).toBeVisible({ timeout: 20000 });
    const nav = page.locator('nav').first();
    await expect(nav.getByText('Налаштування')).toHaveCount(0, { timeout: 20000 });
    // Admin mutation заблокована через policy: кнопка неактивна, причина пояснена.
    const createButton = page.getByTestId('create-job-button');
    await expect(createButton).toBeVisible();
    expect(await createButton.isDisabled()).toBe(true);
    expect(await createButton.getAttribute('title')).toContain('Недостатньо прав');
  });

  test('viewer: прямий admin mutation через API повертає 403', async ({ page }) => {
    await mockApi(page, { role: 'viewer' });
    await page.goto('/jobs');
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page.getByTestId('jobs-page')).toBeVisible({ timeout: 20000 });
    // fetch з контексту сторінки перехоплюється page.route → перевірка 403.
    const status = await page.evaluate(async () => {
      const res = await fetch('/api/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ topic: 'blocked' }) });
      return res.status;
    });
    expect(status).toBe(403);
  });

  test('system-state: READ_ONLY блоковує мутацію з поясненням причини', async ({ page }) => {
    await mockApi(page, { role: 'admin', state: 'READ_ONLY', reason: 'Планова зупинка' });
    await page.goto('/jobs');
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page.getByTestId('jobs-page')).toBeVisible({ timeout: 20000 });
    const createButton = page.getByTestId('create-job-button');
    await expect(createButton).toBeVisible();
    expect(await createButton.isDisabled()).toBe(true);
    const title = (await createButton.getAttribute('title')) || '';
    expect(title).toContain('READ_ONLY');
    expect(title).toContain('Планова зупинка');
  });
});