import { test, expect } from '@playwright/test';

function mockApi(page: any, overrides: Record<string, any> = {}) {
  return page.route('**/api/**', async (route: any) => {
    const r = route.request();
    const p = new URL(r.url()).pathname;
    const m = r.method();
    if (p.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: 'admin', role: 'admin' } });
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
    await mockApi(page, { secrets: { smtp_password: true, external_ai_api_key: false } });
    await page.goto('/settings');
    await page.getByText('Ключі інтеграцій').click();
    await expect(page.getByTestId('settings-secrets')).toBeVisible();
    await expect(page.getByText('smtp_password')).toBeVisible();
    await expect(page.getByText('Збережено', { exact: true }).first()).toBeVisible();
  });

  test('Редагування секрету', async ({ page }) => {
    await mockApi(page, { secrets: { smtp_password: true } });
    await page.goto('/settings');
    await page.getByText('Ключі інтеграцій').click();
    await page.getByText('smtp_password').locator('xpath=ancestor::div[contains(@class,"border-t")][1]').locator('button:has-text("Редагувати")').click();
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

test.describe('Settings — LAB panel', () => {
  test('адреса панелі LAB зберігається й показується після перезавантаження', async ({ page }) => {
    let saved: string | null = null;
    await mockApi(page);
    await page.route('**/api/settings/lab-panel', async (route: any) => {
      if (route.request().method() === 'PUT') saved = route.request().postDataJSON().url || null;
      return route.fulfill({ json: { url: saved } });
    });
    await page.goto('/settings?tab=system');
    await page.getByTestId('lab-panel-url').fill('https://10.10.10.20:8444');
    await page.getByTestId('lab-panel-save').click();
    expect(saved).toBe('https://10.10.10.20:8444');
    await page.reload();
    await expect(page.getByTestId('lab-panel-url')).toHaveValue('https://10.10.10.20:8444');
  });
});

test.describe('Settings — side reads', () => {
  test('відмова в адресі LAB чи у фоновій перевірці бекапів не викидає з налаштувань на вхід', async ({ page }) => {
    await mockApi(page, { status: { system: { state: 'NORMAL' }, version: '0.0.1.19', core: 'OK' } });
    await page.route('**/api/settings/lab-panel', (route: any) => route.fulfill({ status: 401, json: { detail: 'Not authenticated' } }));
    await page.route('**/api/system/backups', (route: any) => route.fulfill({ status: 502, json: { detail: 'Сервіс BACKUP_URL недоступний' } }));
    await page.goto('/settings?tab=system');
    await expect(page.getByTestId('system-info')).toContainText('0.0.1.19', { timeout: 20000 });
    await expect(page).toHaveURL(/\/settings\?tab=system/);
    await page.goto('/settings?tab=backup');
    await expect(page.getByTestId('backup-module-missing')).toBeVisible();
    await expect(page).toHaveURL(/\/settings\?tab=backup/);
  });
});
