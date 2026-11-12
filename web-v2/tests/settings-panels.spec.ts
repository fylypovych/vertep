import { test, expect } from '@playwright/test';

function mockApi(page: any, update: () => Record<string, unknown>, onCheck = () => {}) {
  return page.route('**/api/**', async (route: any) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, username: 'admin', user: 'admin', role: 'admin' } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL', reason: null }, version: '0' } });
    if (path.endsWith('/system/update/check')) { onCheck(); return route.fulfill({ json: { state: 'PENDING' } }); }
    if (path.endsWith('/system/update')) return route.fulfill({ json: update() });
    if (path.endsWith('/settings/secrets')) return route.fulfill({ json: { secrets: { youtube_client_id: true } } });
    return route.fulfill({ json: {} });
  });
}

const base = { current_version: '1', phase: 'NORMAL', progress: 0, message: '', log: [] };

test('ОНОВИТИ активна лише після успішної перевірки з новим релізом', async ({ page }) => {
  let status: Record<string, unknown> = { ...base, state: 'IDLE', available_version: '2', update_available: true };
  let checks = 0;
  await mockApi(page, () => status, () => { checks++; status = { ...base, action: 'check', state: 'SUCCEEDED', available_version: '2', update_available: true }; });
  await page.goto('/settings?tab=update');
  await expect(page.getByTestId('update-install')).toBeDisabled({ timeout: 20000 });
  await page.getByTestId('update-check').click();
  await expect(page.getByTestId('update-install')).toBeEnabled();
  expect(checks).toBe(1);
});

test('ОНОВИТИ лишається неактивною, якщо нового релізу немає або перевірка впала', async ({ page }) => {
  let status: Record<string, unknown> = { ...base, action: 'check', state: 'SUCCEEDED', update_available: false };
  await mockApi(page, () => status);
  await page.goto('/settings?tab=update');
  await expect(page.getByTestId('update-install')).toBeDisabled({ timeout: 20000 });
  status = { ...base, action: 'check', state: 'FAILED', update_available: true };
  await page.reload();
  await expect(page.getByTestId('update-install')).toBeDisabled({ timeout: 20000 });
});

test('англійською кнопка називається UPDATE', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('vertep_language', 'en'));
  await mockApi(page, () => ({ ...base, state: 'IDLE' }));
  await page.goto('/settings?tab=update');
  await expect(page.getByTestId('update-install')).toHaveText('UPDATE', { timeout: 20000 });
  await expect(page.getByTestId('update-check')).toHaveText('Check for updates');
});

test('облікові дані соцмереж згруповані плитками за платформою у «Публікації»', async ({ page }) => {
  await mockApi(page, () => ({}));
  await page.goto('/settings?tab=publishing');
  const youtube = page.getByTestId('secret-group-youtube');
  await expect(youtube).toContainText('YouTube', { timeout: 20000 });
  await expect(youtube).toContainText('youtube_client_id');
  await expect(youtube).toContainText('1/4');
  await page.goto('/settings?tab=secrets');
  await expect(page.getByTestId('secret-group-telegram')).toContainText('telegram_bot_token', { timeout: 20000 });
});
