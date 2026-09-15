import { test, expect, type Page } from '@playwright/test';

/**
 * Browser regression tests (Issue #52 / i.0.0.0.51).
 *
 * Contract: /api/session returns { authenticated:boolean, user?, role? }.
 * - AuthGuard пропускає ЛИШЕ справжню авторизовану сесію (authenticated:true).
 * - Неавторизований/некоректний profile веде на login.
 * - admin зберігає роль і бачить Settings.
 * - Справжній viewer не отримує admin прав.
 * - createSession надсилає Basic Authorization (не JSON body).
 */

const STATUS_MOCK = {
  core: 'OK', postgres: 'OK', redis: 'OK', storage: 'OK',
  version: '0.0.1.99',
  system: { state: 'NORMAL' },
  queue: { depth: 0, inflight: 0, dead_letter: 0 },
  scheduler: { pending: 0, next_run: null },
  orchestration: { active_jobs: 0, active_scenes: 0 },
  providers: {
    llm: { backend: 'ollama', options: [], env: '', configured: true },
    tts: { backend: 'none', options: [], env: '', configured: true },
  },
  update: { current_version: '0.0.1.99', state: 'IDLE' },
};

async function mockStatus(page: Page): Promise<void> {
  await page.route('**/api/status', (route) => route.fulfill({ json: STATUS_MOCK }));
}

async function mockSession(page: Page, session: { authenticated: boolean; user?: string | null; role?: string | null }): Promise<void> {
  await page.route('**/api/session', (route) => route.fulfill({
    json: { authenticated: session.authenticated, user: session.user ?? null, role: session.role ?? null },
  }));
}

async function mockPageApi(page: Page): Promise<void> {
  await page.route('**/api/jobs', (route) => route.request().method() === 'GET'
    ? route.fulfill({ json: [] })
    : route.fulfill({ status: 403, json: { detail: 'Insufficient role' } }));
  await page.route('**/api/workers', (route) => route.fulfill({ json: [] }));
  await page.route('**/api/characters', (route) => route.fulfill({ json: [] }));
  await page.route('**/api/brands', (route) => route.fulfill({ json: [] }));
  await page.route('**/api/workflows', (route) => route.fulfill({ json: [] }));
  await page.route('**/api/alerts', (route) => route.fulfill({ json: [] }));
}
test.describe('Session/Login contract — Issue #52 (i.0.0.0.51)', () => {
  test('AuthGuard: authenticated:false веде неавторизованого на /login', async ({ page }) => {
    await mockStatus(page);
    await mockSession(page, { authenticated: false });
    await page.goto('/');
    await expect(page.locator('h2', { hasText: 'Вхід' })).toBeVisible({ timeout: 20000 });
    await expect(page).toHaveURL(/\/login$/);
  });

  test('Login page: authenticated:false НЕ редиректить у панель (залишається на вході)', async ({ page }) => {
    await mockStatus(page);
    await mockSession(page, { authenticated: false });
    await page.goto('/login');
    await expect(page.locator('h2', { hasText: 'Вхід' })).toBeVisible({ timeout: 20000 });
    await page.waitForTimeout(400);
    await expect(page).toHaveURL(/\/login$/);
  });
test('admin: бачить Settings та header показує «Адмін»', async ({ page }) => {
    await mockPageApi(page);
    await mockStatus(page);
    await mockSession(page, { authenticated: true, user: 'ciadmin', role: 'admin' });
    await page.goto('/jobs');
    await page.waitForLoadState('networkidle').catch(() => {});
    const nav = page.locator('nav').first();
    await expect(nav.getByText('Налаштування')).toBeVisible({ timeout: 20000 });
    await expect(page.getByText('Адмін', { exact: false })).toBeVisible();
  });

  test('authenticated без role: admin НЕ знижується до viewer (Settings видно)', async ({ page }) => {
    // Регресія Issue #52: відсутня role не повинна перетворювати admin на viewer.
    await mockPageApi(page);
    await mockStatus(page);
    await mockSession(page, { authenticated: true, user: 'ciadmin', role: null });
    await page.goto('/jobs');
    await page.waitForLoadState('networkidle').catch(() => {});
    const nav = page.locator('nav').first();
    await expect(nav.getByText('Налаштування')).toBeVisible({ timeout: 20000 });
  });

  test('viewer: не отримує admin прав — Settings приховано, header показує «Переглядач»', async ({ page }) => {
    await mockStatus(page);
    await mockSession(page, { authenticated: true, user: 'cireader', role: 'viewer' });
    await mockPageApi(page);
    const nav = page.locator('nav').first();
    await page.goto('/jobs');
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(nav.getByText('Налаштування')).toHaveCount(0, { timeout: 20000 });
    await expect(page.getByText('Переглядач', { exact: false }).first()).toBeVisible();
  });

  test('createSession надсилає Basic Authorization (не JSON body)', async ({ page }) => {
    await mockStatus(page);
    let authed = false;
    let capturedAuth: string | null = null;
    let capturedPostBody: string | null = null;

    await page.route('**/api/session', async (route) => {
      const req = route.request();
      const m = req.method();
      const path = new URL(req.url()).pathname;
      if (m === 'POST' && path.endsWith('/session')) {
        const headers = req.headers();
        capturedAuth = headers['authorization'] || headers['Authorization'] || null;
        capturedPostBody = req.postData();
        authed = true;
        return route.fulfill({ json: { authenticated: true, user: 'ciadmin', role: 'admin' } });
      }
      return route.fulfill({ json: { authenticated: authed, user: authed ? 'ciadmin' : null, role: authed ? 'admin' : null } });
    });

    await page.goto('/login');
    await expect(page.locator('h2', { hasText: 'Вхід' })).toBeVisible({ timeout: 20000 });

    await page.locator('input[type="text"]').fill('ciadmin');
    await page.locator('input[type="password"]').fill('secret');
    await page.getByRole('button', { name: /Увійти/i }).click();

    await expect(capturedAuth).not.toBeNull();
    expect(capturedAuth).toBe(`Basic ${Buffer.from('ciadmin:secret').toString('base64')}`);
    expect(capturedPostBody).toBeNull();
  });
});