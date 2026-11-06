import { test, expect } from '@playwright/test';

function mockApi(page: any, retry: { status: number; body: unknown }) {
  return page.route('**/api/**', async (route: any) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, username: 'admin', user: 'admin', role: 'admin' } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL', reason: null }, version: '0' } });
    if (path.endsWith('/jobs/j1/retry')) return route.fulfill({ status: retry.status, json: retry.body });
    if (path.endsWith('/jobs/j1')) return route.fulfill({ json: { job_id: 'j1', topic: 'Моя тема', status: 'FAILED', character_id: 'c', events: [], event_log: [], published_to: [], publication_results: {}, stages: {}, scenes: [], artifacts: [], created_at: '2026-01-31T15:00:00Z' } });
    return route.fulfill({ json: route.request().method() === 'GET' ? [] : {} });
  });
}

test('перший вхід українською, вибір мови зберігається', async ({ page }) => {
  await page.route('**/api/**', (route: any) => route.fulfill({ status: 401, json: {} }));
  await page.goto('/login');
  await expect(page.getByRole('heading', { name: 'Вхід' })).toBeVisible({ timeout: 20000 });
  await expect(page.locator('html')).toHaveAttribute('lang', 'uk');
  await page.getByTestId('language-en').click();
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible({ timeout: 20000 });
  await page.getByTestId('language-uk').click();
  await expect(page.getByRole('heading', { name: 'Вхід' })).toBeVisible();
});

test('перемикання мови не губить введений текст', async ({ page }) => {
  await page.route('**/api/**', (route: any) => route.fulfill({ status: 401, json: {} }));
  await page.goto('/login');
  const login = page.locator('input[formcontrolname="login"]');
  await login.fill('адміністратор', { timeout: 20000 });
  await page.getByTestId('language-en').click();
  await expect(login).toHaveValue('адміністратор');
});

test('панель перекладається, а дані користувача ні', async ({ page }) => {
  await mockApi(page, { status: 500, body: {} });
  await page.goto('/jobs/j1');
  await expect(page.getByText('Моя тема').first()).toBeVisible({ timeout: 20000 });
  await page.getByTestId('language-en').click();
  await expect(page.getByText('Моя тема').first()).toBeVisible();
  await expect(page.getByTestId('language-en')).toHaveAttribute('aria-pressed', 'true');
});

for (const [language, text] of [['uk', 'Завдання не знайдено'], ['en', 'Job not found']] as const) {
  test(`помилка з кодом показується мовою ${language}`, async ({ page }) => {
    await page.addInitScript((value: string) => localStorage.setItem('vertep_language', value), language);
    await mockApi(page, { status: 404, body: { detail: 'Job not found', code: 'job_not_found' } });
    await page.goto('/jobs/j1');
    await page.locator('button', { hasText: language === 'en' ? /Retry/ : /Повторити/ }).first().click({ timeout: 20000 });
    await page.locator('app-confirm-dialog button').last().click();
    await expect(page.getByText(text).first()).toBeVisible({ timeout: 10000 });
  });
}

test('невідомий код англійською не показує український текст сервера', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('vertep_language', 'en'));
  await mockApi(page, { status: 409, body: { detail: 'Сервер сказав українською', code: 'http_409' } });
  await page.goto('/jobs/j1');
  await page.locator('button', { hasText: /Retry/ }).first().click({ timeout: 20000 });
  await page.locator('app-confirm-dialog button').last().click();
  await expect(page.getByText('Сервер сказав українською')).toHaveCount(0);
});
