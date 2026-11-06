import { test, expect } from '@playwright/test';

function mockApi(page: any, role: string, saved: unknown[]) {
  return page.route('**/api/**', async (route: any) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, username: role, user: role, role } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL', reason: null }, version: '0' } });
    if (path.endsWith('/settings/publishing')) {
      if (request.method() === 'PUT') {
        const body = JSON.parse(request.postData() || '{}');
        if (!body.public_url.startsWith('https://')) return route.fulfill({ status: 400, json: { detail: 'Публічна адреса має мати вигляд https://домен' } });
        saved.push(body);
        return route.fulfill({ json: body });
      }
      return route.fulfill({ json: { public_url: '', telegram_channel_id: '@old_channel' } });
    }
    return route.fulfill({ json: request.method() === 'GET' ? [] : {} });
  });
}

test('адмін зберігає параметри публікації й бачить відмову сервера', async ({ page }) => {
  const saved: unknown[] = [];
  await mockApi(page, 'admin', saved);
  await page.goto('/settings?tab=publishing');
  await expect(page.getByTestId('publishing-telegram-channel')).toHaveValue('@old_channel', { timeout: 20000 });
  await page.getByTestId('publishing-public-url').fill('http://internal');
  await page.getByTestId('publishing-save').click();
  await expect(page.getByTestId('publishing-error')).toContainText('https://домен');
  await page.getByTestId('publishing-public-url').fill('https://vertep.example.com');
  await page.getByTestId('publishing-save').click();
  await expect.poll(() => saved.length).toBe(1);
  expect(saved[0]).toEqual({ public_url: 'https://vertep.example.com', telegram_channel_id: '@old_channel' });
});
