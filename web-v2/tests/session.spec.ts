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
    await expect(nav.locator('a[href="/settings?tab=system"]')).toBeVisible({ timeout: 20000 });
    await expect(page.getByText('Адмін', { exact: true })).toBeVisible();
    await nav.locator('a[href="/settings?tab=system"]').click();
    await expect(page.getByTestId('settings-page')).toBeVisible();
  });

  test('authenticated без role: некоректна сесія веде на login', async ({ page }) => {
    await mockPageApi(page);
    await mockStatus(page);
    await mockSession(page, { authenticated: true, user: 'ciadmin', role: null });
    await page.goto('/jobs');
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.locator('h2', { hasText: 'Вхід' })).toBeVisible();
  });

  test('admin: Settings доступні навіть при помилці повторного запиту профілю в header', async ({ page }) => {
    await mockPageApi(page);
    await mockStatus(page);
    let requests = 0;
    await page.route('**/api/session', route => {
      requests++;
      return requests === 2
        ? route.fulfill({ status: 503, json: { detail: 'Temporary profile failure' } })
        : route.fulfill({ json: { authenticated: true, user: 'ciadmin', role: 'admin' } });
    });
    await page.goto('/jobs');
    await expect.poll(() => requests).toBeGreaterThanOrEqual(2);
    const settings = page.locator('nav').first().locator('a[href="/settings?tab=system"]');
    await expect(settings).toBeVisible();
    await expect(page.getByText('Адмін', { exact: true })).toBeVisible();
    await settings.click();
    await expect(page.getByTestId('settings-page')).toBeVisible();
  });

  test('viewer: не отримує admin прав — Settings приховано, header показує «Переглядач»', async ({ page }) => {
    await mockStatus(page);
    await mockSession(page, { authenticated: true, user: 'cireader', role: 'viewer' });
    await mockPageApi(page);
    const nav = page.locator('nav').first();
    await page.goto('/jobs');
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(nav.locator('a[href="/settings?tab=system"]')).toHaveCount(0, { timeout: 20000 });
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

  // Issue #75 S1: Basic credentials are UTF-8 encoded, not latin-1: кирилиця
  // має кодуватися в ті самі байти, якими сервер декодує заголовок.
  test('createSession кодує UTF-8 credentials у Basic Authorization', async ({ page }) => {
    await mockStatus(page);
    let authed = false;
    let capturedAuth: string | null = null;

    await page.route('**/api/session', async (route) => {
      const req = route.request();
      if (req.method() === 'POST') {
        capturedAuth = req.headers()['authorization'] || null;
        authed = true;
        return route.fulfill({ json: { authenticated: true, user: 'ідентифікатор', role: 'admin' } });
      }
      return route.fulfill({ json: { authenticated: authed, user: authed ? 'ідентифікатор' : null, role: authed ? 'admin' : null } });
    });

    await page.goto('/login');
    await expect(page.locator('h2', { hasText: 'Вхід' })).toBeVisible({ timeout: 20000 });
    const login = 'ідентифікатор';
    const password = 'пароль-із-кирилицею';
    await page.locator('input[type="text"]').fill(login);
    await page.locator('input[type="password"]').fill(password);
    await page.getByRole('button', { name: /Увійти/i }).click();

    await expect.poll(() => capturedAuth).not.toBeNull();
    const encoded = (capturedAuth as string).replace(/^Basic /, '');
    expect(Buffer.from(encoded, 'base64').toString('utf8')).toBe(`${login}:${password}`);
  });

  // ── Issue #75 S1: одна identity, жодного role-fallback ──────────────
  test('unknown role: некоректна identity веде на login, а не стає admin', async ({ page }) => {
    await mockPageApi(page);
    await mockStatus(page);
    await mockSession(page, { authenticated: true, user: 'ciadmin', role: 'superuser' });
    await page.goto('/jobs');
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.locator('h2', { hasText: 'Вхід' })).toBeVisible();
  });

  test('authenticated без user: некоректна identity веде на login', async ({ page }) => {
    await mockPageApi(page);
    await mockStatus(page);
    await mockSession(page, { authenticated: true, user: null, role: 'admin' });
    await page.goto('/jobs');
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page).toHaveURL(/\/login$/);
  });

  test('login: 200 без валідної identity не вважається входом', async ({ page }) => {
    await mockStatus(page);
    await page.route('**/api/session', async (route) => {
      const request = route.request();
      if (request.method() === 'POST') {
        // Сервер відповів 200, але без role — це не авторизована сесія.
        return route.fulfill({ json: { authenticated: true, user: 'ciadmin', role: null } });
      }
      return route.fulfill({ json: { authenticated: false, user: null, role: null } });
    });
    await page.goto('/login');
    await expect(page.locator('h2', { hasText: 'Вхід' })).toBeVisible({ timeout: 20000 });
    await page.locator('input[type="text"]').fill('ciadmin');
    await page.locator('input[type="password"]').fill('secret');
    await page.getByRole('button', { name: /Увійти/i }).click();
    await expect(page.getByText('Не вдалося підтвердити сесію')).toBeVisible();
    await expect(page).toHaveURL(/\/login$/);
  });
});

test.describe('Account fields and password policy — Issue #75 S3', () => {
  const PASSWORD_POLICY = { min_length: 12, require_different_from_current: true };

  async function mockAccountApi(
    page: Page,
    session: Record<string, unknown>,
    onProfile?: (body: Record<string, unknown>) => { status?: number; json?: Record<string, unknown> },
  ): Promise<{ saved: () => Record<string, unknown> | null; passwordCalls: () => number }> {
    let saved: Record<string, unknown> | null = null;
    let passwordCalls = 0;
    await mockPageApi(page);
    await mockStatus(page);
    await page.route('**/api/session/profile', async (route) => {
      const body = JSON.parse(route.request().postData() || '{}');
      const result = onProfile?.(body) ?? {};
      saved = body;
      return route.fulfill({ status: result.status ?? 200, json: result.json ?? { ...session, ...body } });
    });
    await page.route('**/api/session/password', async (route) => {
      passwordCalls++;
      const body = JSON.parse(route.request().postData() || '{}');
      const detail = body.old_password === 'wrong-password'
        ? 'Current password is incorrect'
        : 'ok';
      return route.fulfill({ status: detail === 'ok' ? 200 : 400, json: { detail } });
    });
    await page.route('**/api/session', (route) => route.fulfill({ json: session }));
    return { saved: () => saved, passwordCalls: () => passwordCalls };
  }

  test('профіль показує ім\'я, email і політику пароля з сервера', async ({ page }) => {
    const api = await mockAccountApi(page, {
      authenticated: true, user: 'ciadmin', login: 'ciadmin', role: 'admin',
      display_name: 'Адмін Тест', email: 'admin@example.com',
      password_policy: { ...PASSWORD_POLICY, min_length: 14 },
    });
    await page.goto('/profile');
    await expect(page.getByTestId('profile-page')).toBeVisible({ timeout: 20000 });
    await expect(page.getByTestId('profile-login')).toHaveText('ciadmin');
    await expect(page.getByTestId('display-name-input')).toHaveValue('Адмін Тест');
    await expect(page.getByTestId('email-input')).toHaveValue('admin@example.com');
    // Політика береться з сервера, а не з локальної константи.
    await expect(page.getByTestId('password-policy-hint')).toContainText('14');
    expect(api.passwordCalls()).toBe(0);
  });

  test('збереження полів облікового запису надсилає display_name та email', async ({ page }) => {
    const api = await mockAccountApi(page, {
      authenticated: true, user: 'ciadmin', login: 'ciadmin', role: 'admin',
      display_name: null, email: null, password_policy: PASSWORD_POLICY,
    });
    await page.goto('/profile');
    await expect(page.getByTestId('profile-page')).toBeVisible({ timeout: 20000 });
    await page.getByTestId('display-name-input').fill('Нове ім\'я');
    await page.getByTestId('email-input').fill('new@example.com');
    await page.getByTestId('account-save').click();
    await expect.poll(() => api.saved()).toEqual({ display_name: 'Нове ім\'я', email: 'new@example.com' });
  });

  test('серверна помилка профілю показується користувачу', async ({ page }) => {
    await mockAccountApi(
      page,
      { authenticated: true, user: 'ciadmin', login: 'ciadmin', role: 'admin',
        display_name: null, email: null, password_policy: PASSWORD_POLICY },
      () => ({ status: 422, json: { detail: 'Недійсні поля профілю', fields: { email: 'Невірний формат email' } } }),
    );
    await page.goto('/profile');
    await expect(page.getByTestId('profile-page')).toBeVisible({ timeout: 20000 });
    // Client-validator приймає localhost-домен, сервер — відхиляє: перевіряємо
    // саме серверне повідомлення, а не локальну валідацію.
    await page.getByTestId('email-input').fill('user@localhost');
    await page.getByTestId('display-name-input').fill('Ім\'я');
    await page.getByTestId('account-save').click();
    await expect(page.getByTestId('account-error')).toContainText('Недійсні поля профілю');
  });

  test('невірний поточний пароль показується як відновлювана помилка', async ({ page }) => {
    const api = await mockAccountApi(page, {
      authenticated: true, user: 'ciadmin', login: 'ciadmin', role: 'admin',
      display_name: null, email: null, password_policy: PASSWORD_POLICY,
    });
    await page.goto('/profile');
    await expect(page.getByTestId('profile-page')).toBeVisible({ timeout: 20000 });
    await page.getByTestId('old-password-input').fill('wrong-password');
    await page.getByTestId('new-password-input').fill('brand-new-password-12');
    await page.getByTestId('confirm-password-input').fill('brand-new-password-12');
    await page.getByTestId('password-save').click();
    await expect(page.getByTestId('password-error')).toContainText('Current password is incorrect');
    // Сесія лишається чинною, повторна спроба можлива.
    await expect(page.getByTestId('profile-page')).toBeVisible();
    await expect(page.getByTestId('old-password-input')).toBeEditable();
    expect(api.passwordCalls()).toBe(1);
  });

  test('коротший за політику пароль не надсилається на сервер', async ({ page }) => {
    const api = await mockAccountApi(page, {
      authenticated: true, user: 'ciadmin', login: 'ciadmin', role: 'admin',
      display_name: null, email: null, password_policy: PASSWORD_POLICY,
    });
    await page.goto('/profile');
    await expect(page.getByTestId('profile-page')).toBeVisible({ timeout: 20000 });
    await page.getByTestId('old-password-input').fill('current-password-12');
    await page.getByTestId('new-password-input').fill('short');
    await page.getByTestId('confirm-password-input').fill('short');
    await expect(page.getByTestId('password-save')).toBeDisabled();
    expect(api.passwordCalls()).toBe(0);
  });
});


test('втрата сесії при переході між розділами веде на login', async ({ page }) => {
  await mockPageApi(page);
  await mockStatus(page);
  let active = true;
  await page.route('**/api/session', route => route.fulfill({ json: active
    ? { authenticated: true, user: 'admin', role: 'admin' }
    : { authenticated: false, user: null, role: null } }));
  await page.goto('/jobs');
  await expect(page.locator('app-header')).toBeVisible();
  await page.waitForLoadState('networkidle');
  active = false;
  await page.locator('nav a[href="/alerts"]').click();
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.locator('app-header')).toHaveCount(0);
});

test('втрата сесії між guard і header веде на login', async ({ page }) => {
  await mockPageApi(page);
  await mockStatus(page);
  let requests = 0;
  await page.route('**/api/session', route => route.fulfill({ json: ++requests === 1
    ? { authenticated: true, user: 'admin', role: 'admin' }
    : { authenticated: false, user: null, role: null } }));
  await page.goto('/jobs');
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.locator('app-header')).toHaveCount(0);
});
