import { test, expect } from '@playwright/test';

function mockApi(page: any, overrides: Record<string, any> = {}) {
  return page.route('**/api/**', async (route: any) => {
    const r = route.request();
    const p = new URL(r.url()).pathname;
    const m = r.method();
    if (p.endsWith('/session')) return route.fulfill({ json: { username: 'admin', role: 'admin' } });
    if (p.endsWith('/status')) return route.fulfill({ json: overrides.status || { system: { state: 'NORMAL' }, version: '0.0.1.30' } });
    if (p.endsWith('/settings/secrets') && m === 'GET') return route.fulfill({ json: { secrets: overrides.secrets || {} } });
    if (p.endsWith('/settings/secrets') && m === 'PUT') return route.fulfill({ json: { saved: true } });
    if (p.endsWith('/settings/secrets') && m === 'DELETE') return route.fulfill({ json: { deleted: true } });
    if (p.endsWith('/models') && m === 'GET') return route.fulfill({ json: { models: overrides.models || [] } });
    if (p.endsWith('/models') && m === 'POST') return route.fulfill({ json: { ok: true } });
    if (p.endsWith('/settings/logo') && m === 'GET') return route.fulfill({ status: 404 });
    if (p.endsWith('/backups') && m === 'GET') return route.fulfill({ json: { snapshots: overrides.backups || [] } });
    if (p.endsWith('/backups') && m === 'POST') return route.fulfill({ json: { snapshot_id: 'snap_1' } });
    return route.fulfill({ json: [] });
  });
}

test.describe('Settings — Secrets', () => {
  test('Вкладка Секрети відображається', async ({ page }) => {
    await mockApi(page, { secrets: { telegram_bot_token: true, smtp_password: false } });
    await page.goto('/settings');
    await page.getByText('Секрети').click();
    await expect(page.getByTestId('settings-secrets')).toBeVisible();
    await expect(page.getByText('telegram_bot_token')).toBeVisible();
    await expect(page.getByText('Встановлено').first()).toBeVisible();
  });

  test('Редагування секрету', async ({ page }) => {
    await mockApi(page, { secrets: { telegram_bot_token: true } });
    await page.goto('/settings');
    await page.getByText('Секрети').click();
    await page.getByText('telegram_bot_token').locator('..').locator('button:has-text("Редагувати")').click();
    await expect(page.getByRole('button', { name: 'Зберегти' })).toBeVisible();
  });
});

test.describe('Settings — Models', () => {
  test('Вкладка Моделі відображається', async ({ page }) => {
    await mockApi(page, { models: [{ name: 'llama3', size: 4000000000 }] });
    await page.goto('/settings');
    await page.getByText('Моделі').click();
    await expect(page.getByTestId('settings-models')).toBeVisible();
    await expect(page.getByText('llama3')).toBeVisible();
  });
});
