import { test, expect } from '@playwright/test';

const status = { configured: false, selected_role: null, hardware: { cpu: 'x86_64' }, backends: ['ollama'],
  roles: { core: { label: 'Основний сервер', modules: ['core'], capabilities: [] },
           gpu: { label: 'GPU', modules: ['worker'], capabilities: ['image_generation'] } } };
const completed = { installation_id: 'id-1', installation_name: 'Vertep', manifest_sha256: 'abc', core_url: 'https://core',
  registration_token: 'VT-1', core_certificate: 'CERT', backup_key: 'SECRET-KEY' };

test('майстер проходить усі кроки, а ключ не потрапляє в маніфест', async ({ page }) => {
  let sent: Record<string, unknown> = {};
  let token = '';
  await page.route('**/api/setup', route => { token = route.request().headers()['x-vertep-setup-token']; return route.fulfill({ json: status }); });
  await page.route('**/api/setup/health*', route => route.fulfill({ json: { ready: true, checks: { core: 'OK' } } }));
  await page.route('**/api/setup/complete', route => { sent = route.request().postDataJSON(); return route.fulfill({ json: completed }); });
  await page.goto('/setup?token=abc');
  const next = page.locator('#next');
  await expect(page.locator('#coreConnection')).toBeHidden();
  await expect.poll(() => token).toBe('abc');

  await next.click();
  await next.click();
  await page.fill('#username', 'admin');
  await page.fill('#password', 'short');
  await page.fill('#confirmation', 'short');
  await next.click();
  await expect(page.getByTestId('setup-error')).toContainText('12');
  await page.fill('#password', 'long-enough-password');
  await page.fill('#confirmation', 'long-enough-password');
  await next.click();
  await next.click();
  await expect(page.locator('#hardware')).toContainText('x86_64');
  await next.click();
  await next.click();
  await page.fill('#telegramToken', 'tg-token');
  await next.click();
  await expect(page.locator('#health')).toContainText('core … OK');
  await expect(next).toHaveText('Завершити налаштування');
  await next.click();

  await expect(page.locator('#manifest')).toContainText('id-1');
  expect(sent['ai_backend']).toBe('ollama');
  expect(sent['telegram_bot_token']).toBe('tg-token');
  const manifest = page.waitForEvent('download');
  await page.locator('#downloadManifest').click();
  const body = await (await manifest).createReadStream();
  let text = '';
  for await (const chunk of body) text += chunk;
  expect(text).not.toContain('SECRET-KEY');

  await next.click();
  await expect(next).toBeDisabled();
  await expect(page.locator('#backupKeySaved')).toBeDisabled();
  const key = page.waitForEvent('download');
  await page.locator('#backupKeyDownload').click();
  expect((await key).suggestedFilename()).toBe('vertep-backup.key');
  await page.locator('#backupKeySaved').check();
  await next.click();
  await expect(page.locator('#result')).toContainText('VT-1');
  await expect(page.locator('#certificate')).toContainText('CERT');
  await expect(next).toHaveText('Відкрити Dashboard');
});

test('без токена майстер просить одноразовий код', async ({ page }) => {
  await page.route('**/api/setup', route => route.fulfill({ status: 401, body: 'Invalid setup code' }));
  await page.goto('/setup');
  await page.fill('#setupCode', 'code-1');
  await page.getByRole('button', { name: 'Продовжити' }).click();
  await expect(page).toHaveURL(/token=code-1/);
});

test('панель не веде на старий інтерфейс', async ({ page }) => {
  await page.route('**/api/**', route => route.fulfill({ json: {} }));
  await page.goto('/login');
  await expect(page.locator('text=Класичний дизайн')).toHaveCount(0);
});
