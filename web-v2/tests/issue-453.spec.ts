import { test, expect } from '@playwright/test';

const SOCIAL = ['youtube_client_id', 'youtube_client_secret', 'youtube_access_token', 'youtube_refresh_token', 'facebook_access_token',
  'instagram_client_id', 'instagram_client_secret', 'instagram_access_token', 'instagram_refresh_token',
  'threads_client_id', 'threads_client_secret', 'threads_access_token', 'threads_refresh_token',
  'tiktok_client_id', 'tiktok_client_secret', 'tiktok_access_token', 'tiktok_refresh_token'];
const SERVICE = ['telegram_bot_token', 'money_printer_token', 'external_ai_api_key', 'smtp_password'];

function mockApi(page: any, store: Record<string, string>, writes: { method: string; name: string; body?: any }[]) {
  return page.route('**/api/**', async (route: any) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, username: 'admin', user: 'admin', role: 'admin' } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL', reason: null }, version: '0' } });
    // The store answers only saved/not saved, as the real API does: values never come back.
    if (path.endsWith('/settings/secrets') && method === 'GET') {
      return route.fulfill({ json: { secrets: Object.fromEntries([...SOCIAL, ...SERVICE].map(name => [name, !!store[name]])) } });
    }
    const secret = path.match(/\/settings\/secrets\/([^/]+)$/);
    if (secret) {
      const name = decodeURIComponent(secret[1]);
      if (method === 'PUT') { store[name] = JSON.parse(request.postData() || '{}').value; writes.push({ method, name }); }
      if (method === 'DELETE') { delete store[name]; writes.push({ method, name }); }
      return route.fulfill({ json: { secrets: {} } });
    }
    if (path.endsWith('/settings/publishing')) return route.fulfill({ json: { public_url: 'https://vertep.example.com', telegram_channel_id: '@news' } });
    return route.fulfill({ json: method === 'GET' ? [] : {} });
  });
}

test('соцмережі керуються у «Публікації», сервісні ключі — у «Ключах інтеграцій», Telegram token один', async ({ page }) => {
  const store: Record<string, string> = { telegram_bot_token: 'old-bot', youtube_refresh_token: 'old-refresh' };
  const writes: { method: string; name: string }[] = [];
  await mockApi(page, store, writes);

  await page.goto('/settings?tab=publishing');
  const credentials = page.getByTestId('settings-publishing');
  await expect(credentials.getByTestId('secret-group-youtube')).toContainText('1/4', { timeout: 20000 });
  for (const name of SOCIAL) await expect(credentials).toContainText(name);
  for (const platform of ['youtube', 'facebook', 'instagram', 'threads', 'tiktok']) {
    await expect(credentials.getByTestId(`secret-group-${platform}`)).toBeVisible();
  }
  // Telegram publishing shows the shared token state and links to its single editing place.
  const telegram = credentials.getByTestId('secret-shared-telegram-publishing');
  await expect(telegram).toContainText('Збережено');
  await expect(telegram.getByRole('button', { name: 'Редагувати' })).toHaveCount(0);
  // Editing in the publishing tab writes through the existing secret API, no other value is touched.
  const tiktok = credentials.getByTestId('secret-group-tiktok');
  await tiktok.getByText('tiktok_access_token').locator('xpath=ancestor::div[contains(@class,"border-t")][1]')
    .getByRole('button', { name: 'Редагувати' }).click();
  await tiktok.locator('#secret-tiktok_access_token').fill('new-tiktok');
  await tiktok.getByRole('button', { name: 'Зберегти' }).click();
  await expect(tiktok).toContainText('1/4');
  expect(writes).toEqual([{ method: 'PUT', name: 'tiktok_access_token' }]);
  expect(store).toEqual({ telegram_bot_token: 'old-bot', youtube_refresh_token: 'old-refresh', tiktok_access_token: 'new-tiktok' });
  // Saved values are never rendered.
  const html = await page.content();
  for (const value of ['old-bot', 'old-refresh', 'new-tiktok']) expect(html).not.toContain(value);

  await telegram.getByRole('link').click();
  await expect(page).toHaveURL(/tab=secrets/);
  const keys = page.getByTestId('settings-secrets');
  await expect(keys.getByRole('heading', { name: 'Ключі інтеграцій' })).toBeVisible();
  await expect(keys.getByTestId('secret-group-telegram')).toContainText('Збережено');
  for (const name of SERVICE) await expect(keys).toContainText(name);
  for (const name of SOCIAL) await expect(keys).not.toContainText(name);
  await expect(keys.getByTestId('secrets-publishing-moved')).toContainText('Публікація');
});

test('старе посилання tab=secrets відкриває «Ключі інтеграцій»; англійська локаль', async ({ page }) => {
  await mockApi(page, {}, []);
  await page.addInitScript(() => localStorage.setItem('vertep_language', 'en'));
  await page.goto('/settings?tab=secrets');
  await expect(page.getByTestId('settings-secrets').getByRole('heading', { name: 'Integration keys' })).toBeVisible({ timeout: 20000 });
  await expect(page.getByTestId('secret-group-telegram')).toContainText('Telegram bot');
  await page.goto('/settings?tab=publishing');
  await expect(page.getByTestId('settings-publishing')).toContainText('Platform credentials', { timeout: 20000 });
  await expect(page.getByTestId('secret-shared-telegram-publishing')).toContainText('same token as the Telegram control bot');
});
