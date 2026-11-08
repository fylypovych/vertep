import { test, expect } from '@playwright/test';

const INTEGRATIONS = {
  ollama: { status: 'ONLINE', http_status: 200 },
  comfyui: { status: 'OFFLINE', error: 'connection refused' },
  publisher: {
    youtube: { configured: true, ready: true },
    tiktok: { configured: false, ready: false },
    facebook: { configured: true, ready: false },
    instagram: { configured: true, ready: true, missing_scopes: ['publish'] },
    threads: { configured: true },
    telegram: { configured: true, ready: true },
  },
};

async function open(page: any, integrations = INTEGRATIONS) {
  await page.route('**/api/**', async (route: any) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: 'admin', username: 'admin', role: 'admin' } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL' } } });
    if (path.endsWith('/integrations')) return route.fulfill({ json: integrations });
    return route.fulfill({ json: route.request().method() === 'GET' ? [] : {} });
  });
  await page.goto('/settings?tab=integrations');
}

test('плитки підключень мають зелений, жовтий або червоний колір за станом', async ({ page }) => {
  await open(page);
  const tone = (id: string) => page.getByTestId(id);
  await expect(tone('service-status-ollama')).toHaveAttribute('data-tone', 'green', { timeout: 20000 });
  await expect(tone('service-status-comfyui')).toHaveAttribute('data-tone', 'red');
  await expect(tone('publisher-status-youtube')).toHaveAttribute('data-tone', 'green');
  await expect(tone('publisher-status-telegram')).toHaveAttribute('data-tone', 'green');
  await expect(tone('publisher-status-tiktok')).toHaveAttribute('data-tone', 'red');
  for (const id of ['facebook', 'instagram', 'threads']) {
    await expect(tone(`publisher-status-${id}`)).toHaveAttribute('data-tone', 'yellow');
  }
  await expect(tone('publisher-status-youtube')).toHaveClass(/bg-emerald-50/);
  await expect(tone('publisher-status-facebook')).toHaveClass(/bg-amber-50/);
  await expect(tone('publisher-status-tiktok')).toHaveClass(/bg-red-50/);
});

test('сервіс, що відповідає з помилкою HTTP, показано жовтим', async ({ page }) => {
  await open(page, { ...INTEGRATIONS, ollama: { status: 'ONLINE', http_status: 500 } });
  await expect(page.getByTestId('service-status-ollama')).toHaveAttribute('data-tone', 'yellow', { timeout: 20000 });
  await expect(page.getByTestId('service-status-ollama')).toContainText('Відповідає з помилкою');
});
