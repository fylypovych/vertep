import { test, expect } from '@playwright/test';

function securityCheck(admin: unknown, adminPasswordSet: boolean) {
  return { ok: true, weak_or_missing: [], recommendation: '', checks: {
    secrets_store: { status: 'ok', detail: '' }, certificates: {}, integrations: [],
    effective_config: { admin_password_set: adminPasswordSet, admin_credentials: admin, node_api_token_set: true,
      postgres_password_set: true, secret_store_passphrase_set: true, session_secret_set: true, publisher_mock: false },
  } };
}

async function open(page: any, body: unknown) {
  await page.route('**/api/**', async (route: any) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, username: 'admin', user: 'admin', role: 'admin' } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL', reason: null }, version: '0' } });
    if (path.endsWith('/security/check')) return route.fulfill({ json: body });
    return route.fulfill({ json: {} });
  });
  await page.goto('/settings?tab=security');
  return page.getByTestId('security-admin-credentials');
}

test('адміністратор із майстра першого запуску без ADMIN_PASSWORD показаний налаштованим', async ({ page }) => {
  const row = await open(page, securityCheck({ state: 'configured', sources: ['first_run'], problems: [] }, true));
  await expect(row).toContainText('Обліковий запис адміністратора: налаштовано (створено в майстрі першого запуску)', { timeout: 20000 });
  await expect(row).not.toContainText('відсутній');
});

test('пошкоджений і відсутній обліковий запис пояснено, а не показано як встановлений', async ({ page }) => {
  let row = await open(page, securityCheck({ state: 'invalid', sources: [], problems: ['first_run_invalid'] }, false));
  await expect(row).toContainText('пароль не може бути перевірений', { timeout: 20000 });
  await expect(row).toContainText('вхід цим обліковим записом неможливий');
  await page.unrouteAll({ behavior: 'ignoreErrors' });
  row = await open(page, securityCheck({ state: 'missing', sources: [], problems: [] }, false));
  await expect(row).toContainText('не налаштовано', { timeout: 20000 });
  await expect(row).toContainText('Жодного облікового запису адміністратора не знайдено');
});

test('env-only адміністратор і англійська локаль', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('vertep_language', 'en'));
  const row = await open(page, securityCheck({ state: 'configured', sources: ['environment'], problems: [] }, true));
  await expect(row).toContainText('Administrator account: configured (ADMIN_PASSWORD variable)', { timeout: 20000 });
});

test('#463: пошкоджений запис майстра з робочим ADMIN_PASSWORD — налаштовано через fallback і з попередженням; uk/en', async ({ page }) => {
  const body = securityCheck({ state: 'configured', sources: ['environment'], problems: ['first_run_invalid'] }, true);
  let row = await open(page, body);
  await expect(row).toContainText('налаштовано (змінна ADMIN_PASSWORD)', { timeout: 20000 });
  await expect(row).toContainText('Частина записів адміністратора пошкоджена; вхід працює через інше джерело.');
  await page.evaluate(() => localStorage.setItem('vertep_language', 'en'));
  await page.reload();
  row = page.getByTestId('security-admin-credentials');
  await expect(row).toContainText('Some administrator records are damaged; sign-in works through another source.', { timeout: 20000 });
});
